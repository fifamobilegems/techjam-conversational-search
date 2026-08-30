"""Pluggable text encoders.

Two backends ship here and they exist for different reasons.

`sentence-transformer`
    The quality backend. Downloads a model once, then runs fully offline --
    which matters because organizer policy may disable network access during
    official scoring (`docs/submission_rules.md`).

`hashing`
    A dependency-light, deterministic fallback: hashed character n-grams
    projected into a fixed space. It is meaningfully worse at paraphrase than
    a trained encoder, but it needs no model download and no torch, so the
    dense pipeline, its tests, and anyone iterating on fusion logic all keep
    working on a bare checkout.

Adding a third backend (an API embedder, a larger local model) means writing
one class with `name`, `dimension`, and `encode`, then registering it in
`BACKENDS`. Nothing downstream needs to change: the store records which
backend produced an artifact and refuses to mix them.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Iterable, Protocol, Sequence, runtime_checkable

import numpy as np


DEFAULT_BACKEND = "sentence-transformer"
DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# Environment overrides, so a teammate can switch backends for a whole run
# without editing code. `.env` is loaded by `starter.env.load_project_env`.
BACKEND_ENV = "EMBEDDING_BACKEND"
MODEL_ENV = "EMBEDDING_MODEL"
DEVICE_ENV = "EMBEDDING_DEVICE"

TOKEN_RE = re.compile(r"[a-z0-9]+")


@runtime_checkable
class Embedder(Protocol):
    """Minimal contract every backend satisfies."""

    @property
    def name(self) -> str:
        """Stable identifier recorded in the artifact manifest."""

    @property
    def dimension(self) -> int:
        """Length of one output vector."""

    def encode(self, texts: Sequence[str], batch_size: int = 64) -> np.ndarray:
        """Return an (len(texts), dimension) float32 array of unit vectors."""


def l2_normalize(matrix: np.ndarray) -> np.ndarray:
    """Scale rows to unit length so a dot product is a cosine similarity."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    # A zero row (empty document) would divide by zero; leave it at zero,
    # which scores 0.0 against every query rather than NaN.
    np.maximum(norms, 1e-12, out=norms)
    return (matrix / norms).astype(np.float32, copy=False)


class SentenceTransformerEmbedder:
    """Local transformer encoder via `sentence-transformers`.

    The import is deferred to construction time so that importing this module
    -- which the tests and the hashing path do -- never pays the torch import
    cost or fails on a checkout without torch installed.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL, device: str | None = None) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "The 'sentence-transformer' backend needs "
                "`pip install -r requirements-embeddings.txt`. "
                f"Use {BACKEND_ENV}=hashing for a dependency-light fallback."
            ) from error

        self.model_name = model_name
        device = device or os.environ.get(DEVICE_ENV) or None

        # Prefer the local cache. Left to itself the loader contacts the Hub
        # on every construction to revalidate the snapshot, which is a few
        # seconds on a good link and an unbounded stall on a bad or absent
        # one -- unacceptable inside a scored turn. The network path is kept
        # only for the one-time download on a machine that has never pulled
        # the model.
        try:
            self._model = SentenceTransformer(model_name, device=device, local_files_only=True)
        except Exception:  # noqa: BLE001 - any cache miss means "try the Hub"
            self._model = SentenceTransformer(model_name, device=device)
        # Renamed in sentence-transformers 5.x; support both so a routine
        # dependency bump does not break the build script.
        dimension = getattr(self._model, "get_embedding_dimension", None) or (
            self._model.get_sentence_embedding_dimension
        )
        self._dimension = int(dimension())

    @property
    def name(self) -> str:
        return f"sentence-transformer:{self.model_name}"

    @property
    def dimension(self) -> int:
        return self._dimension

    def encode(self, texts: Sequence[str], batch_size: int = 64) -> np.ndarray:
        vectors = self._model.encode(
            list(texts),
            batch_size=batch_size,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)


class HashingEmbedder:
    """Deterministic hashed n-gram encoder. No model, no download, no torch.

    Word tokens plus character 4-grams are hashed into a fixed number of
    buckets with a signed hash (the sign cancels collisions in expectation
    instead of letting them accumulate), then sub-linearly scaled and
    normalized. Character n-grams give it partial robustness to morphology
    ("boot" / "boots") that a pure bag of words lacks.

    Determinism matters: `blake2b` is used rather than `hash()`, whose seed
    changes every process, so an artifact built today still matches a query
    encoded tomorrow.
    """

    def __init__(self, dimension: int = 512, char_ngram: int = 4) -> None:
        self._dimension = int(dimension)
        self.char_ngram = int(char_ngram)

    @property
    def name(self) -> str:
        return f"hashing:{self._dimension}d:{self.char_ngram}gram"

    @property
    def dimension(self) -> int:
        return self._dimension

    def _bucket(self, term: str) -> tuple[int, float]:
        digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        return value % self._dimension, 1.0 if value & (1 << 63) else -1.0

    def _features(self, text: str) -> Iterable[str]:
        lowered = text.lower()
        tokens = TOKEN_RE.findall(lowered)
        yield from tokens
        for token in tokens:
            if len(token) <= self.char_ngram:
                continue
            for start in range(len(token) - self.char_ngram + 1):
                yield f"#{token[start:start + self.char_ngram]}"

    def encode(self, texts: Sequence[str], batch_size: int = 64) -> np.ndarray:
        matrix = np.zeros((len(texts), self._dimension), dtype=np.float32)
        for row, text in enumerate(texts):
            for term in self._features(text or ""):
                column, sign = self._bucket(term)
                matrix[row, column] += sign
        # Sub-linear scaling keeps one repeated term from dominating a row,
        # the same reason BM25 saturates term frequency.
        np.copysign(np.log1p(np.abs(matrix)), matrix, out=matrix)
        return l2_normalize(matrix)


BACKENDS = {
    "sentence-transformer": SentenceTransformerEmbedder,
    "hashing": HashingEmbedder,
}


def get_embedder(backend: str | None = None, model: str | None = None, **kwargs) -> Embedder:
    """Build the configured backend.

    Resolution order is argument, then environment, then default -- so a
    build script flag beats `.env`, and `.env` beats the shipped default.
    """

    backend = (backend or os.environ.get(BACKEND_ENV) or DEFAULT_BACKEND).strip()
    if backend not in BACKENDS:
        raise ValueError(f"Unknown embedding backend {backend!r}. Known: {sorted(BACKENDS)}")

    if backend == "sentence-transformer":
        return SentenceTransformerEmbedder(
            model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL, **kwargs
        )
    return HashingEmbedder(**kwargs)


def embedder_slug(name: str) -> str:
    """Filesystem-safe directory name for an artifact built by `name`."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
