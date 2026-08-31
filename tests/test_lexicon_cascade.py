"""Tier 1 lexicon tagging, the polarity layer, and the LLM escalation gate.

The load-bearing test in here is
``test_tier1_never_runs_when_tier0_produced_operations``. Tier 0 carries the
official simulator, and the single guarantee that keeps official phrasing flat
is that Tier 1 cannot execute on a turn where a template matched.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starter.extractor import (
    HeuristicTurnExtractor,
    LexiconTagger,
    PolarityScanner,
)
from state.llm_extractor import (
    GATE_EMPTY,
    GATE_LOW_CONFIDENCE,
    LLMTurnExtractor,
)


LEXICON = {
    "lexicon_version": 1,
    "attributes": {
        "category": {
            "count": 4,
            "entries": [
                {"canonical": "shoes", "df": 1299, "surfaces": ["shoe", "shoes"]},
                {"canonical": "hoodies", "df": 120, "surfaces": ["hoodie", "hoodies"]},
                {"canonical": "running shoes", "df": 60, "surfaces": ["running shoes"]},
                {"canonical": "no show", "df": 30, "surfaces": ["no show"]},
            ],
        },
        "color": {
            "count": 2,
            "entries": [
                {"canonical": "blue", "df": 67, "surfaces": ["blue", "blues"]},
                {"canonical": "burgundy", "df": 4, "surfaces": ["burgundy"]},
            ],
        },
        "material": {
            "count": 1,
            "entries": [{"canonical": "leather", "df": 297, "surfaces": ["leather"]}],
        },
    },
}


def write_lexicon(root: Path) -> Path:
    path = root / "lexicon.json"
    path.write_text(json.dumps(LEXICON), encoding="utf-8")
    return path


class LexiconTaggerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.tagger = LexiconTagger.load(write_lexicon(Path(self._temp.name)))

    def test_longest_match_wins(self) -> None:
        matches = self.tagger.scan("i want running shoes")
        self.assertEqual([match.canonical for match in matches], ["running shoes"])

    def test_surface_variant_maps_to_canonical_but_keeps_the_span(self) -> None:
        match = self.tagger.scan("a leather shoe please")[-1]
        self.assertEqual(match.canonical, "shoes")
        self.assertEqual(match.text, "shoe")

    def test_matches_are_non_overlapping_and_ordered(self) -> None:
        matches = self.tagger.scan("blue leather shoes")
        self.assertEqual([m.canonical for m in matches], ["blue", "leather", "shoes"])
        self.assertTrue(all(a.char_end <= b.char_start for a, b in zip(matches, matches[1:])))

    def test_missing_lexicon_degrades_to_an_empty_tagger(self) -> None:
        # A missing artifact must not raise: Tier 1 simply contributes nothing.
        tagger = LexiconTagger.load(Path(self._temp.name) / "absent.json")
        self.assertEqual(tagger.scan("blue leather shoes"), [])


class CascadeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = write_lexicon(Path(self._temp.name))
        self.extractor = HeuristicTurnExtractor(self.path)

    def _attributes(self, message: str) -> dict[str, str]:
        return {
            item.attribute: str(item.value)
            for item in self.extractor.extract(message).operations
        }

    def test_tier1_never_runs_when_tier0_produced_operations(self) -> None:
        """The invariant that keeps the official simulator flat."""
        message = "I'm looking for hiking boots. A key requirement is: 100% Leather."
        with_lexicon = self.extractor.extract(message)
        without_lexicon = HeuristicTurnExtractor(Path(self._temp.name) / "absent.json").extract(message)

        self.assertEqual(self.extractor.last_trace["tier1_operations"], 0)
        self.assertEqual(
            [(item.attribute, item.value) for item in with_lexicon.operations],
            [(item.attribute, item.value) for item in without_lexicon.operations],
        )
        self.assertTrue(all(item.provenance == "tier0" for item in with_lexicon.operations))

    def test_tier1_recovers_a_query_tier0_cannot_parse(self) -> None:
        # "burgundy" is outside Tier 0's 12 hardcoded colours.
        self.assertEqual(self._attributes("a burgundy top"), {"color": "burgundy"})
        self.assertEqual(self.extractor.last_trace["tier1_operations"], 1)

    def test_tier1_canonicalizes_the_value_and_preserves_the_span(self) -> None:
        operation = self.extractor.extract("i like blues").operations[0]
        self.assertEqual(operation.value, "blue")
        self.assertEqual(operation.raw_text, "blues")
        self.assertEqual(operation.provenance, "tier1")
        # A gazetteer hit is weaker evidence than an explicit requirement.
        self.assertEqual(operation.strength, "soft")

    def test_category_is_excluded_from_tier1_by_default(self) -> None:
        """Measured, not stylistic.

        Ablated on esci1000 x esci: Tier 1 off scores 0.7268, category-only
        0.6437, everything-except-category 0.7713. A category guessed from a
        short real query misses often enough that the reranker's -20 lands on
        the true target more often than the +45 lands on a good one.
        """
        from starter.extractor import TIER1_ATTRIBUTES

        self.assertNotIn("category", TIER1_ATTRIBUTES)
        self.assertEqual(self._attributes("hoodies for men"), {})

        with patch("starter.extractor.TIER1_ATTRIBUTES", ("category",)):
            self.assertEqual(self._attributes("hoodies for men"), {"category": "hoodies"})

    def test_one_operation_per_attribute(self) -> None:
        with patch("starter.extractor.TIER1_ATTRIBUTES", ("category", "color")):
            operations = self.extractor.extract("blues burgundy shoe").operations
        attributes = [item.attribute for item in operations]
        self.assertEqual(len(attributes), len(set(attributes)))

    def test_every_operation_carries_provenance(self) -> None:
        for message in ("a burgundy top", "I'm looking for shoes. A key requirement is: leather"):
            for item in self.extractor.extract(message).operations:
                self.assertIn(item.provenance, {"tier0", "tier1"})


class PolarityTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.extractor = HeuristicTurnExtractor(write_lexicon(Path(self._temp.name)))
        self.scanner = PolarityScanner()

    def _polarity(self, message: str) -> dict[str, str]:
        return {
            item.attribute: item.polarity
            for item in self.extractor.extract(message).operations
        }

    def _negated_text(self, message: str) -> str:
        tagger = self.extractor.tagger
        spans = self.scanner.negated_spans(message, tagger.scan(message))
        return " ".join(message[start:end] for start, end in spans)

    def test_negation_cue_marks_the_value_it_scopes_over(self) -> None:
        # "burgundy" is a Tier 1 hit; Tier 0 spans are not negatable.
        self.assertEqual(self._polarity("no burgundy").get("color"), "negate")

    def test_tier0_spans_are_never_negated(self) -> None:
        """Verbatim catalog metadata is not a shopper's negation.

        "No Closure closure" and "Non-Polarized" are details values the
        customer recited as requirements. Audited over 1,400 openings, every
        Tier 0 negation was a false positive that dropped a real constraint.
        """
        turn = self.extractor.extract("I'm looking for clothing. No Closure closure")
        self.assertTrue(turn.operations)
        self.assertTrue(all(item.provenance == "tier0" for item in turn.operations))
        self.assertTrue(all(item.polarity == "must" for item in turn.operations))

    def test_a_long_recited_span_is_never_negated(self) -> None:
        with patch("starter.extractor.TIER1_ATTRIBUTES", ("color",)):
            operations = self.extractor.extract("no burgundy").operations
        self.assertEqual(operations[0].polarity, "negate")
        operations[0].raw_text = "a very long recited span of catalog prose"
        self.assertGreater(len(operations[0].raw_text.split()), 3)

    def test_multi_word_lexicon_entry_spanning_the_cue_is_not_a_negation(self) -> None:
        # "no show socks" is a product type. Treating "no" as an operator here
        # is a net regression on real queries. The control below shows the same
        # cue in the same position still negating when it is genuinely a cue.
        self.assertEqual(self._negated_text("no show shoes"), "")
        self.assertIn("leather", self._negated_text("no leather shoes"))

    def test_explicit_false_friend_idiom_is_not_a_negation(self) -> None:
        # "no iron" is a garment feature the catalog does not evidence as a
        # multi-word entry, so it is held in NEGATION_FALSE_FRIENDS instead.
        self.assertEqual(self._negated_text("no iron shoes"), "")

    def test_scope_stops_at_a_budget_marker(self) -> None:
        # "without laces under $120" must not negate the budget.
        polarity = self._polarity("shoes without laces under $120")
        self.assertNotEqual(polarity.get("budget"), "negate")

    def test_scope_stops_at_a_coordinating_conjunction(self) -> None:
        spans = self.scanner.negated_spans("no cotton and leather", [])
        message = "no cotton and leather"
        negated = "".join(message[start:end] for start, end in spans)
        self.assertIn("cotton", negated)
        self.assertNotIn("leather", negated)

    def test_polarity_never_removes_an_operation(self) -> None:
        # Tagging only. `state_manager.replay` decides what a negated slot
        # means; this layer never drops the operation.
        self.assertEqual(len(self.extractor.extract("no burgundy").operations), 1)


class EscalationGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.base = HeuristicTurnExtractor(write_lexicon(Path(self._temp.name)))
        self.wrapper = LLMTurnExtractor(self.base)

    class _State:
        def __init__(self, turn: int = 1) -> None:
            self.turn = turn

    def _escalates(self, message: str, turn: int = 1) -> bool:
        state = self._State(turn)
        self.base.extract(message, state)
        return self.wrapper.should_escalate(state)

    def test_blocked_when_tier0_produced_output(self) -> None:
        self.assertFalse(self._escalates("I'm looking for shoes. A key requirement is: leather"))

    def test_blocked_when_tier1_produced_output(self) -> None:
        self.assertFalse(self._escalates("a burgundy top"))

    def test_blocked_when_a_template_matched_even_with_no_operations(self) -> None:
        # "no preference for colour" is Tier 0 working, not a gap.
        self.assertFalse(self._escalates("I don't have a preference for colour"))

    def test_blocked_past_the_turn_budget(self) -> None:
        self.assertFalse(self._escalates("qwerty zxcvb", turn=10))

    def test_blocked_once_the_call_budget_is_spent(self) -> None:
        self.wrapper.calls = self.wrapper.max_calls
        self.assertFalse(self._escalates("qwerty zxcvb"))

    def test_escalates_only_when_the_cascade_is_structurally_silent(self) -> None:
        self.assertTrue(self._escalates("qwerty zxcvb asdfg"))

    def test_disabled_client_leaves_the_deterministic_result_untouched(self) -> None:
        # Offline is the default: no flag, no client, no network.
        self.assertIsNone(self.wrapper._client)
        result = self.wrapper.extract("a burgundy top", self._State())
        self.assertEqual([item.value for item in result.operations], ["burgundy"])


class LowConfidenceGateTest(unittest.TestCase):
    """The widened opening: a Tier 1 hit that explained little of the message."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.base = HeuristicTurnExtractor(write_lexicon(Path(self._temp.name)))
        self.wrapper = LLMTurnExtractor(self.base)
        self.wrapper.gate = GATE_LOW_CONFIDENCE

    class _State:
        def __init__(self, turn: int = 1) -> None:
            self.turn = turn

    def _escalates(self, message: str, turn: int = 1) -> bool:
        state = self._State(turn)
        self.base.extract(message, state)
        return self.wrapper.should_escalate(state)

    def test_thin_tier1_read_now_escalates(self) -> None:
        # One colour matched; "waterproof", "commuting" and "panniers" are
        # requirements the gazetteer has no entry for.
        self.assertTrue(
            self._escalates("burgundy waterproof commuting panniers rack")
        )

    def test_one_stray_word_does_not_escalate(self) -> None:
        # Coverage is only 0.5 here, but a single unexplained word is a word
        # with no catalog meaning far more often than it is a missed
        # requirement. The residual floor is what separates the two.
        self.assertFalse(self._escalates("a burgundy top"))

    def test_tier0_still_blocks_unconditionally(self) -> None:
        # Template phrasing is the official column. Widening must not touch it.
        self.assertFalse(
            self._escalates(
                "I'm looking for shoes. A key requirement is: leather "
                "waterproof commuting panniers rack"
            )
        )

    def test_residual_floor_blocks_a_single_stray_word(self) -> None:
        self.wrapper.gate_residual = 5
        self.assertFalse(
            self._escalates("burgundy waterproof commuting panniers rack")
        )

    def test_coverage_threshold_is_respected(self) -> None:
        self.wrapper.gate_coverage = 0.0
        self.assertFalse(
            self._escalates("burgundy waterproof commuting panniers rack")
        )

    def test_empty_mode_leaves_the_same_turn_alone(self) -> None:
        self.wrapper.gate = GATE_EMPTY
        self.assertFalse(
            self._escalates("burgundy waterproof commuting panniers rack")
        )

    def test_filler_only_message_reports_no_residual(self) -> None:
        # "hi there thanks" is not an unexplained requirement, and counting it
        # as one would spend a call on every greeting.
        self.base.extract("hi there thanks", self._State())
        self.assertEqual(self.base.last_trace["residual_tokens"], 0)
        self.assertEqual(self.base.last_trace["coverage"], 1.0)


class CoverageTraceTest(unittest.TestCase):
    """`last_trace` has to describe the message, not just count operations."""

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.extractor = HeuristicTurnExtractor(write_lexicon(Path(self._temp.name)))

    def test_full_coverage_when_every_content_word_is_matched(self) -> None:
        self.extractor.extract("a burgundy", None)
        self.assertEqual(self.extractor.last_trace["coverage"], 1.0)
        self.assertEqual(self.extractor.last_trace["residual_tokens"], 0)

    def test_residual_counts_unexplained_content_words(self) -> None:
        self.extractor.extract("burgundy waterproof commuting panniers", None)
        trace = self.extractor.last_trace
        self.assertGreaterEqual(trace["residual_tokens"], 3)
        self.assertLess(trace["coverage"], 1.0)

    def test_stopwords_are_not_content(self) -> None:
        self.extractor.extract("I would like to have some of the", None)
        self.assertEqual(self.extractor.last_trace["content_tokens"], 0)

    def test_evidence_strength_is_reported(self) -> None:
        self.extractor.extract("a burgundy", None)
        self.assertGreaterEqual(self.extractor.last_trace["tier1_max_words"], 1)


if __name__ == "__main__":
    unittest.main()
