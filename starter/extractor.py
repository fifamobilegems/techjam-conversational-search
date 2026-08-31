from __future__ import annotations

import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from state.state_manager import AttributeUpdate, ExtractedTurn


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
    "alloy",
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

COLOR_ALIASES = {"navy": "blue", "beige": "brown", "tan": "brown", "grey": "gray"}
USE_CASE_ALIASES = {"jogging": "running"}

ATTRIBUTE_WORDS = (
    "category",
    "material",
    "color",
    "size",
    "style",
    "brand",
    "budget",
    "feature",
    "use_case",
    "other",
)

TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)

# The customer signals it has nothing further to disclose with
# "I don't have an additional preference for X." That is NOT the same as
# the boundary reply "I don't have a preference for X; please use your
# judgment." -- the first ends the questioning, the second only rules out
# one field.
EXHAUSTED_RE = re.compile(
    r"\b(?:do not|don't|dont)\s+have\s+an\s+additional\s+preference"
    r"(?:\s+for\s+(?P<attribute>[a-z_ ]+))?",
    re.IGNORECASE,
)

# Scenario markers, taken from the opening message wording. These are
# observable text patterns, not inferred "moods".
BUYING_MARKER = "a key requirement is:"
BROWSING_MARKER = "but i'm still exploring"

USE_CASE_WORDS = {
    "hiking",
    "running",
    "gym",
    "winter",
    "outdoor",
    "work",
    "trail",
    "walking",
    "yoga",
    "sports",
}

CATEGORY_FILLER_WORDS = {
    "a",
    "an",
    "some",
    "new",
    "pair",
    "pairs",
    "of",
    "for",
    "me",
}


def _clean(value: str, limit: int = 180) -> str:
    """Collapse whitespace, trim punctuation, and cap length."""
    value = re.sub(r"\s+", " ", value).strip(" -;,.\t\n")
    return value[:limit].rstrip()


def _split_constraints(value: str) -> list[str]:
    """Split one disclosed span into separate constraints on ';' or 'and'."""
    value = _clean(value)
    if not value:
        return []
    parts = re.split(r"\s*;\s*", value)
    if len(parts) == 1:
        parts = re.split(r"\s+\band\b\s+", value, flags=re.IGNORECASE)
    return [_clean(part) for part in parts if _clean(part)]


def _first_word_match(words: Iterable[str], text: str) -> str | None:
    """Return the first vocabulary word occurring in `text`, else None."""
    lowered = text.lower()
    for word in sorted(words):
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return word
    return None


def _category_from_phrase(value: str) -> str | None:
    """Strip colour, material and budget words from a phrase, leaving the product type.

    "black leather boots under $50" reduces to "boots", which is what the
    category slot should hold.
    """
    category = re.sub(
        r"\b(?:under|below|less than|around|about)\s+\$?\d+(?:\.\d{1,2})?\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    category = re.sub(r"\$\s*\d+(?:\.\d{1,2})?", " ", category)
    for word in sorted(COLORS | MATERIALS):
        category = re.sub(rf"\b{re.escape(word)}\b", " ", category, flags=re.IGNORECASE)
    category = _clean(category)
    tokens = [
        token
        for token in category.split()
        if token.lower() not in CATEGORY_FILLER_WORDS
    ]
    category = _clean(" ".join(tokens))
    return category or None


def _state_slots(state: object | None) -> dict:
    """Read `state.slots` defensively; returns {} for any state-like object without it."""
    slots = getattr(state, "slots", None)
    return slots if isinstance(slots, dict) else {}


def _last_non_category_attribute(state: object | None) -> str | None:
    """Most recently touched attribute other than category.

    Used by the override path: "ignore my earlier preference" names no
    attribute, so the one changed most recently is the one being retracted.
    """
    history = getattr(state, "history", None)
    if not isinstance(history, list):
        return None
    for item in reversed(history):
        if not isinstance(item, dict):
            continue
        attribute = item.get("attribute")
        if isinstance(attribute, str) and attribute in ATTRIBUTE_WORDS and attribute != "category":
            return attribute
    return None


def classify_constraint(value: str) -> str:
    """Assign a free-text constraint span to one of the allowed attributes.

    An ordered cascade rather than a classifier: each test is a literal cue,
    so every decision is traceable to text. Unrecognised spans fall through
    to "feature", which is the least damaging bucket in the reranker.
    """
    lowered = value.lower()
    if re.search(r"\bmaterial\s*:", lowered):
        return "material"
    if re.search(r"\bcolou?r\s*:", lowered):
        return "color"
    if "budget" in lowered or re.search(r"(?:\$|<=|<|under|below|less than|around)\s*\d", lowered):
        return "budget"
    if _first_word_match(MATERIALS, lowered):
        return "material"
    if "color" in lowered or _first_word_match(COLORS, lowered):
        return "color"
    if any(word in lowered for word in ("size", "sizing", "width", "wide", "narrow", "petite", "plus")):
        return "size"
    if any(word in lowered for word in ("brand", "store", "manufacturer", "made by")):
        return "brand"
    if any(word in lowered for word in ("hiking", "running", "gym", "winter", "outdoor", "work", "trail")):
        return "use_case"
    if any(word in lowered for word in ("department", "style", "fit", "sleeve", "neck", "casual", "formal")):
        return "style"
    return "feature"


DEFAULT_LEXICON_PATH = Path("data/lexicon.json")

# Which attributes Tier 1 is allowed to emit.
#
# Tier 1 values reach `retriever._constraint_score_details` as ordinary
# constraints, where a miss is a penalty rather than a zero: category, colour
# and material cost -20, brand -50. A gazetteer guess is therefore a bet, not
# a free hint, and it has to be measured per attribute.
#
# `category` is excluded by default because that measurement is unambiguous.
# Ablated on esci1000 x esci, 200 samples, TechnicalScore:
#
#     Tier 1 off                          0.7268
#     category only                       0.6437   (-0.083)
#     category + colour + material        0.6684
#     everything except category          0.7713   (+0.045)
#
# A category guessed from a short real query is frequently right in spirit and
# wrong in wording -- "bras" against a listing filed under "Lingerie
# Accessories" -- and the -20 lands on the true target. The other attributes
# name substances and shades that appear verbatim in the product text, so they
# hit or abstain rather than misfire.
#
# Re-run the ablation after any retrieval change, and re-enable `category`
# once the reranker honours `strength="soft"` (filed for Role D in
# REQUESTS.md), which converts that -20 into an abstention:
#
#     TIER1_ATTRIBUTES=category,color,material,size,style,brand python3 -m tools.bench
TIER1_ATTRIBUTES = tuple(
    item.strip()
    for item in os.environ.get(
        "TIER1_ATTRIBUTES", "color,material,size,style,brand"
    ).split(",")
    if item.strip()
)

# Tokens plus their offsets in the original message, so a canonical value can
# be stored in the slot while the shopper's own wording survives as raw_text.
TOKEN_SPAN_RE = re.compile(r"[a-z0-9]+")

# Words that carry no product requirement, so leaving them unexplained is not
# evidence that extraction missed anything. The retrieval stopword list is the
# seed; the extras are conversational filler the shopper wraps a request in
# ("hi, I need something for ..."). Kept local to this module because the
# escalation gate is an extraction concern and must not import a retrieval
# constant -- the layers stay independently testable.
RESIDUAL_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "i",
    "in", "is", "it", "looking", "me", "my", "of", "on", "or", "please", "some",
    "that", "the", "this", "to", "want", "with", "would", "you",
    # conversational filler
    "about", "actually", "also", "am", "any", "anything", "can", "could", "do",
    "does", "get", "getting", "give", "going", "good", "have", "hello", "help",
    "hey", "hi", "just", "kind", "know", "like", "maybe", "more", "much",
    "need", "needs", "not", "now", "one", "really", "right", "s", "see",
    "something", "sort", "sure", "thanks", "there", "they", "thing", "things",
    "think", "thinking", "up", "us", "ve", "we", "well", "what", "when",
    "where", "which", "will", "yes",
})


# Structural templates Tier 0 keys on. Used only by the Phase 8 escalation
# gate, which asks "did the message look like anything we know?" -- not by
# extraction itself.
TEMPLATE_MARKER_RE = re.compile(
    r"(a key requirement is|what i need is|what matters is|looking for|shopping for|"
    r"searching for|ignore (?:my )?(?:earlier|previous) preference|no preference|"
    r"(?:do not|don'?t) have (?:a|an additional) preference|(?:do not|don'?t) care)",
    re.IGNORECASE,
)

# --- polarity layer -------------------------------------------------------
#
# NegEx-style: a closed cue set and a short forward scope. Deliberately not a
# parser -- true product-attribute negation is under 1% of real queries, so the
# risk here is entirely on the false-positive side.

NEGATION_CUES = {
    "without", "not", "no", "non", "never", "avoid", "exclude", "excluding",
    "anti", "sans", "isn", "aren", "doesn", "dont",
}

# Two-token cues, matched before single tokens.
NEGATION_PHRASES = {
    ("do", "not"), ("don", "t"), ("does", "not"), ("other", "than"),
    ("rather", "than"), ("free", "of"), ("free", "from"), ("instead", "of"),
}

# A negation stops at a clause boundary. Coordinating conjunctions end it too:
# "no leather and cotton" negates leather, not cotton.
SCOPE_TERMINATORS = {
    "but", "however", "although", "though", "and", "or", "yet", "so",
    "while", "whereas", "plus",
    # Budget and prepositional markers start a new constraint. Without these,
    # "without laces under $120" negates the budget as well as the laces.
    "under", "below", "over", "above", "less", "more", "than", "around",
    "about", "between", "within", "up", "max", "maximum", "min", "minimum",
    "for", "with", "size", "in",
}

# Longest run of tokens a single cue may negate. Short on purpose: a wide
# window swallows the next constraint, and over-negating is the failure mode
# that makes this layer a net regression.
SCOPE_WINDOW = 3

# Only these tiers may be negated.
#
# Tier 0 spans are verbatim catalog metadata the customer recited AS a
# requirement, and a negation word inside one belongs to the attribute's name,
# not to the shopper. Audited over 1,400 opening messages, every Tier 0
# negation was a false positive:
#
#     "No Closure closure"                    a details value
#     "Non-Polarized"                         a details value
#     "...meaning 'Never Truly Part'..."      a product description
#     "...there is no better leather..."      marketing prose
#
# Negating those drops a real constraint, so template Tier 0 is excluded
# outright. Its terminal fallback is not: `tier0_fallback` is a bare gazetteer
# sweep over the raw message, indistinguishable in kind from a Tier 1 guess,
# and excluding it meant "not blue" and "no polyester" survived as positive
# hard constraints. Template spans stay un-negatable, so official phrasing --
# which always matches a template -- is still untouched by this module.
NEGATABLE_PROVENANCE = frozenset({"tier0_fallback", "tier1", "tier2"})

# A shopper negates a thing, not a paragraph. The one true negation in the
# audit was "without horns"; every false positive was a long recited span.
MAX_NEGATABLE_SPAN_WORDS = 3

# Product-feature idioms whose names contain a negation cue. The lexicon guard
# catches these only when the catalog evidences them as a multi-word entry
# ("no show" is mined; "no iron" is not), so the plan's named cases are held
# explicitly. Each is a garment feature, never an operator.
NEGATION_FALSE_FRIENDS = {
    ("no", "show"), ("no", "iron"), ("no", "tie"), ("no", "slip"),
    ("non", "slip"), ("non", "iron"), ("no", "fade"), ("no", "roll"),
    ("no", "dig"), ("no", "chafe"), ("no", "sew"), ("no", "seam"),
    ("no", "gap"), ("non", "skid"), ("non", "stick"), ("no", "pull"),
}

CLAUSE_PUNCTUATION = set(".,;:!?()")


@dataclass(frozen=True)
class LexiconMatch:
    """One gazetteer hit, located in the original message."""

    attribute: str
    canonical: str
    text: str
    token_start: int
    token_end: int
    char_start: int
    char_end: int
    df: int

    @property
    def word_count(self) -> int:
        """Number of tokens the match spans. Longest match wins in Tier 1."""
        return self.token_end - self.token_start + 1


class LexiconTagger:
    """Longest-match gazetteer over `data/lexicon.json`.

    The lexicon is mined from catalog metadata by `scripts/build_lexicon.py`,
    which turns 12 hardcoded colours into 107 and 10 materials into 107, and
    adds 525 category terms where Tier 0 has no vocabulary at all.

    A missing or unreadable lexicon yields an empty tagger rather than an
    error: Tier 1 then contributes nothing and the agent behaves exactly as it
    did before this cascade existed.
    """

    def __init__(self, index: dict[str, tuple[str, str, int]] | None = None) -> None:
        """Wrap a prebuilt surface-form index."""
        self.index = index or {}
        self.max_words = max((len(key.split()) for key in self.index), default=0)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "LexiconTagger":
        """Load `data/lexicon.json` into a surface-form lookup.

        A missing or unreadable file yields an empty tagger rather than an error,
        so a checkout without the artifact still runs -- Tier 1 simply
        contributes nothing.
        """
        lexicon_path = Path(path) if path is not None else DEFAULT_LEXICON_PATH
        try:
            payload = json.loads(lexicon_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls({})

        index: dict[str, tuple[str, str, int]] = {}
        for attribute, block in sorted((payload.get("attributes") or {}).items()):
            for entry in block.get("entries", []):
                canonical = str(entry.get("canonical", ""))
                df = int(entry.get("df", 0))
                for surface in entry.get("surfaces", []):
                    key = str(surface)
                    # The build script resolves cross-attribute ownership, but
                    # a generated variant can still collide. Higher document
                    # frequency wins so the result never depends on file order.
                    existing = index.get(key)
                    if existing is None or df > existing[2]:
                        index[key] = (attribute, canonical, df)
        return cls(index)

    def scan(self, message: str) -> list[LexiconMatch]:
        """Non-overlapping longest matches, left to right."""
        if not self.index:
            return []

        tokens = [
            (match.group(0), match.start(), match.end())
            for match in TOKEN_SPAN_RE.finditer(message.lower())
        ]
        matches: list[LexiconMatch] = []
        position = 0
        while position < len(tokens):
            span = 0
            found: tuple[str, str, int] | None = None
            for width in range(min(self.max_words, len(tokens) - position), 0, -1):
                surface = " ".join(token for token, _, _ in tokens[position:position + width])
                entry = self.index.get(surface)
                if entry is not None:
                    span, found = width, entry
                    break
            if found is None:
                position += 1
                continue
            attribute, canonical, df = found
            char_start = tokens[position][1]
            char_end = tokens[position + span - 1][2]
            matches.append(
                LexiconMatch(
                    attribute=attribute,
                    canonical=canonical,
                    text=message[char_start:char_end],
                    token_start=position,
                    token_end=position + span - 1,
                    char_start=char_start,
                    char_end=char_end,
                    df=df,
                )
            )
            position += span
        return matches


class PolarityScanner:
    """Mark operations the shopper actually rejected.

    A gazetteer has no notion of scope, so "not blue" tags `color=blue` as a
    positive constraint -- worse than missing it, because the value then enters
    the BM25 query and spends candidate budget retrieving what was rejected.

    This layer only ever *tags*: it sets `polarity="negate"` and never drops an
    operation. Acting on the tag -- stripping the value from the query text and
    applying it as an exclusion filter -- belongs to retrieval, and is filed in
    REQUESTS.md for Role D. Tagging alone cannot move any score, which is the
    point: negation is here for correctness, not for points.
    """

    def apply(
        self,
        message: str,
        operations: list[AttributeUpdate],
        matches: list[LexiconMatch],
    ) -> None:
        """Tag operations that fall inside a negation scope.

        Tags only: an operation is never dropped here. What a negated slot means
        is `state_manager.replay`'s decision.
        """
        spans = self.negated_spans(message, matches)
        if not spans:
            return

        lowered = message.lower()
        for operation in operations:
            # Only positive assertions can be negated. `no_preference`,
            # `clear` and `demote` already encode their own semantics, and
            # re-marking them would double-count the same cue.
            if operation.action != "set":
                continue
            if getattr(operation, "provenance", "legacy") not in NEGATABLE_PROVENANCE:
                continue
            raw = str(operation.raw_text or operation.value or "").lower().strip()
            if not raw or len(raw.split()) > MAX_NEGATABLE_SPAN_WORDS:
                continue
            start = lowered.find(raw)
            if start < 0:
                continue
            end = start + len(raw)
            if any(start < span_end and end > span_start for span_start, span_end in spans):
                operation.polarity = "negate"

    def negated_spans(self, message: str, matches: list[LexiconMatch]) -> list[tuple[int, int]]:
        """Character ranges falling inside the scope of a negation cue."""
        lowered = message.lower()
        tokens = [
            (match.group(0), match.start(), match.end())
            for match in TOKEN_SPAN_RE.finditer(lowered)
        ]
        if not tokens:
            return []

        # FALSE-FRIEND GUARD, applied before any scope is computed.
        #
        # "no show socks" and "no iron shirt" are product types whose names
        # contain a negation cue. A multi-word lexicon entry covering the cue
        # token means the cue is part of a product name, not an operator.
        # Getting this wrong is a net regression on real queries, which is why
        # it runs first rather than as a post-filter.
        guarded = {
            index
            for match in matches
            if match.word_count > 1
            for index in range(match.token_start, match.token_end + 1)
        }
        for position in range(len(tokens) - 1):
            if (tokens[position][0], tokens[position + 1][0]) in NEGATION_FALSE_FRIENDS:
                guarded.update((position, position + 1))

        spans: list[tuple[int, int]] = []
        index = 0
        while index < len(tokens):
            width = self._cue_width(tokens, index)
            if width == 0 or index in guarded:
                index += 1
                continue
            scope = self._scope(lowered, tokens, index + width)
            if scope is not None:
                spans.append(scope)
            index += width
        return spans

    def _cue_width(self, tokens: list[tuple[str, int, int]], index: int) -> int:
        """Return how many tokens the cue at `index` spans, or 0 if it is not a cue.

        Two-token phrases are checked first so "other than" is not read as a
        bare "other".
        """
        if index + 1 < len(tokens):
            pair = (tokens[index][0], tokens[index + 1][0])
            if pair in NEGATION_PHRASES:
                return 2
        return 1 if tokens[index][0] in NEGATION_CUES else 0

    def _scope(
        self,
        lowered: str,
        tokens: list[tuple[str, int, int]],
        start_index: int,
    ) -> tuple[int, int] | None:
        """Forward window from the cue, cut at the first clause boundary."""
        if start_index >= len(tokens):
            return None
        span_start = tokens[start_index][1]
        span_end = None
        for offset in range(min(SCOPE_WINDOW, len(tokens) - start_index)):
            position = start_index + offset
            token, token_start, token_end = tokens[position]
            if token in SCOPE_TERMINATORS:
                break
            if offset > 0:
                gap = lowered[tokens[position - 1][2]:token_start]
                if any(character in CLAUSE_PUNCTUATION for character in gap):
                    break
            span_end = token_end
        return None if span_end is None else (span_start, span_end)


class HeuristicTurnExtractor:
    """Deterministic extraction cascade.

    Tier 0 -- the template regex pipeline in :meth:`_extract_tier0` -- runs
    first and stays authoritative. It is worth 0.84 on official phrasing
    because it captures catalog metadata the simulator recites verbatim, so it
    is deliberately left untouched: fuzzy-matching it would damage exactly the
    thing that makes it work.

    Tier 1 -- lexicon tagging -- runs ONLY when Tier 0 returned no operations.
    That single condition is what keeps official phrasing flat: wherever a
    template matches, Tier 1 never executes and the output is byte-identical
    to before the cascade existed.

    Tier 2 -- the LLM in :mod:`state.llm_extractor` -- wraps this class from
    outside and is gated on :attr:`last_trace`.
    """

    def __init__(self, lexicon_path: str | Path | None = None) -> None:
        """Load the lexicon and prepare the polarity scanner."""
        self.tagger = LexiconTagger.load(lexicon_path)
        self.polarity = PolarityScanner()
        # Cumulative per-tier yield, and a per-turn record the LLM tier reads
        # to decide whether escalation is structurally justified.
        self.tier_counts: Counter = Counter()
        self.last_trace: dict[str, object] = {}

    def extract(self, user_message: str, state: object | None = None) -> ExtractedTurn:
        """Run the cascade and tag every operation with its origin."""

        turn = self._extract_tier0(user_message, state)
        for operation in turn.operations:
            # `_extract_tier0` already distinguishes template spans ("tier0")
            # from its terminal gazetteer sweep ("tier0_fallback"). Only
            # untagged operations need the default.
            if operation.provenance == "legacy":
                operation.provenance = "tier0"

        tier0_count = len(turn.operations)
        tier1_count = 0

        # The lexicon scan runs unconditionally, but only *emits* when Tier 0
        # came back empty. The matches themselves are needed either way: the
        # polarity layer uses them as its false-friend guard, and a cue sitting
        # inside "no show socks" must be recognised as part of a product name
        # even on a turn where Tier 0 did all the work.
        matches = self.tagger.scan(user_message)

        if not turn.operations:
            for operation in self._tier1_operations(matches):
                turn.operations.append(operation)
            tier1_count = len(turn.operations)

        self.polarity.apply(user_message, turn.operations, matches)

        turn.provenance = "tier1" if tier1_count else "tier0"
        self.tier_counts["turns"] += 1
        self.tier_counts["tier0_operations"] += tier0_count
        self.tier_counts["tier1_operations"] += tier1_count
        self.tier_counts["tier0_turns"] += 1 if tier0_count else 0
        self.tier_counts["tier1_turns"] += 1 if tier1_count else 0
        self.tier_counts["empty_turns"] += 0 if turn.operations else 1

        coverage = self._coverage(user_message, turn.operations, matches)
        self.last_trace = {
            "tier0_operations": tier0_count,
            "tier1_operations": tier1_count,
            "lexicon_matches": len(matches),
            "template_matched": bool(TEMPLATE_MARKER_RE.search(user_message)),
            "negated_operations": sum(
                1 for item in turn.operations if getattr(item, "polarity", "must") == "negate"
            ),
            **coverage,
        }
        return turn

    @staticmethod
    def _coverage(
        user_message: str,
        operations: list[AttributeUpdate],
        matches: list[LexiconMatch],
    ) -> dict[str, object]:
        """How much of the message the deterministic cascade actually explained.

        The Phase 8 gate could only ask "did the cascade emit anything?". That
        treats a one-word gazetteer hit on a two-clause sentence as a complete
        reading of it. These four numbers make the weaker question askable:
        which content words did the cascade account for, and how strong was the
        evidence behind the ones it did?

        Coverage is computed over token *strings*, not offsets, because an
        operation carries the shopper's wording in ``raw_text`` without a
        position -- a template span is assembled, not sliced. A repeated token
        therefore counts as covered wherever it appears, which errs toward
        *less* escalation and keeps the gate conservative.

        Unemitted lexicon matches count as covered on purpose. Tier 1 emits one
        operation per attribute, so a dropped second colour is still vocabulary
        the cascade recognised, and a model would add nothing there.
        """

        content = [
            token
            for token in TOKEN_SPAN_RE.findall(user_message.lower())
            if token not in RESIDUAL_STOPWORDS and len(token) > 1
        ]
        if not content:
            return {
                "content_tokens": 0,
                "residual_tokens": 0,
                "coverage": 1.0,
                "tier1_max_df": 0,
                "tier1_max_words": 0,
            }

        covered: set[str] = set()
        for match in matches:
            covered.update(TOKEN_SPAN_RE.findall(match.text.lower()))
        for operation in operations:
            for field in (operation.raw_text, operation.value):
                if field:
                    covered.update(TOKEN_SPAN_RE.findall(str(field).lower()))

        residual = [token for token in content if token not in covered]
        tier1 = [item for item in matches if item.attribute in set(TIER1_ATTRIBUTES)]
        return {
            "content_tokens": len(content),
            "residual_tokens": len(residual),
            "coverage": round(1.0 - len(residual) / len(content), 4),
            # Document frequency of the *most common* surface Tier 1 relied on.
            # "black" matches thousands of products and separates nothing; a
            # model number matches one. A high value means weak evidence.
            "tier1_max_df": max((item.df for item in tier1), default=0),
            # A multi-word gazetteer hit ("merino wool") is far stronger
            # evidence than a single common adjective.
            "tier1_max_words": max((item.word_count for item in tier1), default=0),
        }

    def _tier1_operations(self, matches: list[LexiconMatch]) -> list[AttributeUpdate]:
        """Turn lexicon matches into at most one operation per attribute.

        Slots hold one value each, so emitting two categories would simply
        overwrite. The longest match wins -- "running shoes" beats "shoes" --
        and document frequency breaks ties toward the value the catalog
        actually evidences.
        """

        allowed = set(TIER1_ATTRIBUTES)
        best: dict[str, LexiconMatch] = {}
        for match in matches:
            if match.attribute not in allowed:
                continue
            current = best.get(match.attribute)
            if current is None or (match.word_count, match.df) > (current.word_count, current.df):
                best[match.attribute] = match

        operations = []
        for attribute in sorted(best):
            match = best[attribute]
            operations.append(
                AttributeUpdate(
                    attribute=attribute,
                    action="set",
                    # Canonical value drives the slot and the constraint
                    # scorer; the shopper's own wording is kept as the span
                    # retrieval matches literally.
                    value=match.canonical,
                    raw_text=match.text,
                    provenance="tier1",
                    # A gazetteer hit is weaker evidence than a template the
                    # customer explicitly framed as a requirement.
                    strength="soft",
                    confidence=0.6,
                )
            )
        return operations

    def _extract_tier0(self, user_message: str, state: object | None = None) -> ExtractedTurn:
        """The original template-regex pipeline, unchanged.

        Worth 0.84 on official phrasing because it captures catalog metadata the
        simulator recites verbatim. Deliberately untouched: fuzzy-matching it
        would damage exactly the property that makes it work.
        """
        lowered = user_message.lower()
        # "I don't have an additional preference for X" is scoped to X.
        #
        # It only means the CONVERSATION is over when X was the universal
        # question -- `other` is answered without being classified, so nothing
        # further to say about `other` means nothing further at all. For a
        # typed question it means only that one field is empty, and latching
        # global exhaustion there ends the session after the first
        # unanswerable attribute. That was invisible while the policy asked
        # `other` every turn, and collapses the official columns the moment
        # typed questions ship.
        exhausted_match = EXHAUSTED_RE.search(lowered)
        exhausted_attribute = (
            (exhausted_match.group("attribute") or "").strip().replace(" ", "_")
            if exhausted_match
            else ""
        )
        information_exhausted = bool(exhausted_match) and exhausted_attribute in {"", "other"}
        scenario = self._scenario(lowered, state)
        operations: list[AttributeUpdate] = []
        seen: set[tuple[str, str, str]] = set()
        slots = _state_slots(state)
        override_requested = any(
            phrase in lowered
            for phrase in ("actually", "instead", "ignore earlier", "ignore my earlier")
        )

        def add_operation(
            attribute: str,
            action: str,
            value: str | None = None,
            raw_text: str | None = None,
            provenance: str = "tier0",
        ) -> None:
            """Record one attribute change, ignoring exact duplicates."""
            cleaned = _clean(value or "") if value is not None else None
            key = (attribute, action, cleaned or "")
            if key in seen:
                return
            seen.add(key)
            operations.append(
                AttributeUpdate(
                    attribute=attribute,
                    action=action,
                    value=cleaned,
                    raw_text=_clean(raw_text or cleaned or "") or None,
                    provenance=provenance,
                )
            )

        def add_set(attribute: str, value: str, provenance: str = "tier0") -> None:
            """Record a `set`, canonicalizing the value and demoting any it overrides."""
            raw_value = _clean(value)
            cleaned = raw_value
            if attribute == "color":
                # Preserve labelled catalog wording such as "color: grey".
                # Only a standalone noisy colour alias is safe to canonicalize.
                cleaned = COLOR_ALIASES.get(raw_value.lower(), raw_value.lower())
            elif attribute == "use_case":
                cleaned = USE_CASE_ALIASES.get(raw_value.lower(), raw_value.lower())
            if (
                override_requested
                and attribute != "category"
                and attribute in slots
                and str(slots[attribute]) != cleaned
            ):
                add_operation(attribute, "demote", str(slots[attribute]))
            add_operation(attribute, "set", cleaned, raw_value, provenance=provenance)

        def has_set(attribute: str) -> bool:
            """True when this turn already set the given attribute."""
            return any(item.attribute == attribute and item.action == "set" for item in operations)

        intent = None
        if override_requested:
            intent = "override"
        elif any(phrase in lowered for phrase in ("still exploring", "exploring", "browsing")):
            intent = "browsing"
        elif any(phrase in lowered for phrase in ("key requirement", "what i need", "i need", "must-have")):
            intent = "buying"
        elif "looking for" in lowered:
            intent = "buying"

        if re.search(r"\bignore\s+(?:my\s+)?(?:earlier|previous)\s+preference\b", lowered):
            attribute = _last_non_category_attribute(state)
            if attribute:
                add_operation(attribute, "demote")

        for pattern in (
            r"\bignore\s+([^;,.]+)",
            r"\binstead\s+of\s+([^;,.]+)",
            r"\bno\s+longer\s+(?:need|want)\s+([^;,.]+)",
        ):
            for match in re.finditer(pattern, user_message, flags=re.IGNORECASE):
                ignored = _clean(match.group(1))
                if re.search(r"\b(?:my\s+)?(?:earlier|previous)\s+preference\b", ignored, flags=re.I):
                    continue
                add_operation(classify_constraint(ignored), "demote", ignored)

        if (
            exhausted_match
            and not information_exhausted
            and exhausted_attribute in ATTRIBUTE_WORDS
        ):
            add_operation(exhausted_attribute, "no_preference")

        clear_match = re.search(
            r"\b(?:clear|remove|drop)\s+(category|material|color|size|style|brand|budget|feature|use_case|other)\b",
            lowered,
        )
        if clear_match:
            add_operation(clear_match.group(1), "clear")

        no_preference_patterns = (
            r"\b(?:no preference|no pref)\s+(?:for|on|about)\s+([a-z_ ]+)",
            r"\b(?:do not|don't|dont)\s+have\s+(?:a\s+)?preference\s+for\s+([a-z_ ]+)",
            r"\b(?:do not|don't|dont)\s+care\s+(?:about|for)\s+([a-z_ ]+)",
        )

        # "no additional preference" means the customer is done talking,
        # not that this field is irrelevant. Do not blacklist the field.
        for pattern in () if information_exhausted else no_preference_patterns:
            no_pref_match = re.search(pattern, lowered)
            if no_pref_match:
                attribute = no_pref_match.group(1).strip().replace(" ", "_")
                if attribute in ATTRIBUTE_WORDS:
                    intent = intent or "boundary"
                    add_operation(attribute, "no_preference")

        category_match = re.search(
            r"\b(?:looking for|shopping for|searching for)\s+(?:(?:a|an|some)\s+)?(.*?)(?:, but|\.|;|$)",
            user_message,
            flags=re.IGNORECASE,
        )
        if category_match and not lowered.startswith("for that"):
            category = _clean(category_match.group(1))
            if category and category.lower() not in {"that", "it", "this"}:
                add_set("category", category)

        constraint_spans: list[str] = []
        for pattern in (
            r"\bkey requirement is:?\s*(.+)$",
            r"\bwhat i need is:?\s*(.+)$",
            r"\bwhat matters is:?\s*(.+)$",
        ):
            match = re.search(pattern, user_message, flags=re.IGNORECASE)
            if match:
                constraint_spans.append(match.group(1))

        # Intent-override opening messages look like:
        # "I'm looking for <category>. <old preference>"
        if "." in user_message and "key requirement" not in lowered and "what matters is" not in lowered:
            tail = _clean(user_message.split(".", 1)[1])
            if tail and not tail.lower().startswith(("ask me", "those options", "what i need")):
                constraint_spans.append(tail)

        for span in constraint_spans:
            for value in _split_constraints(span):
                add_set(classify_constraint(value), value)

        direct_need_spans: list[str] = []
        if not constraint_spans:
            for pattern in (
                r"\b(?:i\s+need|i\s+want|need|want)\s+(?!is\b)([^.;]+)",
                r"\b(?:exploring|browsing)\s+(?:for\s+)?(?:(?:a|an|some)\s+)?([^.;]+)",
            ):
                for match in re.finditer(pattern, user_message, flags=re.IGNORECASE):
                    value = _clean(match.group(1))
                    if value and not value.lower().startswith(("to ", "your ")):
                        direct_need_spans.append(value)

        for value in direct_need_spans:
            color = _first_word_match(COLORS | set(COLOR_ALIASES), value)
            if color:
                add_set("color", color)

            material = _first_word_match(MATERIALS, value)
            if material:
                add_set("material", material)

            use_case = _first_word_match(USE_CASE_WORDS | set(USE_CASE_ALIASES), value)
            if use_case:
                add_set("use_case", use_case)

            category = _category_from_phrase(value)
            if category:
                add_set("category", category)

        if not constraint_spans:
            budget_match = re.search(
                r"\b(?:under|below|less than|around|about|budget(?: is)?|<=|<)?\s*\$?\s*(\d+(?:\.\d{1,2})?)",
                user_message,
                flags=re.IGNORECASE,
            )
            if budget_match and any(
                marker in lowered
                for marker in ("$", "budget", "under", "below", "less than", "around")
            ):
                add_set("budget", _clean(budget_match.group(0)))

            # Tagged `tier0_fallback`, not `tier0`. These two lines are a bare
            # gazetteer sweep of the whole message -- the same kind of guess
            # Tier 1 makes -- not catalog metadata the customer recited inside
            # a template. Treating them as un-negatable Tier 0 meant "not blue"
            # and "without leather" produced positive hard constraints, which
            # is worse than extracting nothing: the rejected value entered the
            # slot, the query and the scorer.
            color = _first_word_match(COLORS | set(COLOR_ALIASES), user_message)
            if color and not has_set("color"):
                add_set("color", color, provenance="tier0_fallback")

            material = _first_word_match(MATERIALS, user_message)
            if material and not has_set("material"):
                add_set("material", material, provenance="tier0_fallback")

        return ExtractedTurn(
            intent=intent,
            operations=operations,
            information_exhausted=information_exhausted,
            scenario=scenario,
        )

    def _scenario(self, lowered: str, state: object | None) -> str | None:
        """
        Label the session from the opening message only.

        The three openings are textually distinct, so this needs no
        guessing. Browsing and boundary share an opening; boundary
        identifies itself later, on the first question it declines.
        """

        if getattr(state, "scenario", "unknown") != "unknown":
            return None

        if not lowered.startswith("i'm looking for"):
            return None

        if BUYING_MARKER in lowered:
            return "buying"

        if BROWSING_MARKER in lowered:
            return "browsing_or_boundary"

        return "intent_override"
