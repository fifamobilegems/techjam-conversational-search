from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from starter.extractor import HeuristicTurnExtractor
from starter.retriever import CatalogRetriever
from state.state_manager import AttributeUpdate, ExtractedTurn, StateManager


CATALOG_ROWS = [
    {
        "parent_asin": "A",
        "title": "Popular black polyester running shoe",
        "features": ["mesh upper", "lightweight"],
        "description": ["daily running shoe"],
        "price": 60.0,
        "categories": ["Clothing, Shoes & Jewelry", "Women", "Shoes", "Running"],
        "details": {"Department": "womens"},
        "average_rating": 4.9,
        "rating_number": 10000,
        "store": "FastFeet",
    },
    {
        "parent_asin": "B",
        "title": "Black leather winter boot",
        "features": ["genuine leather", "warm lining"],
        "description": ["winter boot for outdoor use"],
        "price": 90.0,
        "categories": ["Clothing, Shoes & Jewelry", "Women", "Shoes", "Boots"],
        "details": {"Department": "womens"},
        "average_rating": 4.2,
        "rating_number": 100,
        "store": "TrailCo",
    },
    {
        "parent_asin": "C",
        "title": "Blue cotton t-shirt",
        "features": ["100% cotton"],
        "description": ["soft casual shirt"],
        "price": 25.0,
        "categories": ["Clothing, Shoes & Jewelry", "Men", "Clothing", "Shirts"],
        "details": {"Department": "mens"},
        "average_rating": 4.6,
        "rating_number": 500,
        "store": "ShirtCo",
    },
]


def write_catalog(root: Path) -> Path:
    path = root / "catalog.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in CATALOG_ROWS), encoding="utf-8")
    return path


class RetrieverTest(unittest.TestCase):
    def test_hard_constraints_outrank_fuzzy_similarity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            retriever = CatalogRetriever(write_catalog(Path(directory)))
            result = retriever.retrieve_and_rerank(
                "black shoe",
                {"category": "boots", "material": "leather", "color": "black", "budget": "under $100"},
                top_k=3,
            )
            self.assertEqual(result[0], "B")

    def test_budget_penalizes_clearly_expensive_products(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            retriever = CatalogRetriever(write_catalog(Path(directory)))
            result = retriever.retrieve_and_rerank(
                "black footwear",
                {"budget": "under $50"},
                top_k=3,
            )
            self.assertNotEqual(result[0], "B")

    def test_no_preference_skips_constraint_penalty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            retriever = CatalogRetriever(write_catalog(Path(directory)))
            with_constraint = retriever.retrieve_and_rerank(
                "black shoe",
                {"brand": "TrailCo"},
                top_k=3,
            )
            without_constraint = retriever.retrieve_and_rerank(
                "black shoe",
                {"brand": "TrailCo"},
                no_preference=["brand"],
                top_k=3,
            )
            self.assertEqual(with_constraint[0], "B")
            self.assertEqual(without_constraint[0], "A")

    def test_returns_valid_unique_top_k_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            retriever = CatalogRetriever(write_catalog(Path(directory)))
            result = retriever.retrieve_and_rerank("", {}, top_k=10)
            self.assertLessEqual(len(result), 10)
            self.assertEqual(len(result), len(set(result)))
            self.assertTrue(set(result).issubset({"A", "B", "C"}))


class ExtractorTest(unittest.TestCase):
    def test_extracts_category_and_key_requirement(self) -> None:
        extracted = HeuristicTurnExtractor().extract(
            "I'm looking for Boots. A key requirement is: black leather."
        )
        self.assertEqual(extracted.intent, "buying")
        operations = [(item.attribute, item.action, item.value) for item in extracted.operations]
        self.assertIn(("category", "set", "Boots"), operations)
        self.assertIn(("material", "set", "black leather"), operations)

    def test_extracts_no_preference(self) -> None:
        extracted = HeuristicTurnExtractor().extract(
            "I don't have an additional preference for material."
        )
        operations = [(item.attribute, item.action, item.value) for item in extracted.operations]
        self.assertIn(("material", "no_preference", None), operations)

    def test_extracts_direct_clear(self) -> None:
        extracted = HeuristicTurnExtractor().extract("Please clear material.")
        operations = [(item.attribute, item.action, item.value) for item in extracted.operations]
        self.assertIn(("material", "clear", None), operations)


class WorkflowPolicyTest(unittest.TestCase):
    def test_i_need_black_running_shoes_retrieves(self) -> None:
        manager = StateManager()
        session_id = "s"
        state = manager.reset(session_id)
        extracted = HeuristicTurnExtractor().extract("I need black running shoes", state)

        manager.update(session_id, extracted, 1)
        exported = manager.export(session_id)

        self.assertEqual(exported["next_action"], "retrieve")
        self.assertEqual(exported["constraints"]["color"], "black")
        self.assertEqual(exported["constraints"]["category"], "running shoes")

    def test_exploring_shoes_clarifies_high_information_attribute(self) -> None:
        manager = StateManager()
        session_id = "s"
        state = manager.reset(session_id)
        extracted = HeuristicTurnExtractor().extract("I'm exploring shoes", state)

        manager.update(session_id, extracted, 1)
        exported = manager.export(session_id)

        self.assertEqual(exported["next_action"], "clarify")
        self.assertIn(exported["ask_attribute"], {"use_case", "size"})

    def test_override_replaces_material(self) -> None:
        manager = StateManager()
        session_id = "s"
        state = manager.reset(session_id)
        manager.update(
            session_id,
            ExtractedTurn(
                intent="buying",
                operations=[AttributeUpdate(attribute="material", action="set", value="leather")],
            ),
            1,
        )

        extracted = HeuristicTurnExtractor().extract(
            "Actually ignore leather; I need wool",
            state,
        )
        manager.update(session_id, extracted, 2)
        exported = manager.export(session_id)

        self.assertEqual(exported["constraints"]["material"], "wool")
        self.assertEqual(exported["next_action"], "retrieve")

    def test_no_preference_for_color_is_not_asked_again(self) -> None:
        manager = StateManager()
        session_id = "s"
        state = manager.reset(session_id)
        extracted = HeuristicTurnExtractor().extract("No preference for color", state)

        manager.update(session_id, extracted, 1)
        manager.mark_asked(session_id, "use_case")
        manager.mark_asked(session_id, "size")
        manager.mark_asked(session_id, "category")
        manager.mark_asked(session_id, "material")
        manager.mark_asked(session_id, "feature")
        manager.mark_asked(session_id, "budget")
        manager.mark_asked(session_id, "brand")
        manager.mark_asked(session_id, "style")
        manager.mark_asked(session_id, "other")

        self.assertIn("color", manager.export(session_id)["no_preference"])
        self.assertNotIn("color", manager.get_missing_attributes(session_id))
        self.assertIsNone(manager.choose_next_attribute(session_id))

    def test_never_asks_same_field_twice(self) -> None:
        manager = StateManager()
        session_id = "s"
        manager.reset(session_id)

        first = manager.choose_next_attribute(session_id)
        self.assertIsNotNone(first)
        manager.mark_asked(session_id, first)

        second = manager.choose_next_attribute(session_id)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
