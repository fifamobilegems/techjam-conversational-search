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
import os
import re
import sqlite3
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import Iterable, NamedTuple


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

    # --- stage 4: profile tie-break ---
    profile_scale: float = 0.0

    def as_mapping(self) -> dict[str, float]:
        return {item.name: float(getattr(self, item.name)) for item in fields(self)}

    def with_values(self, values: dict[str, float]) -> "RerankWeights":
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
# NOT the default. `docs/ARCHITECTURE.md` names the ~0.89 official column "the
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
)

WEIGHT_PRESETS: dict[str, RerankWeights] = {
    "default": RerankWeights(),
    "calibrated": CALIBRATED_WEIGHTS,
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


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def config_from_env() -> RerankConfig:
    base = RerankConfig()
    return RerankConfig(
        staged=_env_flag("RERANK_STAGED", base.staged),
        prefilter=_env_flag("RERANK_PREFILTER", base.prefilter),
        exclude_negated=_env_flag("RERANK_EXCLUDE_NEGATED", base.exclude_negated),
        soft_abstain=_env_flag("RERANK_SOFT_ABSTAIN", base.soft_abstain),
        department_penalty=_env_flag("RERANK_DEPARTMENT_PENALTY", base.department_penalty),
        department_gate=_env_flag("RERANK_DEPARTMENT_GATE", base.department_gate),
        profile_tiebreak=_env_flag("RERANK_PROFILE_TIEBREAK", base.profile_tiebreak),
        overfetch=int(os.environ.get("RERANK_OVERFETCH", base.overfetch)),
        min_survivors=int(os.environ.get("RERANK_MIN_SURVIVORS", base.min_survivors)),
    )


# =============================================================================
# RECORDS AND TEXT
# =============================================================================


@dataclass(frozen=True)
class ProductRecord:
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
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(f"{key} {item}" for key, item in value.items() if item not in (None, "", []))
    if isinstance(value, list):
        return " ".join(str(item) for item in value if item not in (None, ""))
    return str(value)


def _terms(text: str) -> list[str]:
    return [
        token.lower()
        for token in TOKEN_RE.findall(text)
        if len(token) > 1 and token.lower() not in STOPWORDS
    ]


def _normalized_text(text: str) -> str:
    return " ".join(_terms(text))


def _is_boilerplate(value: object) -> bool:
    return _normalized_text(str(value)) in BOILERPLATE_PHRASES


def _remove_boilerplate(text: str) -> str:
    result = text
    for phrase in BOILERPLATE_PHRASES:
        result = re.sub(rf"\b{re.escape(phrase)}\b", " ", result, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", result).strip()


def _match_strength(value: object, text: str) -> float:
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
    text = str(value).lower()
    for word in sorted(words):
        if re.search(rf"\b{re.escape(word)}\b", text):
            return word
    return None


def _safe_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: object) -> int:
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
    if rule.low is not None and price < rule.low:
        return False
    return rule.high is None or price <= rule.high


# =============================================================================
# RETRIEVAL PLAN
# =============================================================================


@dataclass(frozen=True)
class TypedConstraint:
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


class CatalogRetriever:
    def __init__(
        self,
        catalog_path: str | Path = "data/catalog.jsonl",
        candidate_limit: int = 500,
        config: RerankConfig | None = None,
        weights: RerankWeights | None = None,
    ) -> None:
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
    ) -> list[str]:
        plan = self.build_plan(
            search_query, constraints, no_preference, raw_constraints, user_profile
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
        return self._sanitize(ranked_ids, top_k)

    def build_plan(
        self,
        search_query: str,
        constraints: dict,
        no_preference: Iterable[str] = (),
        raw_constraints: Iterable[dict] = (),
        user_profile: dict | None = None,
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

        return RetrievalPlan(
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
            stages["relevance"] = [generation, *stages["relevance"]]
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
        for attribute, key in (("color", "Color"), ("material", "Material"), ("size", "Size"),
                               ("style", "Style"), ("brand", "store")):
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

    def _structured_value(self, record: ProductRecord, key: str) -> str:
        if key == "store":
            return record.store.strip().lower()[:40]
        return record.facets.get(key, "")

    # --------------------------------------------------------------- build

    def _build(self) -> None:
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

    def _iter_catalog(self) -> Iterable[dict]:
        if not self.catalog_path.exists():
            raise FileNotFoundError(f"Catalog not found: {self.catalog_path}")
        opener = gzip.open if self.catalog_path.suffix == ".gz" else open
        with opener(self.catalog_path, mode="rt", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)

    def _record(self, product: dict) -> ProductRecord:
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
        rating_bonus = max(0.0, record.rating - 3.5) * self.weights.rating_coefficient
        popularity_bonus = (
            min(2.0, math.log1p(record.rating_count) / 6.0) * self.weights.popularity_scale
        )
        return rating_bonus + popularity_bonus

    def _sanitize(self, ranked_ids: Iterable[str], top_k: int) -> list[str]:
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
