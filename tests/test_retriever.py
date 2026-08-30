from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starter.retriever import CatalogRetriever


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
            explanation = retriever.last_diagnostics["candidate_scores"]["B"]
            self.assertIn("final_score", explanation)
            self.assertIn("material", explanation["constraint_details"])

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

    def test_boilerplate_is_neutral_and_demoted_raw_evidence_is_scored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {
                "RERANK_FILTER_BOILERPLATE": "1",
                "RERANK_RAW_PHRASES": "1",
            }):
                retriever = CatalogRetriever(write_catalog(Path(directory)))
            retriever.retrieve_and_rerank(
                "black boot imported",
                {"feature": "Imported", "category": "boots"},
                raw_constraints=[
                    {"attribute": "feature", "match_phrase": "warm lining", "weight": 0.4}
                ],
                top_k=3,
            )
            details = retriever.last_diagnostics["candidate_scores"]["B"]["constraint_details"]
            self.assertEqual(details["feature"], 0.0)
            self.assertEqual(details["raw:feature:warm lining"], 7.2)


if __name__ == "__main__":
    unittest.main()
