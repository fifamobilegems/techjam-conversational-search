from __future__ import annotations

import gzip
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


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

FIELD_WEIGHTS = (0.0, 6.0, 5.0, 4.0, 2.5, 3.0, 1.0, 1.5)


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


def _match_strength(value: object, text: str) -> float:
    needle = _normalized_text(str(value))
    if not needle:
        return 0.0
    haystack = _normalized_text(text)
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


class CatalogRetriever:
    def __init__(self, catalog_path: str | Path = "data/catalog.jsonl", candidate_limit: int = 500) -> None:
        self.catalog_path = Path(catalog_path)
        self.candidate_limit = candidate_limit
        self.connection = sqlite3.connect(":memory:")
        self.products: dict[str, ProductRecord] = {}
        self.valid_ids: set[str] = set()
        self.popular_ids: list[str] = []
        self._build()

    def retrieve_and_rerank(
        self,
        search_query: str,
        constraints: dict,
        no_preference: Iterable[str] = (),
        top_k: int = 10,
    ) -> list[str]:
        no_preference_set = {str(item) for item in no_preference}
        active_constraints = {
            str(key): value
            for key, value in constraints.items()
            if key not in no_preference_set and value not in (None, "", [])
        }
        query_text = self._query_text(search_query, active_constraints)
        candidate_scores: dict[str, dict[str, float | int | None]] = {}

        for rank, parent_asin in enumerate(self._bm25_search(query_text), start=1):
            candidate = candidate_scores.setdefault(
                parent_asin,
                {"bm25_rank": None, "fusion_score": 0.0, "constraint_score": 0.0, "quality_score": 0.0},
            )
            candidate["bm25_rank"] = rank
            candidate["fusion_score"] = float(candidate["fusion_score"]) + 100.0 / (60.0 + rank)

        self._add_popular_backfill(candidate_scores)

        ranked: list[tuple[str, float]] = []
        for parent_asin, scores in candidate_scores.items():
            record = self.products[parent_asin]
            constraint_score = self._constraint_score(record, active_constraints)
            quality_score = self._quality_score(record)
            final_score = float(scores["fusion_score"]) + constraint_score + quality_score
            scores["constraint_score"] = constraint_score
            scores["quality_score"] = quality_score
            ranked.append((parent_asin, final_score))

        ranked.sort(key=lambda item: item[1], reverse=True)
        return self._sanitize([parent_asin for parent_asin, _ in ranked], top_k)

    def _build(self) -> None:
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
        details = _text(product.get("details"))
        store = _text(product.get("store"))
        description = _text(product.get("description"))
        all_text = " ".join(
            part
            for part in (title, categories, features, details, store, description, price_text)
            if part
        )
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
        )

    def _query_text(self, search_query: str, constraints: dict) -> str:
        parts = [search_query]
        parts.extend(str(value) for value in constraints.values() if value not in (None, "", []))
        return " ".join(part for part in parts if part).strip()

    def _bm25_search(self, query_text: str) -> list[str]:
        terms = list(dict.fromkeys(_terms(query_text)))[:50]
        if not terms:
            return []
        expression = " OR ".join(f'"{term}"' for term in terms)
        weights = ", ".join(str(weight) for weight in FIELD_WEIGHTS)
        rows = self.connection.execute(
            f"SELECT parent_asin FROM products WHERE products MATCH ? "
            f"ORDER BY bm25(products, {weights}) LIMIT ?",
            (expression, self.candidate_limit),
        ).fetchall()
        return [str(row[0]) for row in rows]

    def _add_popular_backfill(self, candidate_scores: dict[str, dict[str, float | int | None]]) -> None:
        needed = max(0, min(self.candidate_limit, 50) - len(candidate_scores))
        if needed == 0:
            return
        added = 0
        for rank, parent_asin in enumerate(self.popular_ids, start=1):
            if parent_asin in candidate_scores:
                continue
            candidate_scores[parent_asin] = {
                "bm25_rank": None,
                "fusion_score": 0.2 / rank,
                "constraint_score": 0.0,
                "quality_score": 0.0,
            }
            added += 1
            if added >= needed:
                break

    def _constraint_score(self, record: ProductRecord, constraints: dict) -> float:
        score = 0.0
        for attribute, value in constraints.items():
            if attribute == "category":
                score += self._category_score(record, value)
            elif attribute == "brand":
                score += self._brand_score(record, value)
            elif attribute == "material":
                score += self._word_constraint_score(record, value, MATERIALS, 40.0)
            elif attribute == "color":
                score += self._word_constraint_score(record, value, COLORS, 35.0)
            elif attribute == "size":
                score += self._generic_constraint_score(record, value, 35.0)
            elif attribute == "budget":
                score += self._budget_score(record, value)
            elif attribute == "feature":
                score += self._generic_constraint_score(record, value, 25.0)
            elif attribute == "use_case":
                score += self._generic_constraint_score(record, value, 25.0)
            elif attribute == "style":
                score += self._generic_constraint_score(record, value, 20.0)
            elif attribute == "other":
                score += self._generic_constraint_score(record, value, 22.0)
        return score

    def _category_score(self, record: ProductRecord, value: object) -> float:
        category_strength = _match_strength(value, f"{record.categories} {record.title} {record.details}")
        if category_strength >= 0.8:
            return 45.0
        if category_strength >= 0.5:
            return 25.0
        if category_strength > 0:
            return 10.0
        return -20.0

    def _brand_score(self, record: ProductRecord, value: object) -> float:
        store_strength = _match_strength(value, record.store)
        full_strength = _match_strength(value, f"{record.store} {record.title} {record.details}")
        if store_strength >= 0.8:
            return 50.0
        if full_strength >= 0.8:
            return 30.0
        if full_strength > 0:
            return 12.0
        return -50.0

    def _word_constraint_score(
        self,
        record: ProductRecord,
        value: object,
        vocabulary: Iterable[str],
        boost: float,
    ) -> float:
        explicit_word = _first_word_match(vocabulary, value)
        if explicit_word:
            if re.search(rf"\b{re.escape(explicit_word)}\b", record.all_text, flags=re.IGNORECASE):
                return boost
            return -20.0
        return self._generic_constraint_score(record, value, boost)

    def _generic_constraint_score(self, record: ProductRecord, value: object, boost: float) -> float:
        strength = _match_strength(value, record.all_text)
        if strength >= 0.8:
            return boost
        if strength >= 0.5:
            return boost * 0.55
        if strength > 0:
            return boost * 0.2
        return -12.0

    def _budget_score(self, record: ProductRecord, value: object) -> float:
        numbers = _budget_numbers(value)
        if not numbers:
            return 0.0
        if record.price is None:
            return -8.0

        lowered = str(value).lower()
        amount = max(numbers)
        if any(marker in lowered for marker in ("under", "below", "less than", "<", "<=", "max")):
            return 30.0 if record.price <= amount else -60.0
        if "between" in lowered and len(numbers) >= 2:
            low, high = min(numbers), max(numbers)
            return 30.0 if low <= record.price <= high else -60.0
        if any(marker in lowered for marker in ("around", "about", "budget")):
            return 30.0 if record.price <= amount * 1.25 else -35.0
        return 15.0 if record.price <= amount * 1.5 else -20.0

    def _quality_score(self, record: ProductRecord) -> float:
        rating_bonus = max(0.0, record.rating - 3.5) * 0.4
        popularity_bonus = min(2.0, math.log1p(record.rating_count) / 6.0)
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
