"""Regression tests for the staged reranker (Role D, phases 6.1 and 7.1).

A separate file from ``test_retriever.py`` so the two roles' edits cannot
collide. The cases here are the *structural* guarantees — the ones whose whole
point is that no weight can trade them away — plus the two behaviours that are
correct but measure near zero on the benchmarks (negation, hard-constraint
staging) and would otherwise have nothing defending them.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from starter.retriever import (
    CatalogRetriever,
    RerankConfig,
    RerankWeights,
    _infer_department,
)


CATALOG = [
    {
        "parent_asin": "CHEAP",
        "title": "Plain hiking boot",
        "features": ["waterproof"],
        "description": ["hiking boot"],
        "price": 80.0,
        "categories": ["Shoes", "Boots"],
        "details": {"Department": "mens"},
        "average_rating": 4.0,
        "rating_number": 20,
        "store": "TrailCo",
    },
    {
        # Satisfies four constraints and violates one. Summed, its bonuses
        # (+45 category, +40 material, +35 color, +50 brand) outweigh the -60
        # budget penalty by a wide margin -- that is the trade staging removes.
        "parent_asin": "PRICEY",
        "title": "Black leather hiking boot",
        "features": ["waterproof", "leather"],
        "description": ["black leather hiking boot"],
        "price": 400.0,
        "categories": ["Shoes", "Boots"],
        "details": {"Department": "mens"},
        "average_rating": 4.9,
        "rating_number": 90000,
        "store": "TrailCo",
    },
    {
        "parent_asin": "LACED",
        "title": "Leather hiking boot with laces",
        "features": ["laces", "waterproof"],
        "description": ["lace up hiking boot"],
        "price": 100.0,
        "categories": ["Shoes", "Boots"],
        "details": {"Department": "mens"},
        "average_rating": 4.8,
        "rating_number": 9000,
        "store": "TrailCo",
    },
    {
        "parent_asin": "WOMENS",
        "title": "Leather hiking boot",
        "features": ["waterproof"],
        "description": ["hiking boot"],
        "price": 90.0,
        "categories": ["Shoes", "Boots"],
        "details": {"Department": "Womens"},
        "average_rating": 4.7,
        "rating_number": 40000,
        "store": "TrailCo",
    },
]


# Four satisfied, one violated -- the shape that beat the -60 penalty.
CONSTRAINTS = {
    "budget": "under $150",
    "category": "boots",
    "material": "leather",
    "color": "black",
    "brand": "TrailCo",
}


def build(directory: str, **config) -> CatalogRetriever:
    path = Path(directory) / "catalog.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in CATALOG), encoding="utf-8")
    return CatalogRetriever(path, config=RerankConfig(**config))


class HardConstraintStagingTest(unittest.TestCase):
    def test_budget_violation_cannot_be_outranked_by_relevance(self) -> None:
        """The bug the summed score allowed, stated as a test.

        PRICEY satisfies category, material, color and brand, and costs $400
        against a stated ceiling of $150. Its bonuses sum to far more than the
        -60 the violation costs, so under one summed score it wins. No amount
        of relevance may rescue it.
        """
        with tempfile.TemporaryDirectory() as directory:
            retriever = build(directory, staged=True)
            ranked = retriever.retrieve_and_rerank(
                "black leather hiking boot",
                CONSTRAINTS,
                top_k=4,
            )
            self.assertEqual(ranked[-1], "PRICEY")
            self.assertLess(ranked.index("CHEAP"), ranked.index("PRICEY"))
            scores = retriever.last_diagnostics["candidate_scores"]
            self.assertEqual(scores["PRICEY"]["violations"], 1)
            self.assertEqual(scores["CHEAP"]["violations"], 0)
            # ... and it really does win the score it is being denied.
            self.assertGreater(
                scores["PRICEY"]["final_score"], scores["CHEAP"]["final_score"]
            )

    def test_unstaged_scoring_lets_the_violator_win(self) -> None:
        """The same inputs without staging, to show the test has teeth."""
        with tempfile.TemporaryDirectory() as directory:
            retriever = build(directory, staged=False)
            ranked = retriever.retrieve_and_rerank(
                "black leather hiking boot",
                CONSTRAINTS,
                top_k=4,
            )
            self.assertLess(ranked.index("PRICEY"), ranked.index("CHEAP"))

    def test_absent_price_is_unknown_not_violated(self) -> None:
        """Sparse metadata must not be read as a contradiction."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.jsonl"
            row = {**CATALOG[0], "parent_asin": "NOPRICE"}
            row.pop("price")
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            retriever = CatalogRetriever(path, config=RerankConfig(staged=True))
            retriever.retrieve_and_rerank("hiking boot", {"budget": "under $150"}, top_k=1)
            scores = retriever.last_diagnostics["candidate_scores"]
            self.assertEqual(scores["NOPRICE"]["violations"], 0)
            # The old soft penalty still applies where the field is missing.
            self.assertLess(scores["NOPRICE"]["constraint_details"]["budget"], 0.0)


class NegationTest(unittest.TestCase):
    SPANS = [{
        "attribute": "feature",
        "match_phrase": "laces",
        "text": "laces",
        "weight": 1.0,
        "polarity": "negate",
        "strength": "hard",
        "superseded": False,
    }]

    def test_negated_span_is_excluded_and_stripped_from_the_query(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            retriever = build(directory, exclude_negated=True)
            ranked = retriever.retrieve_and_rerank(
                "hiking boots without laces", {}, raw_constraints=self.SPANS, top_k=4
            )
            diagnostics = retriever.last_diagnostics
            self.assertEqual(diagnostics["negated"], ["laces"])
            self.assertNotIn("laces", diagnostics["query_text"])
            self.assertEqual(ranked[-1], "LACED")

    def test_superseded_negation_is_ignored(self) -> None:
        """Replay marks overridden spans superseded; they stop being live."""
        with tempfile.TemporaryDirectory() as directory:
            retriever = build(directory, exclude_negated=True)
            spans = [{**self.SPANS[0], "superseded": True}]
            retriever.retrieve_and_rerank(
                "hiking boots without laces", {}, raw_constraints=spans, top_k=4
            )
            self.assertEqual(retriever.last_diagnostics["negated"], [])


class SoftConstraintTest(unittest.TestCase):
    """Role B's request: a Tier 1 gazetteer guess must not penalize a miss."""

    SPANS = [{
        "attribute": "color",
        "match_phrase": "turquoise",
        "text": "turquoise",
        "weight": 1.0,
        "polarity": "must",
        "strength": "soft",
        "provenance": "tier1",
        "superseded": False,
    }]

    def _color_score(self, soft_abstain: bool, spans: list[dict]) -> float:
        with tempfile.TemporaryDirectory() as directory:
            retriever = build(directory, soft_abstain=soft_abstain)
            retriever.retrieve_and_rerank(
                "hiking boot", {"color": "turquoise"}, raw_constraints=spans, top_k=4
            )
            scores = retriever.last_diagnostics["candidate_scores"]
            return scores["CHEAP"]["constraint_details"]["color"]

    def test_soft_miss_abstains(self) -> None:
        self.assertEqual(self._color_score(True, self.SPANS), 0.0)

    def test_soft_miss_penalizes_when_the_switch_is_off(self) -> None:
        self.assertLess(self._color_score(False, self.SPANS), 0.0)

    def test_hard_span_on_the_same_attribute_wins(self) -> None:
        """One recited value makes the attribute hard, whatever else tagged it."""
        spans = [self.SPANS[0], {**self.SPANS[0], "match_phrase": "teal", "strength": "hard"}]
        self.assertLess(self._color_score(True, spans), 0.0)


class DepartmentTest(unittest.TestCase):
    def test_cues_are_read_and_ambiguity_abstains(self) -> None:
        self.assertEqual(_infer_department("hoodies for men"), "mens")
        self.assertEqual(_infer_department("18w women evening gown"), "womens")
        self.assertIsNone(_infer_department("mens and womens matching sweaters"))
        self.assertIsNone(_infer_department("waterproof hiking boots"))

    def test_conflict_is_penalized_but_not_excluded_by_default(self) -> None:
        """Department is 87% covered and ~5% wrong, so it earns a penalty.

        Excluding on it costs more than mis-ranking on it: measured at -0.019
        technical on synth800/official.
        """
        with tempfile.TemporaryDirectory() as directory:
            retriever = build(directory)
            self.assertFalse(retriever.config.department_gate)
            retriever.retrieve_and_rerank("hiking boot for men", {}, top_k=4)
            scores = retriever.last_diagnostics["candidate_scores"]
            self.assertEqual(retriever.last_diagnostics["department"], "mens")
            self.assertEqual(scores["WOMENS"]["violations"], 0)
            self.assertLess(scores["WOMENS"]["constraint_details"]["department"], 0.0)
            # A matching department is neutral, not a bonus: `department_match`
            # defaults to 0.0 and stays a calibration knob rather than a thumb.
            self.assertEqual(scores["CHEAP"]["constraint_details"]["department"], 0.0)

    def test_hard_gate_excludes_when_explicitly_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            retriever = build(directory, department_gate=True, prefilter=False)
            retriever.retrieve_and_rerank("hiking boot for men", {}, top_k=4)
            scores = retriever.last_diagnostics["candidate_scores"]
            self.assertEqual(scores["WOMENS"]["violations"], 1)


class PrefilterTest(unittest.TestCase):
    def test_prefilter_never_empties_the_pool(self) -> None:
        """A filter that rejects everything must fall back, not return nothing."""
        with tempfile.TemporaryDirectory() as directory:
            retriever = build(directory, prefilter=True, department_gate=True, min_survivors=50)
            ranked = retriever.retrieve_and_rerank(
                "hiking boot", {"budget": "under $1"}, top_k=4
            )
            self.assertTrue(ranked)
            self.assertEqual(retriever.last_diagnostics["prefilter_removed"], 0)


class WeightsTest(unittest.TestCase):
    def test_defaults_round_trip_through_the_mapping(self) -> None:
        weights = RerankWeights()
        self.assertEqual(RerankWeights().with_values(weights.as_mapping()), weights)

    def test_a_weight_change_moves_the_score_it_names(self) -> None:
        """Calibration relies on this: score is linear in each named weight."""
        with tempfile.TemporaryDirectory() as directory:
            retriever = build(directory)
            plan = retriever.build_plan("hiking boot", {"category": "boots"})
            record = retriever.products["CHEAP"]
            stages = retriever.stage_contributions(record, plan)
            base = RerankWeights().as_mapping()
            doubled = {**base, "category_exact": base["category_exact"] * 2}
            first = retriever.assemble(stages, 0, base)["final_score"]
            second = retriever.assemble(stages, 0, doubled)["final_score"]
            self.assertAlmostEqual(second - first, base["category_exact"])


if __name__ == "__main__":
    unittest.main()
