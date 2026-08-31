"""Candidate generation and staged reranking.

Two structural properties matter here and neither is expressible as a single
summed score:

1.  **Hard constraints are not tradeable.** The previous reranker added every
    component into one number, so a strong BM25 match could outrank a product
    that violated a stated budget despite the ``-60`` penalty. Ranking is now
    keyed on ``(-hard_violations, score)``: a determinate violation cannot be
    bought back with lexical relevance, no matter how large.

2.  **Structured coverage is sparse.** ``price`` is present on 21% of the
    catalog, ``details.Color`` on 4.9%, ``details.Material`` on 4.1%. A filter
    that treats "field absent" as "does not match" would discard most of the
    catalog. Every hard predicate is therefore three-valued — satisfied,
    violated, or *unknown* — and only ``violated`` counts. Where the field is
    absent the old soft penalty still applies, inside the score.

Stages 2-4 are ordered by magnitude rather than lexicographically. Strict
lexicographic ordering below the gate would make stages 3 and 4 dead code:
stage-2 scores are continuous, exact ties essentially never occur, and a
tolerance-banded comparator is not transitive so it cannot drive a sort.
Ordering by influence — relevance, then soft preference at a fraction of it,
then a bounded profile epsilon — is what "tie-break only" can actually mean.

Every numeric knob lives in :class:`RerankWeights` and every score is emitted as
``(detail_key, weight_name, coefficient)`` contributions, so the calibration
harness scores a weight vector by dot product against the same code path that
serves production. There is no second scorer to drift out of sync.
"""

from __future__ import annotations

import gzip
import json
import math
from collections import Counter
import os
import re
import sqlite3
from dataclasses import dataclass, fields, replace
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, NamedTuple


TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "please",
    "some",
    "that",
    "the",
    "this",
    "to",
    "want",
    "with",
    "would",
    "you",
    "looking",
}

COLORS = {
    "black",
    "white",
    "blue",
    "red",
    "pink",
    "green",
    "brown",
    "gray",
    "grey",
    "purple",
    "yellow",
    "orange",
}

MATERIALS = {
    "cotton",
    "polyester",
    "nylon",
    "leather",
    "wool",
    "spandex",
    "silk",
    "rayon",
    "fabric",
}

# These are ubiquitous listing fields, not meaningful differentiators.  A
# product should not receive a feature bonus merely for being "Imported".
BOILERPLATE_PHRASES = {
    "imported",
    "machine wash",
    "pull on closure",
    "zipper closure",
    "button closure",
    "snap closure",
    "hook and eye closure",
}

FIELD_WEIGHTS = (0.0, 6.0, 5.0, 4.0, 2.5, 3.0, 1.0, 1.5)

# Unique query terms passed to FTS5. Was 50, which a multi-turn session crosses
# around turn 8 once the shopper's own words are part of the query -- silently
# dropping the newest disclosures. FTS5 handles a wider OR comfortably, and
# `state_manager.query_fragment` now removes the dialogue scaffolding that made
# the old ceiling bind so early.
MAX_QUERY_TERMS = 128

# Fractions of the per-attribute boost awarded at partial match. Structural
# shape rather than free parameters: calibrating both the boost and its
# fractions makes the score bilinear and the coordinate search ill-posed.
GENERIC_MID_FRACTION = 0.55
GENERIC_LOW_FRACTION = 0.2

# Structured `details` keys retained per product for `attribute_stats`.
# Coverage is low by design of the source data -- Color 4.9%, Material 4.1%,
# Style 3.5%, Size 1.9% -- which is exactly what the clarification policy
# needs to know before spending a turn asking about one.
FACET_KEYS = ("Color", "Material", "Size", "Style")


# =============================================================================
# DEPARTMENT
# =============================================================================
#
# `details.Department` is on 87.2% of the catalog -- by a wide margin the best
# covered structured field, and the one shoppers most reliably state. "hoodies
# for men" should not return women's hoodies, and no amount of lexical overlap
# should rescue one.

_DEPARTMENT_CANON = {
    "womens": "womens",
    "women": "womens",
    "woman": "womens",
    "ladies": "womens",
    "mens": "mens",
    "men": "mens",
    "man": "mens",
    "girls": "girls",
    "girl": "girls",
    "boys": "boys",
    "boy": "boys",
    "unisex": "unisex",
    "unisex-adult": "unisex",
    "unisex-child": "unisex",
    "unisex-baby": "unisex",
    "baby-girls": "baby",
    "baby-boys": "baby",
    "baby": "baby",
    "toddler": "baby",
}

# Query-side cues, deliberately narrow. Pronouns ("her", "him") and relationship
# words ("wife") are excluded: they appear in gift framing that says nothing
# about the listing's department and would fire on the wrong sessions.
_DEPARTMENT_CUES = {
    "womens": {"women", "womens", "woman", "ladies", "female"},
    "mens": {"men", "mens", "man", "male"},
    "girls": {"girls", "girl"},
    "boys": {"boys", "boy"},
    "baby": {"baby", "infant", "newborn", "toddler"},
    "unisex": {"unisex"},
}


def _canonical_department(value: object) -> str | None:
    """Map a free-text department to a canonical audience token."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text in _DEPARTMENT_CANON:
        return _DEPARTMENT_CANON[text]
    for token in TOKEN_RE.findall(text):
        if token in _DEPARTMENT_CANON:
            return _DEPARTMENT_CANON[token]
    return None


def _infer_department(text: str) -> str | None:
    """Read a department out of the shopper's own words, or return None.

    Ambiguity abstains. A message naming both "men" and "women" (comparison
    shopping, a couples gift) yields no filter rather than an arbitrary one.
    """
    tokens = {token.lower() for token in TOKEN_RE.findall(text)}
    found = {
        department
        for department, cues in _DEPARTMENT_CUES.items()
        if tokens & cues
    }
    found.discard("unisex")
    if len(found) != 1:
        return None
    return found.pop()


def _department_conflicts(wanted: str | None, actual: str | None) -> bool:
    """True when two departments cannot describe the same product.

    Used as a hard signal: a menswear listing is not a near-miss for a
    womenswear request, it is wrong.
    """
    if wanted is None or actual is None:
        return False
    if wanted == actual:
        return False
    # Unisex listings answer any department; a unisex request accepts anything.
    if "unisex" in (wanted, actual):
        return False
    return True


# =============================================================================
# WEIGHTS
# =============================================================================


@dataclass(frozen=True)
class RerankWeights:
    """Every calibratable magnitude, defaulting to the pre-calibration values.

    Constructed with no arguments this reproduces the committed baseline
    exactly, which is what makes the ablation table in HANDOVER_D honest.
    """

    # --- stage 2: candidate generation and quality ---
    fusion_scale: float = 100.0
    backfill_scale: float = 0.2
    rating_coefficient: float = 0.4
    popularity_scale: float = 1.0

    # --- stage 2: hard structured constraints ---
    category_exact: float = 45.0
    category_partial: float = 25.0
    category_weak: float = 10.0
    category_miss: float = -20.0

    brand_store: float = 50.0
    brand_text: float = 30.0
    brand_weak: float = 12.0
    brand_miss: float = -50.0

    material_boost: float = 40.0
    color_boost: float = 35.0
    size_boost: float = 35.0
    feature_boost: float = 25.0
    use_case_boost: float = 25.0
    style_boost: float = 20.0
    other_boost: float = 22.0

    vocabulary_miss: float = -20.0
    generic_miss: float = -12.0

    budget_within: float = 30.0
    budget_over: float = -60.0
    budget_near: float = 30.0
    budget_near_miss: float = -35.0
    budget_loose: float = 15.0
    budget_loose_miss: float = -20.0
    budget_unpriced: float = -8.0

    # `details.Department` is 87% covered but disagrees with the shopper's own
    # wording on ~5% of ESCI targets -- real ambiguity ("boys" vs a small
    # "mens" listing), not fixable noise. Well-covered evidence that is
    # sometimes wrong earns a penalty, not an exclusion.
    department_miss: float = -25.0
    department_match: float = 0.0

    # --- stage 3: soft preferences and superseded evidence ---
    soft_scale: float = 1.0
    demoted_exact: float = 18.0
    demoted_partial: float = 8.0

    # --- ranking experiments ---
    # Coverage is a fraction in [0,1], so this is its full swing.
    coverage_scale: float = 0.0
    # IDF-weighted evidence, also normalized to [0,1] per span before scaling.
    idf_evidence_scale: float = 0.0
    # Session-level reciprocal-rank fusion, normalized to [0,1].
    consensus_scale: float = 0.0

    # --- stage 4: profile tie-break ---
    profile_scale: float = 0.0

    def as_mapping(self) -> dict[str, float]:
        """Expose the constraint set as a plain attribute -> value dict."""
        return {item.name: float(getattr(self, item.name)) for item in fields(self)}

    def with_values(self, values: dict[str, float]) -> "RerankWeights":
        """Return a copy carrying replacement values, leaving the original intact."""
        return replace(self, **{key: float(value) for key, value in values.items()})


WEIGHT_NAMES: tuple[str, ...] = tuple(item.name for item in fields(RerankWeights))


# Output of `python3 -m scripts.calibrate_rerank` -- coordinate search fitted on
# synth800/realistic and held out on the 234 ESCI provenance=="gold" rows, which
# carry real human E/S/C/I judgments. It transfers (held-out technical
# 0.7651 -> 0.8120), so the weights carry signal about shopper intent rather
# than about the generator that wrote the fitting queries.
#
# Validated on the real agent loop, n=200 stratified, technical score:
#
#     esci1000/realistic   0.7385 -> 0.8440   +0.1055
#     synth800/realistic   0.7495 -> 0.8690   +0.1195
#     esci1000/esci        0.7718 -> 0.8161   +0.0443
#     synth800/official    0.8681 -> 0.8668   -0.0012
#     esci1000/official    0.8754 -> 0.8616   -0.0138
#     public200/official   0.9022 -> 0.8474   -0.0548   <-- the cost
#                                       mean  +0.0332
#
# NOT the default. `README.md` names the official column "the
# constraint to not destroy", and -0.0548 on the closest proxy to the official
# leaderboard is a trade for the team to take deliberately, not one for the
# retriever to take silently. Opt in with `RERANK_WEIGHTS=calibrated`.
#
# What it learned, in one line: trust BM25 rank far more (fusion_scale 100 ->
# 774) and stop punishing absent metadata (generic_miss -12 -> 0,
# budget_loose_miss -20 -> 0). The old weights let a popularity bonus spanning
# 0..1.9 compete against a fusion range of 1.46 across the whole pool, and
# penalized products for lacking a Color field that only 4.9% of the catalog
# has. The one penalty the search made *stronger* is department_miss
# (-25 -> -55), which is also the only one backed by a well-covered field.
CALIBRATED_WEIGHTS = RerankWeights(
    fusion_scale=774.4,
    rating_coefficient=0.0704,
    popularity_scale=0.24,
    category_exact=50.625,
    category_partial=18.0,
    category_weak=4.4,
    category_miss=-44.0,
    brand_store=30.0,
    color_boost=18.9,
    feature_boost=3.6,
    use_case_boost=10.0,
    style_boost=70.4,
    vocabulary_miss=-12.8,
    generic_miss=0.0,
    budget_near=12.0,
    budget_near_miss=-56.0,
    budget_loose=2.4,
    budget_loose_miss=0.0,
    department_miss=-55.0,
    department_match=0.1,
    demoted_exact=7.2,
    profile_scale=0.1,
    # Part 2 ranking experiments. Sized against the fusion range: with
    # fusion_scale=774 a rank-1 BM25 hit contributes ~12.7 and rank-500 ~1.4,
    # so a full-coverage product gains roughly one BM25 rank-decade.
    coverage_scale=22.0,
    idf_evidence_scale=16.0,
    consensus_scale=9.0,
)



# Output of `python3 -m scripts.train_reranker` -- gradient descent on the 770
# human ESCI relevance judgments that join onto the frozen catalog
# (`scripts/build_esci_gold.py`), not on anything our simulators wrote. 457 of
# those queries retrieve at least one judged product, split 378/79 by ESCI's own
# train/test partition.
#
# Selected at epoch 41 of 500 by held-out MRR. The full curve is in
# `docs/reranker_training.json` and it is not subtle: training loss falls
# monotonically to epoch 500 while held-out MRR peaks at 41 and then decays
# (0.2708 -> 0.2422). On 37 parameters and 378 queries, more epochs buy
# memorisation.
#
# Held-out single-turn retrieval over real queries:
#
#     MRR      0.2220 -> 0.2708   +22% relative
#     hit@10   0.3671 -> 0.4937   +35% relative
#
# What it learned, and why it does not simply ship: the fitted vector
# effectively deletes the soft stage (`soft_scale` 1.0 -> 0.0009), flips
# `vocabulary_miss` and `budget_unpriced` from penalties to bonuses, collapses
# `color_boost` 18.9 -> 1.1, and triples `popularity_scale`. Read together
# that is one finding -- on a real one-line shopper query, the attribute-boost
# machinery is mostly noise and BM25 rank plus popularity carries the signal.
#
# The catch is the training distribution. Every example is a **turn-1** query,
# because a human judgment attaches to a query and not to a conversation. Any
# weight whose feature only becomes active once constraints accumulate is
# therefore either untrained (zero gradient, left at its calibrated value --
# every `category_*`, `demoted_*` and the ranking-experiment scales below) or
# trained on an unrepresentative slice (`soft_scale`, which multiplies exactly
# the Tier 1 gazetteer constraints that a multi-turn session leans on).
# `ESCI_TRAINED_SOFT_WEIGHTS` isolates that single coordinate so the bench can
# say which half of the change carries.
ESCI_TRAINED_WEIGHTS = RerankWeights(
    fusion_scale=753.9511,
    backfill_scale=0.2,
    rating_coefficient=0.06,
    popularity_scale=3.0529,
    category_exact=50.625,
    category_partial=18.0,
    category_weak=4.4,
    category_miss=-44.0,
    brand_store=26.0717,
    brand_text=20.4381,
    brand_weak=17.3975,
    brand_miss=-50.0,
    material_boost=29.079,
    color_boost=1.1101,
    size_boost=22.8117,
    feature_boost=-13.9722,
    use_case_boost=10.0,
    style_boost=58.0115,
    other_boost=22.0,
    vocabulary_miss=5.3478,
    generic_miss=-0.0715,
    budget_within=10.7997,
    budget_over=-60.0,
    budget_near=12.0,
    budget_near_miss=-56.0,
    budget_loose=2.4,
    budget_loose_miss=0.0,
    budget_unpriced=11.2003,
    department_miss=-34.5,
    department_match=0.0085,
    soft_scale=0.0009,
    demoted_exact=7.2,
    demoted_partial=8.0,
    coverage_scale=22.0,
    idf_evidence_scale=16.0,
    consensus_scale=9.0,
    profile_scale=0.1,
)

# The fitted vector with `soft_scale` held at its shipped value. Everything the
# ESCI labels say about *magnitudes* is kept; the one coordinate whose training
# distribution is known to be unrepresentative is not.
ESCI_TRAINED_SOFT_WEIGHTS = replace(ESCI_TRAINED_WEIGHTS, soft_scale=1.0)




# `python3 -m scripts.train_reranker --anchor 0.5 --freeze soft_scale`.
#
# The free fit above is a measured regression on every bench cell, so this is
# the same human labels asked a narrower question: not "what do you say?" but
# "what do you say that is worth overruling a session-level calibration for?"
# The anchor penalises drift from `CALIBRATED_WEIGHTS` relative to each
# coordinate's own magnitude, and `soft_scale` is frozen because every training
# example is a turn-1 query and that coordinate multiplies exactly the Tier 1
# constraints only a multi-turn session accumulates.
#
# Held out on ESCI's test split: MRR 0.2220 -> 0.2550 at epoch 273 of 500,
# so the labels still carry signal once the soft stage is protected -- the
# free fit's gain was not merely the side effect of deleting it.
#
# What survives the anchor: BM25 fusion down (774 -> 643) with quality up
# (rating 0.070 -> 0.113, popularity 0.24 -> 0.60), `color_boost` and
# `style_boost` collapsed, and `department_miss` almost eliminated
# (-55 -> -2). That last one is the interesting disagreement -- it is the
# penalty `CALIBRATED_WEIGHTS` deliberately made *stronger* on the grounds
# that Department is the best-covered field in the catalog, and real shopper
# queries say it costs more than it earns.
ESCI_ANCHORED_WEIGHTS = RerankWeights(
    fusion_scale=643.1958,
    backfill_scale=0.2,
    rating_coefficient=0.1133,
    popularity_scale=0.6047,
    category_exact=50.625,
    category_partial=18.0,
    category_weak=4.4,
    category_miss=-44.0,
    brand_store=29.274,
    brand_text=28.1837,
    brand_weak=12.0,
    brand_miss=-50.0,
    material_boost=31.1058,
    color_boost=0.5147,
    size_boost=31.5714,
    feature_boost=3.5789,
    use_case_boost=10.0,
    style_boost=23.8778,
    other_boost=22.0,
    vocabulary_miss=-5.2908,
    generic_miss=0.0,
    budget_within=27.4577,
    budget_over=-60.0,
    budget_near=12.0,
    budget_near_miss=-56.0,
    budget_loose=2.4,
    budget_loose_miss=0.0,
    budget_unpriced=-7.7857,
    department_miss=-1.9899,
    department_match=0.1206,
    soft_scale=1.0,
    demoted_exact=7.2,
    demoted_partial=8.0,
    coverage_scale=22.0,
    idf_evidence_scale=16.0,
    consensus_scale=9.0,
    profile_scale=0.1,
)




# The one coordinate of the ESCI fit that survives contact with the bench.
#
# `docs/esci_reranker.md` §5: applying the learned weights one at a time shows
# the -0.14 is not diffuse. `feature_boost` going negative (3.6 -> -14.0)
# accounts for -0.1411 of it on public200/official by itself, and it is an
# artifact of the training distribution rather than a finding -- the Tier 2
# prompt says "prefer feature when unsure", so `feature` is where unclassified
# spans land, and on a real one-line query matching them predicts nothing.
#
# `popularity_scale` is the opposite: 0.24 -> 3.05 is the only learned change
# that is positive on both probe cells on its own. On the *unmodified official
# evaluator*, full public set, LLM off:
#
#     popularity_scale   technical   HR@10    MRR     MTTC
#     0.24 (shipped)        0.8427   0.9500   0.6840  2.875
#     1.5                   0.8774   0.9550   0.7856  2.790
#     3.05 (this)           0.8883   0.9600   0.8084  2.710
#     4.5                   0.9011   0.9750   0.8165  2.565
#     6.0                   0.9072   0.9850   0.8127  2.455
#
# +0.0456 on the scored set, carried by MRR. Smooth and monotonic to 6.0, so
# 3.05 sits on a trend rather than a spike.
#
# Why it is a prior and not an exploit: `competition_specification.md` says the
# target is "based on a real purchase record", and real purchases are of popular
# products. Median `rating_number` of the target is 6,846 on public200 against
# 12 for the catalog itself -- 570x. A rating-count term spanning 0..2.0 was
# being outvoted by a fusion range of 12.7.
#
# 3.05 rather than the higher-scoring 6.0 **on purpose**. 3.05 is what the human
# ESCI labels chose without ever seeing public200; 6.0 is what sweeping public200
# chooses, which is fitting to the set being scored. Provenance is the reason to
# prefer the smaller number.
#
# The trade: -0.027 on synth800/realistic, whose targets were sampled uniformly
# (median rating_number 13) and so carry no popularity signal at all. See
# `docs/esci_reranker.md` Sec 6 -- that cell is the one that is wrong.
#
# NOT the default. A distributional bet on the hidden 800 is the team's call.
ESCI_POPULARITY_WEIGHTS = replace(CALIBRATED_WEIGHTS, popularity_scale=3.0529)


WEIGHT_PRESETS: dict[str, RerankWeights] = {
    "default": RerankWeights(),
    "calibrated": CALIBRATED_WEIGHTS,
    "esci": ESCI_TRAINED_WEIGHTS,
    "esci_soft": ESCI_TRAINED_SOFT_WEIGHTS,
    "esci_anchored": ESCI_ANCHORED_WEIGHTS,
    "esci_popularity": ESCI_POPULARITY_WEIGHTS,
}


def weights_from_env() -> RerankWeights:
    """Pick a weight preset by name; unknown names fall back to the default."""
    return WEIGHT_PRESETS.get(
        os.environ.get("RERANK_WEIGHTS", "default").strip().lower(), RerankWeights()
    )

# Which attribute feeds which boost knob.
_ATTRIBUTE_BOOST = {
    "material": "material_boost",
    "color": "color_boost",
    "size": "size_boost",
    "feature": "feature_boost",
    "use_case": "use_case_boost",
    "style": "style_boost",
    "other": "other_boost",
}


@dataclass(frozen=True)
class RerankConfig:
    """Behavioural switches, separate from magnitudes so ablations are clean."""

    staged: bool = True
    prefilter: bool = True
    exclude_negated: bool = True
    soft_abstain: bool = True
    # Department as a score penalty: on by default. As a *hard* gate it is off
    # by default -- measured at -0.019 technical on synth800/official, because
    # rejecting the target outright costs more than mis-ranking it ever can.
    department_penalty: bool = True
    department_gate: bool = False
    profile_tiebreak: bool = True
    # Over-fetch so the prefilter spends the candidate budget on survivors
    # rather than on products it is about to reject.
    overfetch: int = 3
    # Never prefilter below this many survivors; a filter that empties the pool
    # is worse than no filter at all.
    min_survivors: int = 50

    # --- ranking experiments (Part 2). Each ablates independently. ---
    #
    # Every remaining miss is a *ranking* miss: the calibration harness shows
    # the target inside the candidate pool in 100% of sessions. So these three
    # change ordering only; none of them widens the net.
    #
    # Reward breadth of constraint satisfaction, not just depth. Additive
    # scoring lets one huge category match outrank a product that quietly
    # satisfies four disclosed constraints -- but the target is precisely the
    # thing that satisfies all of them.
    # Measured NEUTRAL (mean technical 0.8179 vs 0.8183 without, n=100 x 6
    # cells). With few live spans most survivors satisfy all of them, so the
    # term is near-constant across the pool and discriminates nothing. Kept,
    # off, because it should earn its place again if span counts rise.
    coverage_bonus: bool = False
    # Weight span evidence by catalog rarity. "cotton" matches thousands of
    # products and separates nothing; a model number matches one. Flat
    # weighting spends the same score on both.
    # Measured NEGATIVE once paired with the better clarification policy:
    # 0.8584 vs 0.8664 mean technical (n=100 x 6 cells). It looked marginally
    # positive against the weaker `formula` policy (0.8219 vs 0.8183), which
    # is the tell -- it was compensating for worse evidence, not adding
    # signal. BM25 already applies IDF when selecting candidates, so
    # re-applying it in the reranker double-counts term rarity the retrieval
    # stage has priced in. Off.
    idf_evidence: bool = False
    # Fuse this turn's ranking with earlier turns'. A product that stays near
    # the top all session is better evidence than one that spikes once on a
    # noisy query.
    # Measured clearly HARMFUL: 0.7732 vs 0.8183 mean technical, MRR 0.5512
    # vs 0.6815. It is a feedback loop -- reinforcing whatever ranked highly on
    # earlier, less-informed turns entrenches exactly the early mistakes later
    # constraints are supposed to correct. Off, and kept only as a documented
    # negative result.
    rank_consensus: bool = False
    # Reserve Top-K slots for the strongest lexical matches. Constraint scoring
    # spans 20-75 points against a fusion range of ~1.6, so a product BM25
    # ranked first can finish outside the Top-10 on an unremarkable constraint
    # total -- 16 of 21 esci misses were exactly that.
    # Measured POSITIVE (full size, 2000 sessions per side): mean technical
    # 0.8370 -> 0.8526 over seven cells, esci x esci 0.8158 -> 0.8614, official
    # +0.0044. No cell regressed. On.
    hard_floor: bool = True
    # 2 is the knee: esci keeps climbing to reserve 7 (0.8747) but the official
    # column falls monotonically past 2 (0.8482 / 0.8427 / 0.8358 / 0.8288 /
    # 0.8221 at 2/3/5/7/9), so more slots buy esci at the scorer's expense.
    hard_floor_reserve: int = 2


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean environment flag, defaulting when unset."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def config_from_env() -> RerankConfig:
    """Build the retriever configuration, letting the environment override defaults.

    Every knob is overridable so an ablation is one command and needs no
    code edit.
    """
    base = RerankConfig()
    return RerankConfig(
        staged=_env_flag("RERANK_STAGED", base.staged),
        prefilter=_env_flag("RERANK_PREFILTER", base.prefilter),
        exclude_negated=_env_flag("RERANK_EXCLUDE_NEGATED", base.exclude_negated),
        soft_abstain=_env_flag("RERANK_SOFT_ABSTAIN", base.soft_abstain),
        department_penalty=_env_flag("RERANK_DEPARTMENT_PENALTY", base.department_penalty),
        department_gate=_env_flag("RERANK_DEPARTMENT_GATE", base.department_gate),
        profile_tiebreak=_env_flag("RERANK_PROFILE_TIEBREAK", base.profile_tiebreak),
        coverage_bonus=_env_flag("RERANK_COVERAGE", base.coverage_bonus),
        idf_evidence=_env_flag("RERANK_IDF", base.idf_evidence),
        rank_consensus=_env_flag("RERANK_CONSENSUS", base.rank_consensus),
        hard_floor=_env_flag("RERANK_HARD_FLOOR", base.hard_floor),
        hard_floor_reserve=int(
            os.environ.get("RERANK_HARD_FLOOR_RESERVE", base.hard_floor_reserve)
        ),
        overfetch=int(os.environ.get("RERANK_OVERFETCH", base.overfetch)),
        min_survivors=int(os.environ.get("RERANK_MIN_SURVIVORS", base.min_survivors)),
    )


# =============================================================================
# RECORDS AND TEXT
# =============================================================================


@dataclass(frozen=True)
class ProductRecord:
    """One catalog product, pre-tokenized for scoring.

    Built once at startup; the normalized text fields exist so per-turn
    scoring never re-tokenizes the same 50k products.
    """
    parent_asin: str
    title: str
    categories: str
    features: str
    details: str
    store: str
    description: str
    price_text: str
    price: float | None
    rating: float
    rating_count: int
    all_text: str
    all_terms: frozenset[str]
    normalized_text: str
    department: str | None
    # Only the keys Role C's clarification policy asks about. `details` is
    # flattened to a string for BM25; recovering values from that string with a
    # regex is not possible -- there is no delimiter between one key's value
    # and the next key's name.
    facets: dict[str, str]


class Contribution(NamedTuple):
    """One additive term: ``coefficient * weights[weight_name]``.

    ``key`` is the diagnostic label (``"material"``, ``"raw:feature:..."``);
    several contributions may share one key.
    """

    key: str
    weight_name: str
    coefficient: float


# The four stages, in the order they apply. ``relevance`` also carries fusion
# and quality; ``soft`` is rescaled by ``soft_scale`` at assembly time, which is
# why it cannot simply be folded into ``relevance``.
STAGES: tuple[str, ...] = ("relevance", "soft", "demoted", "profile")

# Scores zero but keeps the attribute present in ``constraint_details``, so a
# deliberate abstention is distinguishable from an attribute that was never
# considered. Diagnostics are the only way to tell those apart after the fact.
_NEUTRAL = Contribution("", "generic_miss", 0.0)


def _text(value: object) -> str:
    """Flatten any catalog value -- string, list or dict -- into searchable text."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(f"{key} {item}" for key, item in value.items() if item not in (None, "", []))
    if isinstance(value, list):
        return " ".join(str(item) for item in value if item not in (None, ""))
    return str(value)


def _terms(text: str) -> list[str]:
    """Lowercase alphanumeric tokens, stopwords and single characters removed."""
    return [
        token.lower()
        for token in TOKEN_RE.findall(text)
        if len(token) > 1 and token.lower() not in STOPWORDS
    ]


def _normalized_text(text: str) -> str:
    """Whitespace-joined `_terms`, for substring comparison."""
    return " ".join(_terms(text))


def _is_boilerplate(value: object) -> bool:
    """True for ubiquitous listing phrases that carry no discriminative signal."""
    return _normalized_text(str(value)) in BOILERPLATE_PHRASES


def _remove_boilerplate(text: str) -> str:
    """Strip boilerplate phrases from a query so they cannot dominate BM25."""
    result = text
    for phrase in BOILERPLATE_PHRASES:
        result = re.sub(rf"\b{re.escape(phrase)}\b", " ", result, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", result).strip()


def _match_strength(value: object, text: str) -> float:
    """Fraction of the needle's terms present in the haystack, 1.0 for a substring hit."""
    needle = _normalized_text(str(value))
    haystack = _normalized_text(text)
    return _match_strength_normalized(needle, haystack)


def _match_strength_normalized(needle: str, haystack: str) -> float:
    """Match already-normalized strings; avoids repeated catalog tokenization."""
    if not needle:
        return 0.0
    if not haystack:
        return 0.0
    if needle in haystack:
        return 1.0
    needle_terms = needle.split()
    haystack_terms = set(haystack.split())
    matched = sum(1 for term in needle_terms if term in haystack_terms)
    return matched / max(1, len(needle_terms))


def _first_word_match(words: Iterable[str], value: object) -> str | None:
    """First vocabulary word appearing in `value`, else None."""
    text = str(value).lower()
    for word in sorted(words):
        if re.search(rf"\b{re.escape(word)}\b", text):
            return word
    return None


def _safe_float(value: object) -> float | None:
    """Parse a float, returning None rather than raising on catalog junk."""
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: object) -> int:
    """Parse an int, tolerating floats and thousands separators."""
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(str(value).replace(",", "")))
        except (TypeError, ValueError):
            return 0


def _budget_numbers(value: object) -> list[float]:
    """Every number in a budget phrase, in order of appearance."""
    return [float(match) for match in re.findall(r"\d+(?:\.\d{1,2})?", str(value))]


_UNDER_MARKERS = ("under", "below", "less than", "<", "<=", "max")
_AROUND_MARKERS = ("around", "about", "budget")


class BudgetRule(NamedTuple):
    """A parsed budget constraint, or ``None`` fields when unparseable."""

    low: float | None
    high: float | None
    hard: bool
    within: str
    over: str


def _parse_budget(value: object) -> BudgetRule | None:
    """Turn a budget phrase into a numeric range.

    "under $50", "between 20 and 40" and "around $30" have different
    semantics, so the comparison operator is parsed rather than assumed.
    """
    numbers = _budget_numbers(value)
    if not numbers:
        return None
    lowered = str(value).lower()
    amount = max(numbers)
    if any(marker in lowered for marker in _UNDER_MARKERS):
        return BudgetRule(None, amount, True, "budget_within", "budget_over")
    if "between" in lowered and len(numbers) >= 2:
        return BudgetRule(min(numbers), max(numbers), True, "budget_within", "budget_over")
    if any(marker in lowered for marker in _AROUND_MARKERS):
        return BudgetRule(None, amount * 1.25, False, "budget_near", "budget_near_miss")
    return BudgetRule(None, amount * 1.5, False, "budget_loose", "budget_loose_miss")


def _budget_satisfied(rule: BudgetRule, price: float) -> bool:
    """True when a price falls inside a parsed budget range."""
    if rule.low is not None and price < rule.low:
        return False
    return rule.high is None or price <= rule.high


# =============================================================================
# RETRIEVAL PLAN
# =============================================================================


@dataclass(frozen=True)
class TypedConstraint:
    """One constraint with its attribute resolved and its value normalized."""
    attribute: str
    value: object
    soft: bool


@dataclass
class RetrievalPlan:
    """Everything one turn's retrieval needs, derived once per call.

    Separating this from scoring is what lets the prefilter, the hard gate and
    the score all read the *same* interpretation of the conversation instead of
    each re-deriving it from raw dicts.
    """

    query_text: str
    typed: list[TypedConstraint]
    negated: list[str]
    demoted: list[dict]
    department: str | None
    budget: BudgetRule | None
    budget_soft: bool
    brand: object | None
    brand_soft: bool
    profile_terms: frozenset[str]
    profile_rating: float | None
    # Live (non-superseded) spans, pre-normalized with their rarity weight.
    evidence: tuple[tuple[frozenset[str], float], ...] = ()
    # parent_asin -> accumulated reciprocal rank from earlier turns, in [0,1].
    prior_ranks: Mapping[str, float] = MappingProxyType({})


class CatalogRetriever:
    """Keyword retrieval plus staged constraint reranking over the frozen catalog.

    Everything is in-process: an in-memory SQLite FTS5 index for BM25 and
    plain Python scoring on top. No vector database, per the competition
    scope.
    """
    def __init__(
        self,
        catalog_path: str | Path = "data/catalog.jsonl",
        candidate_limit: int = 500,
        config: RerankConfig | None = None,
        weights: RerankWeights | None = None,
    ) -> None:
        """Load the catalog and build the in-memory search index.

        Expensive and done once; a session reuses the same instance.
        """
        self.catalog_path = Path(catalog_path)
        self.candidate_limit = candidate_limit
        self.config = config if config is not None else config_from_env()
        self.weights = weights if weights is not None else weights_from_env()
        # The narrow demoted-span route is the validated override fix. The
        # broader boilerplate filter remains opt-in because it regressed the
        # public score in its first full-corpus test.
        self.enable_boilerplate_filter = os.environ.get("RERANK_FILTER_BOILERPLATE", "").lower() in {
            "1", "true", "yes"
        }
        self.enable_raw_phrase_scoring = os.environ.get("RERANK_RAW_PHRASES", "1").lower() in {
            "1", "true", "yes"
        }
        self.connection = sqlite3.connect(":memory:")
        self.products: dict[str, ProductRecord] = {}
        self.valid_ids: set[str] = set()
        self.popular_ids: list[str] = []
        self.last_diagnostics: dict[str, object] = {}
        self._build()

    # -------------------------------------------------------------- public

    def retrieve_and_rerank(
        self,
        search_query: str,
        constraints: dict,
        no_preference: Iterable[str] = (),
        top_k: int = 10,
        raw_constraints: Iterable[dict] = (),
        user_profile: dict | None = None,
        prior_ranks: Mapping[str, float] | None = None,
    ) -> list[str]:
        """Return up to `top_k` catalog ids for the current dialogue state.

        The pipeline is: build a query, take a BM25 candidate pool, then rerank
        in stages so hard-constraint coverage cannot be traded away by lexical
        relevance.
        """
        plan = self.build_plan(
            search_query, constraints, no_preference, raw_constraints, user_profile,
            prior_ranks,
        )
        candidate_scores = self.score_pool(self._candidate_pool(plan), plan)

        staged = self.config.staged
        ranked = sorted(
            candidate_scores.items(),
            key=lambda item: (
                -int(item[1]["violations"]) if staged else 0,
                float(item[1]["final_score"]),
            ),
            reverse=True,
        )
        ranked_ids = [parent_asin for parent_asin, _ in ranked]
        self.last_diagnostics = {
            "query_text": plan.query_text,
            "candidate_count": len(candidate_scores),
            "bm25_candidate_ids": list(self._last_bm25_ids),
            "candidate_ids": ranked_ids,
            "department": plan.department,
            "negated": list(plan.negated),
            # The attributes a coverage metric should hold the Top-10 to.
            # Soft (Tier 1 gazetteer) constraints are excluded: they are
            # guesses, and scoring the list against a guess measures the
            # extractor, not the ranker.
            "hard_attributes": [c.attribute for c in plan.typed if not c.soft],
            "prefilter_removed": self._last_prefilter_removed,
            # Retained in memory for local inspection. The trace deliberately
            # does not serialize this 500-product mapping every turn; the
            # offline analyzer recomputes just the three relevant products.
            "candidate_scores": candidate_scores,
            "attribute_stats": self._attribute_stats(ranked_ids, plan),
        }
        if self.config.hard_floor:
            ranked_ids = self._apply_hard_floor(ranked_ids, top_k)
        return self._sanitize(ranked_ids, top_k)

    def _apply_hard_floor(self, ranked_ids: list[str], top_k: int) -> list[str]:
        """Give the best BM25 candidates a place in the returned Top-K.

        Promoted candidates displace the *tail* of the Top-K, so a ranking the
        reranker is confident about keeps its leading positions.
        """
        reserve = self.config.hard_floor_reserve
        if reserve <= 0 or top_k <= reserve:
            return ranked_ids
        head = ranked_ids[:top_k]
        promote = [
            parent_asin
            for parent_asin in self._last_bm25_ids[:reserve]
            if parent_asin not in head
        ]
        if not promote:
            return ranked_ids
        kept = head[: top_k - len(promote)]
        keep_set = set(kept) | set(promote)
        return kept + promote + [
            parent_asin for parent_asin in ranked_ids if parent_asin not in keep_set
        ]

    def build_plan(
        self,
        search_query: str,
        constraints: dict,
        no_preference: Iterable[str] = (),
        raw_constraints: Iterable[dict] = (),
        user_profile: dict | None = None,
        prior_ranks: Mapping[str, float] | None = None,
    ) -> RetrievalPlan:
        """Interpret one turn of conversation state into a retrieval plan."""
        no_preference_set = {str(item) for item in no_preference}
        spans = [dict(span) for span in raw_constraints if isinstance(span, dict)]
        spans = [
            span for span in spans
            if str(span.get("attribute", "")) not in no_preference_set
        ]

        negated = self._negated_phrases(spans) if self.config.exclude_negated else []
        demoted = [
            span for span in spans
            if 0.0 < float(span.get("weight", 1.0) or 0.0) < 1.0
        ]
        soft_attributes = self._soft_attributes(spans)

        active = {
            str(key): value
            for key, value in constraints.items()
            if key not in no_preference_set and value not in (None, "", [])
        }

        typed = [
            TypedConstraint(attribute, value, attribute in soft_attributes)
            for attribute, value in active.items()
        ]

        query_text = self._query_text(search_query, active, negated)
        department = (
            _infer_department(query_text)
            if (self.config.department_gate or self.config.department_penalty)
            else None
        )

        budget_value = active.get("budget")
        brand_value = active.get("brand")

        profile_terms: frozenset[str] = frozenset()
        profile_rating: float | None = None
        if user_profile and self.config.profile_tiebreak:
            tags = user_profile.get("preference_tags") or []
            profile_terms = frozenset(
                term
                for tag in tags
                for term in _terms(str(tag))
            )
            profile_rating = _safe_float(user_profile.get("average_prior_rating"))

        # Live spans, each paired with its catalog rarity. Superseded and
        # negated spans are excluded: the first is stale, the second is
        # something the shopper rejected.
        evidence: list[tuple[frozenset[str], float]] = []
        if self.config.coverage_bonus or self.config.idf_evidence:
            seen_phrases: set[str] = set()
            for span in spans:
                if span.get("superseded") or str(span.get("polarity", "must")) == "negate":
                    continue
                phrase = _normalized_text(str(span.get("match_phrase") or span.get("text") or ""))
                if not phrase or phrase in seen_phrases or _is_boilerplate(phrase):
                    continue
                seen_phrases.add(phrase)
                terms = frozenset(phrase.split())
                if terms:
                    evidence.append((terms, self.term_rarity(terms)))

        return RetrievalPlan(
            evidence=tuple(evidence),
            prior_ranks=prior_ranks or MappingProxyType({}),
            query_text=query_text,
            typed=typed,
            negated=negated,
            demoted=demoted,
            department=department,
            budget=_parse_budget(budget_value) if budget_value is not None else None,
            budget_soft="budget" in soft_attributes,
            brand=brand_value,
            brand_soft="brand" in soft_attributes,
            profile_terms=profile_terms,
            profile_rating=profile_rating,
        )

    def score_pool(
        self,
        pool: dict[str, Contribution],
        plan: RetrievalPlan,
        weights: RerankWeights | None = None,
    ) -> dict[str, dict[str, object]]:
        """Score a candidate pool into ranking records.

        ``pool`` maps ``parent_asin`` to its candidate-generation contribution
        (reciprocal-rank fusion, or the popularity backfill), so the fusion
        magnitude is a calibratable weight like every other.
        """
        active = weights if weights is not None else self.weights
        mapping = active.as_mapping()
        scored: dict[str, dict[str, object]] = {}
        for parent_asin, generation in pool.items():
            record = self.products[parent_asin]
            stages = self.stage_contributions(record, plan)
            stages["relevance"] = [
                generation,
                *self._consensus_contributions(parent_asin, plan),
                *stages["relevance"],
            ]
            violations = self._violation_cache.get(parent_asin)
            if violations is None:
                violations = self.violations(record, plan)
            scored[parent_asin] = self.assemble(stages, violations, mapping)
            scored[parent_asin]["bm25_rank"] = self._bm25_rank.get(parent_asin)
        return scored

    @staticmethod
    def assemble(
        stages: dict[str, list[Contribution]],
        violations: int,
        mapping: dict[str, float],
    ) -> dict[str, object]:
        """Fold staged contributions into one ranking record.

        Stage 1 (``violations``) stays out of the score entirely -- it is the
        first element of the sort key, so no weight can trade against it.
        Stages 2-4 are summed with descending influence.
        """
        details: dict[str, float] = {}
        totals: dict[str, float] = {}
        for stage in STAGES:
            subtotal = 0.0
            for key, weight_name, coefficient in stages.get(stage, ()):
                value = coefficient * mapping[weight_name]
                subtotal += value
                details[key] = details.get(key, 0.0) + value
            totals[stage] = subtotal
        final = (
            totals["relevance"]
            + mapping["soft_scale"] * totals["soft"]
            + totals["demoted"]
            + totals["profile"]
        )
        fusion = details.get("_generation", 0.0)
        quality = details.get("_quality", 0.0)
        return {
            "violations": violations,
            "stage_scores": totals,
            "constraint_score": final - fusion - quality,
            "constraint_details": {
                key: value for key, value in details.items() if not key.startswith("_")
            },
            "fusion_score": fusion,
            "quality_score": quality,
            "final_score": final,
        }

    def stage_contributions(
        self, record: ProductRecord, plan: RetrievalPlan
    ) -> dict[str, list[Contribution]]:
        """Every additive term for one product, bucketed by stage.

        Emitting coefficients rather than numbers is what makes calibration a
        dot product over the production code path instead of a reimplementation
        that can drift away from what actually serves traffic.
        """
        relevance: list[Contribution] = [
            Contribution("_quality", "rating_coefficient", max(0.0, record.rating - 3.5)),
            Contribution(
                "_quality", "popularity_scale", min(2.0, math.log1p(record.rating_count) / 6.0)
            ),
        ]
        soft: list[Contribution] = []

        if self.config.department_penalty and plan.department and record.department:
            relevance.append(Contribution(
                "department",
                "department_miss" if _department_conflicts(plan.department, record.department)
                else "department_match",
                1.0,
            ))

        for constraint in plan.typed:
            attribute = constraint.attribute
            abstain = constraint.soft and self.config.soft_abstain
            if attribute == "category":
                terms = self._category_contributions(record, constraint.value, abstain)
            elif attribute == "brand":
                terms = self._brand_contributions(record, constraint.value, abstain)
            elif attribute == "budget":
                terms = self._budget_contributions(record, plan.budget, abstain)
            elif attribute in _ATTRIBUTE_BOOST:
                boost = _ATTRIBUTE_BOOST[attribute]
                vocabulary = MATERIALS if attribute == "material" else (
                    COLORS if attribute == "color" else None
                )
                if vocabulary is not None:
                    terms = self._vocabulary_contributions(
                        record, constraint.value, vocabulary, boost, abstain
                    )
                else:
                    terms = self._generic_contributions(record, constraint.value, boost, abstain)
            else:
                continue
            bucket = soft if constraint.soft else relevance
            bucket.extend(
                Contribution(attribute, weight_name, coefficient)
                for _, weight_name, coefficient in terms
            )

        relevance.extend(self._evidence_contributions(record, plan))

        demoted = self._demoted_contributions(record, plan) if self.enable_raw_phrase_scoring else []
        return {
            "relevance": relevance,
            "soft": soft,
            "demoted": demoted,
            "profile": self._profile_contributions(record, plan),
        }

    def violations(self, record: ProductRecord, plan: RetrievalPlan) -> int:
        """Count determinate hard-constraint violations.

        Three-valued by construction: a predicate contributes only when the
        product carries the field *and* it contradicts the request. Absent
        fields fall through to the soft penalty inside the score, which is the
        only safe reading of a catalog where price is present on 21% of rows.

        Always computed. ``RerankConfig.staged`` decides whether the count
        *keys the sort*; the prefilter uses it either way, so no combination of
        switches silently turns into a no-op.
        """
        count = 0

        if plan.budget is not None and not plan.budget_soft and plan.budget.hard:
            if record.price is not None and not _budget_satisfied(plan.budget, record.price):
                count += 1

        if self.config.department_gate and _department_conflicts(
            plan.department, record.department
        ):
            count += 1

        if plan.brand is not None and not plan.brand_soft and record.store:
            brand = _normalized_text(str(plan.brand))
            if brand and _match_strength_normalized(brand, record.normalized_text) <= 0.0:
                count += 1

        for phrase in plan.negated:
            if _match_strength_normalized(phrase, record.normalized_text) >= 1.0:
                count += 1

        return count

    # ------------------------------------------------------- contributions

    def _category_contributions(
        self, record: ProductRecord, value: object, soft: bool
    ) -> list[Contribution]:
        """Score a candidate against the requested product category."""
        strength = _match_strength(value, f"{record.categories} {record.title} {record.details}")
        if strength >= 0.8:
            return [Contribution("category", "category_exact", 1.0)]
        if strength >= 0.5:
            return [Contribution("category", "category_partial", 1.0)]
        if strength > 0:
            return [Contribution("category", "category_weak", 1.0)]
        if soft:
            return [_NEUTRAL]
        return [Contribution("category", "category_miss", 1.0)]

    def _brand_contributions(
        self, record: ProductRecord, value: object, soft: bool
    ) -> list[Contribution]:
        """Score a candidate against a requested brand.

        A brand mismatch is the harshest penalty in the reranker: brand is the
        one attribute a shopper is almost never flexible about.
        """
        store_strength = _match_strength(value, record.store)
        full_strength = _match_strength(value, f"{record.store} {record.title} {record.details}")
        if store_strength >= 0.8:
            return [Contribution("brand", "brand_store", 1.0)]
        if full_strength >= 0.8:
            return [Contribution("brand", "brand_text", 1.0)]
        if full_strength > 0:
            return [Contribution("brand", "brand_weak", 1.0)]
        if soft:
            return [_NEUTRAL]
        return [Contribution("brand", "brand_miss", 1.0)]

    def _vocabulary_contributions(
        self,
        record: ProductRecord,
        value: object,
        vocabulary: Iterable[str],
        boost: str,
        soft: bool,
    ) -> list[Contribution]:
        """Score a closed-vocabulary attribute such as colour or material.

        A known vocabulary word that is absent from the listing is evidence of a
        mismatch, not merely absence of evidence.
        """
        explicit_word = _first_word_match(vocabulary, value)
        if explicit_word:
            if re.search(rf"\b{re.escape(explicit_word)}\b", record.all_text, flags=re.IGNORECASE):
                return [Contribution("", boost, 1.0)]
            if soft:
                return [_NEUTRAL]
            return [Contribution("", "vocabulary_miss", 1.0)]
        return self._generic_contributions(record, value, boost, soft)

    def _generic_contributions(
        self, record: ProductRecord, value: object, boost: str, soft: bool
    ) -> list[Contribution]:
        """Score a free-text attribute by term overlap with the listing."""
        if self.enable_boilerplate_filter and _is_boilerplate(value):
            return [_NEUTRAL]
        strength = _match_strength(value, record.all_text)
        if strength >= 0.8:
            return [Contribution("", boost, 1.0)]
        if strength >= 0.5:
            return [Contribution("", boost, GENERIC_MID_FRACTION)]
        if strength > 0:
            return [Contribution("", boost, GENERIC_LOW_FRACTION)]
        if soft:
            return [_NEUTRAL]
        return [Contribution("", "generic_miss", 1.0)]

    def _budget_contributions(
        self, record: ProductRecord, rule: BudgetRule | None, soft: bool
    ) -> list[Contribution]:
        """Score a candidate's price against the requested budget range."""
        if rule is None:
            return []
        if record.price is None:
            if soft:
                return [_NEUTRAL]
            return [Contribution("", "budget_unpriced", 1.0)]
        if _budget_satisfied(rule, record.price):
            return [Contribution("", rule.within, 1.0)]
        if soft:
            return [_NEUTRAL]
        return [Contribution("", rule.over, 1.0)]

    def _demoted_contributions(
        self, record: ProductRecord, plan: RetrievalPlan
    ) -> list[Contribution]:
        """Score superseded spans at their demoted weight, and nothing else.

        An overridden phrase is retained rather than deleted because it can
        still separate the target when the replacement is broad. Scoring
        *ordinary* historic spans, by contrast, regressed the public score --
        that is why the weight window is exclusive at both ends.
        """
        active_phrases = {
            _normalized_text(str(constraint.value)) for constraint in plan.typed
        }
        out: list[Contribution] = []
        for span in plan.demoted:
            phrase = str(span.get("match_phrase") or span.get("text") or "")
            normalized = _normalized_text(phrase)
            if (
                len(normalized.split()) < 2
                or normalized in active_phrases
                or _is_boilerplate(normalized)
            ):
                continue
            try:
                weight = float(span.get("weight", 1.0))
            except (TypeError, ValueError):
                continue
            weight = min(weight, 1.0)
            strength = _match_strength_normalized(normalized, record.normalized_text)
            key = f"raw:{span.get('attribute', 'feature')}:{normalized[:48]}"
            if strength >= 1.0:
                out.append(Contribution(key, "demoted_exact", weight))
            elif strength >= 0.8:
                out.append(Contribution(key, "demoted_partial", weight))
        return out

    def _evidence_contributions(
        self, record: ProductRecord, plan: RetrievalPlan
    ) -> list[Contribution]:
        """Breadth of constraint satisfaction, and rarity of what matched.

        Two separate ideas sharing one pass over the live spans:

        `coverage` is the fraction of distinct disclosed spans this product
        satisfies. Scoring is otherwise purely additive, so one large category
        match can outrank a product that quietly satisfies four constraints --
        yet the target is exactly the product that satisfies all of them.

        `idf_evidence` weights each satisfied span by how rare its terms are in
        the catalog. Matching "cotton" separates a product from almost nothing;
        matching a model number separates it from everything.
        """
        if not plan.evidence:
            return []

        matched = 0
        rarity_total = 0.0
        rarity_available = 0.0
        for terms, rarity in plan.evidence:
            rarity_available += rarity
            overlap = len(terms & record.all_terms) / len(terms)
            if overlap >= 0.8:
                matched += 1
                rarity_total += rarity * overlap

        out: list[Contribution] = []
        if self.config.coverage_bonus:
            out.append(Contribution(
                "coverage", "coverage_scale", matched / len(plan.evidence)
            ))
        if self.config.idf_evidence and rarity_available > 0.0:
            out.append(Contribution(
                "idf_evidence", "idf_evidence_scale", rarity_total / rarity_available
            ))
        return out

    def _consensus_contributions(
        self, parent_asin: str, plan: RetrievalPlan
    ) -> list[Contribution]:
        """Reciprocal-rank fusion across the session's earlier turns.

        Fusion already runs across channels within a turn; this runs it across
        turns. A product held near the top by several different constraint
        sets is stronger evidence than one lifted once by a noisy query, and
        the evaluator records the FIRST turn the target surfaces -- so
        stabilising rank-1 early is worth more than improving it late.
        """
        if not self.config.rank_consensus or not plan.prior_ranks:
            return []
        score = float(plan.prior_ranks.get(parent_asin, 0.0))
        if score <= 0.0:
            return []
        return [Contribution("consensus", "consensus_scale", min(1.0, score))]

    def _profile_contributions(
        self, record: ProductRecord, plan: RetrievalPlan
    ) -> list[Contribution]:
        """Stage 4. Bounded to [0, 1] before ``profile_scale``.

        `user_profile` describes the shopper's history, not this request. It may
        only separate candidates the explicit constraints could not, so its
        magnitude has to stay below the granularity of stage 2 -- hence a
        normalized coefficient and a small scale rather than a raw bonus.
        """
        if not self.config.profile_tiebreak:
            return []
        if not plan.profile_terms and plan.profile_rating is None:
            return []
        coefficient = 0.0
        if plan.profile_terms:
            overlap = len(plan.profile_terms & record.all_terms) / len(plan.profile_terms)
            coefficient += 0.5 * overlap
        if plan.profile_rating is not None and record.rating > 0:
            # Shoppers who rate generously tolerate a wider quality band; the
            # term is a similarity, not a "higher is better" bonus.
            closeness = max(0.0, 1.0 - abs(record.rating - plan.profile_rating) / 5.0)
            coefficient += 0.5 * closeness
        if coefficient <= 0.0:
            return []
        return [Contribution("profile", "profile_scale", coefficient)]

    # ---------------------------------------------------- candidate supply

    def _candidate_pool(self, plan: RetrievalPlan) -> dict[str, Contribution]:
        """BM25 -> prefilter -> reciprocal-rank fusion contributions."""
        self._violation_cache = {}
        fetch = self.candidate_limit * max(1, self.config.overfetch) if self.config.prefilter \
            else self.candidate_limit
        bm25_ids = self._bm25_search(plan.query_text, limit=fetch)
        kept, removed = self._prefilter(bm25_ids, plan)
        self._last_bm25_ids = kept
        self._last_prefilter_removed = removed
        self._bm25_rank = {}

        pool: dict[str, Contribution] = {}
        for rank, parent_asin in enumerate(kept, start=1):
            self._bm25_rank[parent_asin] = rank
            pool[parent_asin] = Contribution("_generation", "fusion_scale", 1.0 / (60.0 + rank))

        self._add_popular_backfill(pool)
        return pool

    def _prefilter(self, bm25_ids: list[str], plan: RetrievalPlan) -> tuple[list[str], int]:
        """Drop determinate violators, then truncate to the candidate budget.

        Filtering before truncation is the point: the budget is spent on
        products that can still win. The floor guarantees the pool never
        collapses -- if too few survive, the unfiltered order is restored and
        the hard gate alone handles the violators.
        """
        if not self.config.prefilter or not bm25_ids:
            return bm25_ids[: self.candidate_limit], 0
        # Cached so `score_pool` does not recompute the same predicates for
        # every survivor: the prefilter already evaluated up to
        # `candidate_limit * overfetch` products this turn.
        counts = self._violation_cache
        for parent_asin in bm25_ids:
            counts[parent_asin] = self.violations(self.products[parent_asin], plan)
        kept = [parent_asin for parent_asin in bm25_ids if counts[parent_asin] == 0]
        removed = len(bm25_ids) - len(kept)
        if len(kept) < min(self.config.min_survivors, len(bm25_ids)):
            return bm25_ids[: self.candidate_limit], 0
        return kept[: self.candidate_limit], removed

    def _bm25_search(self, query_text: str, limit: int | None = None) -> list[str]:
        """Run one field-weighted BM25 query and return candidate ids, best first."""
        terms = list(dict.fromkeys(_terms(query_text)))[:MAX_QUERY_TERMS]
        if not terms:
            return []
        expression = " OR ".join(f'"{term}"' for term in terms)
        weights = ", ".join(str(weight) for weight in FIELD_WEIGHTS)
        rows = self.connection.execute(
            f"SELECT parent_asin FROM products WHERE products MATCH ? "
            f"ORDER BY bm25(products, {weights}) LIMIT ?",
            (expression, limit if limit is not None else self.candidate_limit),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def _add_popular_backfill(self, pool: dict[str, Contribution]) -> None:
        """Top up a thin candidate pool with popular products.

        Guarantees the agent always has something to show; a weak suggestion
        scores better than an empty slate.
        """
        needed = max(0, min(self.candidate_limit, 50) - len(pool))
        if needed == 0:
            return
        added = 0
        for rank, parent_asin in enumerate(self.popular_ids, start=1):
            if parent_asin in pool:
                continue
            pool[parent_asin] = Contribution("_generation", "backfill_scale", 1.0 / rank)
            added += 1
            if added >= needed:
                break

    # ------------------------------------------------------------- helpers

    def _negated_phrases(self, spans: Iterable[dict]) -> list[str]:
        """Phrases the shopper ruled out, normalized and deduplicated.

        These are excluded and stripped from the query, never penalized:
        leaving a rejected term in the BM25 query spends candidate budget
        retrieving exactly what the shopper does not want.
        """
        out: list[str] = []
        seen: set[str] = set()
        for span in spans:
            if str(span.get("polarity", "must")) != "negate":
                continue
            if span.get("superseded"):
                continue
            phrase = _normalized_text(str(span.get("match_phrase") or span.get("text") or ""))
            if not phrase or phrase in seen or _is_boilerplate(phrase):
                continue
            seen.add(phrase)
            out.append(phrase)
        return out

    def _soft_attributes(self, spans: Iterable[dict]) -> set[str]:
        """Attributes whose every live span is a gazetteer guess, not a recital.

        Tier 1 tags catalog vocabulary it finds in free text, so its values are
        often right in spirit and wrong in wording ("bras" against a listing
        filed under "Lingerie Accessories"). Penalizing a wrong guess puts the
        penalty on the true target; abstaining costs nothing.
        """
        hard: set[str] = set()
        soft: set[str] = set()
        for span in spans:
            if span.get("superseded"):
                continue
            attribute = str(span.get("attribute") or "")
            if not attribute:
                continue
            if str(span.get("strength", "hard")) == "soft":
                soft.add(attribute)
            else:
                hard.add(attribute)
        return soft - hard

    def _query_text(self, search_query: str, constraints: dict, negated: Iterable[str]) -> str:
        """Assemble the BM25 query from the accumulated dialogue state."""
        clean = _remove_boilerplate if self.enable_boilerplate_filter else str
        parts = [clean(search_query)]
        parts.extend(
            clean(str(value))
            for value in constraints.values()
            if value not in (None, "", [])
            and (not self.enable_boilerplate_filter or not _is_boilerplate(value))
        )
        text = " ".join(part for part in parts if part).strip()
        return self._strip_negated(text, negated)

    def _strip_negated(self, text: str, negated: Iterable[str]) -> str:
        """Remove each rejected phrase from the query, and nothing more.

        The constituent-token loop this replaces deleted every word of a
        multi-word phrase from the whole query: negating "underwire bra" also
        removed "bra", the category the shopper actually wants. A single-word
        phrase is unchanged by the fix; a multi-word one now loses only the
        contiguous phrase, so the surrounding request survives.
        """
        for phrase in negated:
            if not phrase:
                continue
            pattern = r"\s+".join(re.escape(term) for term in phrase.split())
            if not pattern:
                continue
            text = re.sub(rf"\b{pattern}\b", " ", text, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", text).strip()

    def _attribute_stats(
        self, ranked_ids: list[str], plan: RetrievalPlan
    ) -> dict[str, dict[str, object]]:
        """Per-attribute coverage over the provisional pool, for clarification.

        Requested by Role C for ``state.clarification.question_value``: it needs
        to know which attribute actually splits the live candidates before
        spending a turn asking about it.
        """
        sample = ranked_ids[:200]
        if not sample:
            return {}
        stats: dict[str, dict[str, object]] = {}
        # `category` and `budget` are derived rather than structured, but they
        # are askable, so omitting them made their reduction and coverage terms
        # structurally zero -- the clarification formula could never choose them
        # however well they would split the pool.
        for attribute, key in (("color", "Color"), ("material", "Material"), ("size", "Size"),
                               ("style", "Style"), ("brand", "store"),
                               ("category", "_category"), ("budget", "_price_band")):
            counts: dict[str, int] = {}
            present = 0
            for parent_asin in sample:
                record = self.products.get(parent_asin)
                if record is None:
                    continue
                value = self._structured_value(record, key)
                if not value:
                    continue
                present += 1
                counts[value] = counts.get(value, 0) + 1
            if not present:
                stats[attribute] = {"coverage": 0.0, "value_counts": {}, "instability": 0.0}
                continue
            total = float(present)
            entropy = -sum(
                (count / total) * math.log(count / total, 2)
                for count in counts.values()
                if count
            )
            stats[attribute] = {
                "coverage": round(present / len(sample), 4),
                "value_counts": dict(sorted(counts.items(), key=lambda kv: -kv[1])[:12]),
                "instability": max(0.0, round(entropy, 4)),
            }
        return stats

    # Price bands for the `budget` split, in dollars. Coarse on purpose: the
    # question is "would asking about budget divide these candidates", not
    # "what exactly does each cost".
    _PRICE_BANDS = (15.0, 25.0, 40.0, 60.0, 100.0, 200.0)

    def _structured_value(self, record: ProductRecord, key: str) -> str:
        """Read an attribute from a product's structured `details`, if present."""
        if key == "store":
            return record.store.strip().lower()[:40]
        if key == "_category":
            # Last comma-separated leaf: the most specific label the catalog
            # gives, and the one a category question would actually resolve.
            leaves = [part.strip().lower() for part in record.categories.split(",") if part.strip()]
            return leaves[-1][:40] if leaves else ""
        if key == "_price_band":
            if record.price is None:
                return ""
            for index, ceiling in enumerate(self._PRICE_BANDS):
                if record.price < ceiling:
                    return f"band{index}"
            return f"band{len(self._PRICE_BANDS)}"
        return record.facets.get(key, "")

    # --------------------------------------------------------------- build

    def _build(self) -> None:
        """Load every catalog row into the FTS5 index and the record table."""
        self._last_bm25_ids: list[str] = []
        self._last_prefilter_removed = 0
        self._bm25_rank: dict[str, int] = {}
        self._violation_cache: dict[str, int] = {}
        cursor = self.connection.cursor()
        cursor.execute(
            "CREATE VIRTUAL TABLE products USING fts5("
            "parent_asin UNINDEXED, title, categories, features, details, store, description, price_text, "
            "tokenize='unicode61 remove_diacritics 2')"
        )
        batch: list[tuple[str, str, str, str, str, str, str, str]] = []
        for product in self._iter_catalog():
            record = self._record(product)
            self.products[record.parent_asin] = record
            self.valid_ids.add(record.parent_asin)
            batch.append(
                (
                    record.parent_asin,
                    record.title,
                    record.categories,
                    record.features,
                    record.details,
                    record.store,
                    record.description,
                    record.price_text,
                )
            )
            if len(batch) >= 1000:
                cursor.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?)", batch)
                batch.clear()
        if batch:
            cursor.executemany("INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?)", batch)
        self.connection.commit()
        self.popular_ids = sorted(
            self.products,
            key=lambda parent_asin: self._quality_score(self.products[parent_asin]),
            reverse=True,
        )
        self._build_idf()

    def _build_idf(self) -> None:
        """Document frequency per term, for rarity-weighted span evidence.

        One pass over `all_terms`, which is already materialized per record.
        A term appearing in every product carries no information about which
        product the shopper means; a model number appearing in one carries
        almost all of it. Flat span weighting spends the same score on both.
        """
        document_frequency: Counter[str] = Counter()
        for record in self.products.values():
            document_frequency.update(record.all_terms)
        total = max(1, len(self.products))
        self._idf = {
            term: math.log(total / count)
            for term, count in document_frequency.items()
        }
        # Normalizer: the rarest possible term scores log(N/1).
        self._idf_ceiling = math.log(total) or 1.0

    def term_rarity(self, terms: Iterable[str]) -> float:
        """Mean IDF of a span, normalized to [0, 1]."""
        values = [self._idf.get(term, self._idf_ceiling) for term in terms]
        if not values:
            return 0.0
        return min(1.0, (sum(values) / len(values)) / self._idf_ceiling)

    def _iter_catalog(self) -> Iterable[dict]:
        """Stream catalog rows, transparently handling a gzipped file."""
        if not self.catalog_path.exists():
            raise FileNotFoundError(f"Catalog not found: {self.catalog_path}")
        opener = gzip.open if self.catalog_path.suffix == ".gz" else open
        with opener(self.catalog_path, mode="rt", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)

    def _record(self, product: dict) -> ProductRecord:
        """Convert one raw catalog row into a scoring-ready `ProductRecord`."""
        price = _safe_float(product.get("price"))
        rating = _safe_float(product.get("average_rating")) or 0.0
        rating_count = _safe_int(product.get("rating_number"))
        price_text = "" if price is None else f"price {price} budget ${price}"
        title = _text(product.get("title"))
        categories = _text(product.get("categories"))
        features = _text(product.get("features"))
        raw_details = product.get("details")
        details = _text(raw_details)
        store = _text(product.get("store"))
        description = _text(product.get("description"))
        all_text = " ".join(
            part
            for part in (title, categories, features, details, store, description, price_text)
            if part
        )
        department = None
        facets: dict[str, str] = {}
        if isinstance(raw_details, dict):
            department = _canonical_department(raw_details.get("Department"))
            for key in FACET_KEYS:
                value = raw_details.get(key)
                if isinstance(value, str) and value.strip():
                    facets[key] = value.strip().lower()[:40]
        return ProductRecord(
            parent_asin=str(product["parent_asin"]),
            title=title,
            categories=categories,
            features=features,
            details=details,
            store=store,
            description=description,
            price_text=price_text,
            price=price,
            rating=rating,
            rating_count=rating_count,
            all_text=all_text,
            all_terms=frozenset(_terms(all_text)),
            normalized_text=_normalized_text(all_text),
            department=department,
            facets=facets,
        )

    def _quality_score(self, record: ProductRecord) -> float:
        """Small rating and popularity bonus, used only to break ties."""
        rating_bonus = max(0.0, record.rating - 3.5) * self.weights.rating_coefficient
        popularity_bonus = (
            min(2.0, math.log1p(record.rating_count) / 6.0) * self.weights.popularity_scale
        )
        return rating_bonus + popularity_bonus

    def _sanitize(self, ranked_ids: Iterable[str], top_k: int) -> list[str]:
        """Return the first `top_k` ids that are unique and present in the catalog.

        The evaluator scores only valid unique ids, so this is the last gate
        before a slate leaves the retriever.
        """
        result: list[str] = []
        seen: set[str] = set()
        for parent_asin in ranked_ids:
            if parent_asin not in self.valid_ids or parent_asin in seen:
                continue
            result.append(parent_asin)
            seen.add(parent_asin)
            if len(result) >= top_k:
                break
        return result

    # ---------------------------------------------------------- debugging

    def explain_candidate(
        self,
        parent_asin: str,
        constraints: dict,
        bm25_rank: int | None,
        raw_constraints: Iterable[dict] = (),
    ) -> dict[str, object] | None:
        """Return deterministic score components for offline debugging only."""
        record = self.products.get(parent_asin)
        if record is None:
            return None
        plan = self.build_plan("", constraints, (), raw_constraints, None)
        generation = Contribution(
            "_generation", "fusion_scale", 1.0 / (60.0 + bm25_rank) if bm25_rank else 0.0
        )
        scored = self.score_pool({parent_asin: generation}, plan)[parent_asin]
        return {
            "bm25_rank": bm25_rank,
            "bm25_fusion": scored["fusion_score"],
            "violations": scored["violations"],
            "constraint_total": scored["constraint_score"],
            "constraint_details": scored["constraint_details"],
            "quality": scored["quality_score"],
            "final": scored["final_score"],
        }
