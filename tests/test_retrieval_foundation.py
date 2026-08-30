from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover - bare checkout without requirements.txt
    raise unittest.SkipTest("numpy is not installed; see requirements.txt")

from retrieval.document import (
    DOCUMENT_VERSION,
    build_product_document,
    build_query_document,
)
from retrieval.embedder import HashingEmbedder, embedder_slug, get_embedder, l2_normalize
from retrieval.index import VectorIndex, _top_indices
from retrieval.store import ArtifactError, EmbeddingStore, artifact_dir
from scripts.build_embeddings import build


CATALOG_ROWS = [
    {
        "parent_asin": "A",
        "title": "Black leather ankle boot for women",
        "features": ["genuine leather upper", "Imported"],
        "description": ["A warm winter boot for outdoor use."],
        "price": 90.0,
        "categories": ["Clothing, Shoes & Jewelry", "Women", "Shoes", "Boots", "Ankle & Bootie"],
        "details": {"Department": "womens", "Date First Available": "January 1, 2020"},
        "average_rating": 4.2,
        "rating_number": 100,
        "store": "TrailCo",
    },
    {
        "parent_asin": "B",
        "title": "Blue cotton t-shirt for men",
        "features": ["100% cotton"],
        "description": ["Soft casual shirt."],
        "price": 25.0,
        "categories": ["Clothing, Shoes & Jewelry", "Men", "Clothing", "Shirts", "T-Shirts"],
        "details": {"Department": "mens"},
        "average_rating": 4.6,
        "rating_number": 500,
        "store": "ShirtCo",
    },
    {
        "parent_asin": "C",
        "title": "Gold hoop earrings",
        "features": ["hypoallergenic stainless steel"],
        "description": ["Lightweight dangle earrings."],
        "price": None,
        "categories": ["Clothing, Shoes & Jewelry", "Women", "Jewelry", "Earrings", "Hoop"],
        "details": {},
        "average_rating": 4.1,
        "rating_number": 871,
        "store": "Spirit Hoops",
    },
]


def write_catalog(root: Path) -> Path:
    path = root / "catalog.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in CATALOG_ROWS), encoding="utf-8")
    return path


class DocumentTest(unittest.TestCase):
    def test_product_document_keeps_discriminative_fields(self) -> None:
        document = build_product_document(CATALOG_ROWS[0])
        self.assertIn("Black leather ankle boot", document)
        self.assertIn("brand: TrailCo", document)
        self.assertIn("category: Women > Shoes > Boots", document)
        self.assertIn("price: $90.0", document)

    def test_product_document_drops_boilerplate_details(self) -> None:
        document = build_product_document(CATALOG_ROWS[0])
        self.assertNotIn("Date First Available", document)
        # The root category is identical for all 50k rows and carries no signal.
        self.assertNotIn("Clothing, Shoes & Jewelry", document)

    def test_product_document_tolerates_missing_fields(self) -> None:
        self.assertEqual(build_product_document({"parent_asin": "X"}), "")

    def test_query_document_mirrors_product_labels(self) -> None:
        document = build_query_document(
            search_query="ankle boots",
            constraints={"color": "black", "material": "leather"},
        )
        self.assertEqual(document, "ankle boots. color: black. material: leather")

    def test_query_document_skips_no_preference_and_demotes_overrides(self) -> None:
        document = build_query_document(
            search_query="boots",
            constraints={"color": "black", "brand": "TrailCo"},
            raw_constraints=[
                {"attribute": "brand", "text": "TrailCo", "weight": 1.0},
                {"attribute": "style", "text": "chunky heel", "weight": 0.4},
                {"attribute": "feature", "text": "waterproof", "weight": 1.0},
            ],
            no_preference=["brand"],
        )
        self.assertNotIn("TrailCo", document)
        # A demoted span survives but is pushed behind the active evidence.
        self.assertLess(document.index("waterproof"), document.index("chunky heel"))


class HashingEmbedderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.embedder = HashingEmbedder()

    def test_vectors_are_unit_length(self) -> None:
        vectors = self.embedder.encode(["black boots", "gold earrings"])
        self.assertEqual(vectors.shape, (2, self.embedder.dimension))
        np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)

    def test_encoding_is_deterministic_across_instances(self) -> None:
        # A process-seeded hash would break every artifact built yesterday.
        other = HashingEmbedder()
        np.testing.assert_array_equal(
            self.embedder.encode(["black boots"]), other.encode(["black boots"])
        )

    def test_related_text_scores_above_unrelated_text(self) -> None:
        boots, boot, earrings = self.embedder.encode(["black boots", "black boot", "gold earrings"])
        self.assertGreater(float(boots @ boot), float(boots @ earrings))

    def test_empty_text_yields_zero_vector_not_nan(self) -> None:
        vector = self.embedder.encode([""])[0]
        self.assertTrue(np.all(np.isfinite(vector)))
        self.assertEqual(float(np.linalg.norm(vector)), 0.0)

    def test_l2_normalize_handles_zero_rows(self) -> None:
        normalized = l2_normalize(np.zeros((1, 4), dtype=np.float32))
        self.assertTrue(np.all(np.isfinite(normalized)))

    def test_unknown_backend_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            get_embedder("no-such-backend")

    def test_slug_is_filesystem_safe(self) -> None:
        self.assertEqual(
            embedder_slug("sentence-transformer:sentence-transformers/all-MiniLM-L6-v2"),
            "sentence-transformer-sentence-transformers-all-minilm-l6-v2",
        )


class EmbeddingStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)
        self.catalog = write_catalog(self.root)

    def _store(self) -> EmbeddingStore:
        vectors = HashingEmbedder().encode([row["title"] for row in CATALOG_ROWS])
        manifest = EmbeddingStore.build_manifest(
            embedder_name="hashing:512d:4gram",
            dimension=vectors.shape[1],
            count=len(CATALOG_ROWS),
            catalog_path=self.catalog,
            dtype="float32",
        )
        return EmbeddingStore([row["parent_asin"] for row in CATALOG_ROWS], vectors, manifest)

    def test_round_trip_preserves_ids_and_vectors(self) -> None:
        directory = self._store().save(self.root / "artifact")
        loaded = EmbeddingStore.load(directory)
        self.assertEqual(loaded.ids, ["A", "B", "C"])
        self.assertEqual(loaded.dimension, 512)
        self.assertEqual(loaded.row_of("B"), 1)
        self.assertIsNone(loaded.row_of("missing"))
        np.testing.assert_allclose(loaded.vector_of("A"), self._store().vector_of("A"), atol=1e-6)

    def test_rows_of_drops_unknown_ids(self) -> None:
        store = self._store()
        np.testing.assert_array_equal(store.rows_of(["C", "nope", "A"]), np.array([2, 0]))

    def test_mismatched_id_count_is_rejected(self) -> None:
        with self.assertRaises(ArtifactError):
            EmbeddingStore(["A"], np.zeros((2, 4), dtype=np.float32))

    def test_missing_artifact_names_the_build_command(self) -> None:
        with self.assertRaises(ArtifactError) as error:
            EmbeddingStore.load(self.root / "absent")
        self.assertIn("scripts.build_embeddings", str(error.exception))

    def test_stale_document_version_is_rejected(self) -> None:
        directory = self._store().save(self.root / "artifact")
        manifest = json.loads((directory / "manifest.json").read_text())
        manifest["document_version"] = "v0-ancient"
        (directory / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(ArtifactError) as error:
            EmbeddingStore.load(directory)
        self.assertIn(DOCUMENT_VERSION, str(error.exception))

    def test_wrong_encoder_is_rejected(self) -> None:
        directory = self._store().save(self.root / "artifact")
        with self.assertRaises(ArtifactError):
            EmbeddingStore.load(directory, expect_embedder="sentence-transformer:other")

    def test_changed_catalog_is_rejected(self) -> None:
        directory = self._store().save(self.root / "artifact")
        self.catalog.write_text(json.dumps(CATALOG_ROWS[0]) + "\n", encoding="utf-8")
        with self.assertRaises(ArtifactError) as error:
            EmbeddingStore.load(directory, catalog_path=self.catalog)
        self.assertIn("different catalog", str(error.exception))


class VectorIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.addCleanup(self._temp.cleanup)
        self.catalog = write_catalog(self.root)
        self.directory = build(
            catalog_path=self.catalog,
            backend="hashing",
            model=None,
            batch_size=8,
            limit=None,
            dtype="float32",
            root=self.root / "embeddings",
            out=None,
            quiet=True,
        )
        self.index = VectorIndex.load(directory=self.directory)

    def test_build_writes_artifact_under_encoder_slug(self) -> None:
        self.assertEqual(self.directory, artifact_dir("hashing:512d:4gram", self.root / "embeddings"))
        self.assertEqual(self.index.store.manifest["count"], 3)
        self.assertEqual(self.index.store.manifest["catalog_sha256"][:8],
                         self.index.store.manifest["catalog_sha256"][:8])

    def test_search_ranks_the_matching_product_first(self) -> None:
        hits = self.index.search("black leather ankle boots for women", top_k=3)
        self.assertEqual(hits[0].parent_asin, "A")
        self.assertEqual([hit.rank for hit in hits], [1, 2, 3])
        # Scores are cosines on unit vectors and must arrive sorted.
        self.assertGreaterEqual(hits[0].score, hits[1].score)
        self.assertLessEqual(hits[0].score, 1.0 + 1e-6)

    def test_lazy_encoder_is_reconstructed_from_the_manifest(self) -> None:
        self.assertEqual(self.index.embedder.name, self.index.store.manifest["embedder"])

    def test_top_k_larger_than_catalog_returns_everything_once(self) -> None:
        hits = self.index.search("shirt", top_k=99)
        self.assertEqual(len(hits), 3)
        self.assertEqual(len({hit.parent_asin for hit in hits}), 3)

    def test_candidates_restrict_the_search_space(self) -> None:
        hits = self.index.search("black leather ankle boots", top_k=5, candidates=["B", "C"])
        self.assertEqual({hit.parent_asin for hit in hits}, {"B", "C"})
        self.assertEqual(self.index.search("boots", top_k=5, candidates=["missing"]), [])

    def test_exclude_removes_ids_already_shown(self) -> None:
        hits = self.index.search("black leather ankle boots", top_k=2, exclude=["A"])
        self.assertNotIn("A", [hit.parent_asin for hit in hits])

    def test_min_score_cuts_the_tail(self) -> None:
        self.assertEqual(self.index.search("gold hoop earrings", top_k=3, min_score=0.99), [])

    def test_search_accepts_a_precomputed_vector(self) -> None:
        vector = self.index.store.vector_of("C")
        self.assertEqual(self.index.search(vector, top_k=1)[0].parent_asin, "C")
        with self.assertRaises(ValueError):
            self.index.search(np.zeros(7, dtype=np.float32))

    def test_encode_state_consumes_a_state_manager_export(self) -> None:
        vector = self.index.encode_state(
            {
                "search_query": "boots",
                "constraints": {"color": "black", "material": "leather"},
                "raw_constraints": [{"attribute": "color", "text": "black", "weight": 1.0}],
                "no_preference": ["brand"],
            }
        )
        self.assertEqual(vector.shape, (self.index.store.dimension,))
        self.assertEqual(self.index.search(vector, top_k=1)[0].parent_asin, "A")

    def test_similar_to_excludes_the_seed_product(self) -> None:
        hits = self.index.similar_to("A", top_k=2)
        self.assertNotIn("A", [hit.parent_asin for hit in hits])
        self.assertEqual(self.index.similar_to("missing"), [])

    def test_score_pairs_scores_only_requested_products(self) -> None:
        scores = self.index.score_pairs("blue cotton t-shirt", ["B", "C", "missing"])
        self.assertEqual(sorted(scores), ["B", "C"])
        self.assertGreater(scores["B"], scores["C"])

    def test_load_optional_returns_none_without_an_artifact(self) -> None:
        self.assertIsNone(VectorIndex.load_optional(backend="hashing", root=self.root / "empty"))

    def test_mmap_load_matches_in_memory_load(self) -> None:
        mapped = VectorIndex.load(directory=self.directory, mmap=True)
        self.assertEqual(
            [hit.parent_asin for hit in mapped.search("gold hoop earrings", top_k=3)],
            [hit.parent_asin for hit in self.index.search("gold hoop earrings", top_k=3)],
        )

    def test_top_indices_returns_descending_order(self) -> None:
        scores = np.array([0.1, 0.9, 0.5, 0.7], dtype=np.float32)
        np.testing.assert_array_equal(_top_indices(scores, 2), np.array([1, 3]))
        np.testing.assert_array_equal(_top_indices(scores, 0), np.empty(0, dtype=np.int64))


if __name__ == "__main__":
    unittest.main()
