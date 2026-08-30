"""In-memory dense index over the frozen catalog.

The competition rules forbid an external vector database: everything runs in
process (`docs/competition_specification.md`, "must run entirely in-memory
for light execution"). At 50k products a brute-force matmul is not a
compromise -- one query against a (50000, 384) float32 matrix is a few
milliseconds and is exact, where an ANN structure would add a dependency, a
build step, and a recall cliff for nothing.

This module deliberately knows nothing about BM25, slots, or dialogue
policy. It answers one question -- "which catalog rows are nearest this
vector" -- so that the retrieval strategy built on top of it stays in
`starter/retriever.py` where the rest of the ranking lives.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from retrieval.document import build_query_document
from retrieval.embedder import (
    BACKEND_ENV,
    DEFAULT_BACKEND,
    DEFAULT_MODEL,
    MODEL_ENV,
    Embedder,
    get_embedder,
    l2_normalize,
)
from retrieval.store import DEFAULT_EMBEDDINGS_ROOT, ArtifactError, EmbeddingStore, artifact_dir


# Rows scored per matmul block. Bounds the temporary at roughly
# CHUNK_ROWS * dimension floats regardless of catalog size.
CHUNK_ROWS = 20_000


@dataclass(frozen=True)
class ScoredProduct:
    """One dense hit. `score` is a cosine similarity in [-1, 1]."""

    parent_asin: str
    score: float
    rank: int


class VectorIndex:
    """Exact cosine nearest-neighbour search over an `EmbeddingStore`."""

    def __init__(self, store: EmbeddingStore, embedder: Embedder | None = None) -> None:
        self.store = store
        self._embedder = embedder

    # ----------------------------------------------------------------
    # construction
    # ----------------------------------------------------------------

    @classmethod
    def load(
        cls,
        backend: str | None = None,
        model: str | None = None,
        root: str | Path = DEFAULT_EMBEDDINGS_ROOT,
        directory: str | Path | None = None,
        mmap: bool = False,
        lazy_embedder: bool = True,
    ) -> "VectorIndex":
        """Open the artifact for the configured backend.

        `lazy_embedder` defers constructing the encoder (and importing torch)
        until the first text query. Callers that only ever search with
        precomputed vectors -- "more like this", offline analysis -- never pay
        for the model at all.
        """

        if directory is None:
            probe = get_embedder(backend, model) if not lazy_embedder else None
            name = probe.name if probe is not None else _expected_embedder_name(backend, model)
            directory = artifact_dir(name, root)
            store = EmbeddingStore.load(directory, mmap=mmap)
            return cls(store, embedder=probe)
        return cls(EmbeddingStore.load(Path(directory), mmap=mmap), embedder=None)

    @classmethod
    def load_optional(cls, **kwargs) -> "VectorIndex | None":
        """Like `load`, but returns None when no artifact has been built yet.

        The agent must never fail to start because an optional dense route is
        missing -- official scoring may run on a checkout where nobody ran the
        build script.
        """
        try:
            return cls.load(**kwargs)
        except (ArtifactError, FileNotFoundError):
            return None

    @property
    def embedder(self) -> Embedder:
        """The encoder, constructed on first use if it was deferred."""
        if self._embedder is None:
            name = self.store.manifest.get("embedder", "")
            backend, _, model = name.partition(":")
            self._embedder = get_embedder(backend or None, model or None)
            if self._embedder.name != name:
                raise ArtifactError(
                    f"Encoder {self._embedder.name!r} does not match artifact {name!r}."
                )
        return self._embedder

    # ----------------------------------------------------------------
    # queries
    # ----------------------------------------------------------------

    def encode_query(self, text: str) -> np.ndarray:
        """Encode one query string into a unit vector."""
        vector = self.embedder.encode([text])[0]
        return np.asarray(vector, dtype=np.float32)

    def encode_state(self, state: dict) -> np.ndarray:
        """Encode a `StateManager.export()` dict directly.

        Keeping this here means callers never hand-assemble query text and so
        cannot drift away from the document format the catalog was built with.
        """
        return self.encode_query(
            build_query_document(
                search_query=state.get("search_query", ""),
                constraints=state.get("constraints") or {},
                raw_constraints=state.get("raw_constraints") or (),
                no_preference=state.get("no_preference") or (),
            )
        )

    def search(
        self,
        query: str | np.ndarray | Sequence[float],
        top_k: int = 50,
        candidates: Iterable[str] | None = None,
        min_score: float | None = None,
        exclude: Iterable[str] = (),
    ) -> list[ScoredProduct]:
        """Return the `top_k` nearest products, best first.

        `candidates` restricts the search to a subset -- pass the ids from a
        keyword route to rescore them densely instead of running an
        independent dense route. `exclude` drops ids already committed to an
        earlier slate.
        """

        vector = self._as_vector(query)
        excluded = {str(item) for item in exclude}

        if candidates is None:
            rows = None
            scores = self._scores_all(vector)
        else:
            rows = self.store.rows_of([str(item) for item in candidates])
            if rows.size == 0:
                return []
            scores = np.asarray(self.store.vectors[rows], dtype=np.float32) @ vector

        order = _top_indices(scores, top_k + len(excluded))
        results: list[ScoredProduct] = []
        for position in order:
            parent_asin = self.store.ids[int(rows[position]) if rows is not None else int(position)]
            if parent_asin in excluded:
                continue
            score = float(scores[position])
            if min_score is not None and score < min_score:
                break
            results.append(ScoredProduct(parent_asin, score, len(results) + 1))
            if len(results) >= top_k:
                break
        return results

    def similar_to(self, parent_asin: str, top_k: int = 10, **kwargs) -> list[ScoredProduct]:
        """Neighbours of a catalog product. Needs no encoder at all.

        Useful for browsing-track diversification and for eyeballing whether
        an artifact is sane before wiring it into ranking.
        """
        vector = self.store.vector_of(parent_asin)
        if vector is None:
            return []
        kwargs.setdefault("exclude", (parent_asin,))
        return self.search(vector, top_k=top_k, **kwargs)

    def score_pairs(self, query: str | np.ndarray, parent_asins: Sequence[str]) -> dict[str, float]:
        """Cosine similarity for specific products, unranked.

        The natural way to feed a dense feature into an existing linear
        scorer without letting it choose the candidate set.
        """
        vector = self._as_vector(query)
        scores: dict[str, float] = {}
        for parent_asin in parent_asins:
            row = self.store.row_of(str(parent_asin))
            if row is None:
                continue
            scores[str(parent_asin)] = float(
                np.asarray(self.store.vectors[row], dtype=np.float32) @ vector
            )
        return scores

    # ----------------------------------------------------------------
    # internals
    # ----------------------------------------------------------------

    def _as_vector(self, query: str | np.ndarray | Sequence[float]) -> np.ndarray:
        if isinstance(query, str):
            return self.encode_query(query)
        vector = np.asarray(query, dtype=np.float32).reshape(-1)
        if vector.shape[0] != self.store.dimension:
            raise ValueError(
                f"Query vector has {vector.shape[0]} dims, index has {self.store.dimension}"
            )
        # Callers may hand over an averaged or hand-built vector; normalizing
        # here is what keeps every returned score on one comparable scale.
        return l2_normalize(vector.reshape(1, -1))[0]

    def _scores_all(self, vector: np.ndarray) -> np.ndarray:
        total = len(self.store)
        scores = np.empty(total, dtype=np.float32)
        for start in range(0, total, CHUNK_ROWS):
            stop = min(start + CHUNK_ROWS, total)
            block = np.asarray(self.store.vectors[start:stop], dtype=np.float32)
            scores[start:stop] = block @ vector
        return scores


def _top_indices(scores: np.ndarray, count: int) -> np.ndarray:
    """Indices of the `count` largest scores, sorted descending."""
    count = max(0, min(int(count), scores.shape[0]))
    if count == 0:
        return np.empty(0, dtype=np.int64)
    if count >= scores.shape[0]:
        return np.argsort(-scores, kind="stable")
    # argpartition is O(n) and only the retained slice is fully sorted.
    partition = np.argpartition(-scores, count - 1)[:count]
    return partition[np.argsort(-scores[partition], kind="stable")]


def _expected_embedder_name(backend: str | None, model: str | None) -> str:
    """Artifact name for a backend without constructing the encoder."""
    backend = (backend or os.environ.get(BACKEND_ENV) or DEFAULT_BACKEND).strip()
    if backend == "hashing":
        # Mirrors HashingEmbedder defaults; cheap enough to just build it.
        return get_embedder("hashing").name
    return f"sentence-transformer:{model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL}"
