"""On-disk embedding artifacts and their provenance.

An artifact is a directory:

    data/embeddings/<slug>/
        vectors.npy    (n, dim) unit-normalized, float16 or float32
        ids.json       row -> parent_asin, same order as vectors.npy
        manifest.json  what produced it and from what

The manifest is the point of this module. Dense retrieval fails quietly: a
vector file built from a different document format, a different encoder, or a
different catalog still loads, still returns ten products, and still looks
plausible while scoring badly. Every load therefore re-checks the encoder
name, the document version, the row count, and the catalog checksum, and
raises rather than serving a mismatched index.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from retrieval.document import DOCUMENT_VERSION
from retrieval.embedder import embedder_slug


DEFAULT_EMBEDDINGS_ROOT = Path("data/embeddings")

VECTORS_FILE = "vectors.npy"
IDS_FILE = "ids.json"
MANIFEST_FILE = "manifest.json"

MANIFEST_VERSION = 1


class ArtifactError(RuntimeError):
    """Raised when an embedding artifact is missing, corrupt, or stale."""


def file_checksum(path: str | Path, chunk_size: int = 1 << 20) -> str:
    """SHA256 of a file, streamed -- the catalog is too large to slurp."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_dir(embedder_name: str, root: str | Path = DEFAULT_EMBEDDINGS_ROOT) -> Path:
    """Where the artifact for a given encoder lives.

    Keying the directory on the encoder means two backends can coexist on
    one checkout and a rebuild never silently overwrites the other one.
    """
    return Path(root) / embedder_slug(embedder_name)


@dataclass
class EmbeddingStore:
    """An id-aligned matrix of unit vectors plus the manifest that describes it."""

    ids: list[str]
    vectors: np.ndarray
    manifest: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate shape and build the id -> row lookup."""
        if self.vectors.ndim != 2:
            raise ArtifactError(f"vectors must be 2-D, got shape {self.vectors.shape}")
        if len(self.ids) != self.vectors.shape[0]:
            raise ArtifactError(
                f"{len(self.ids)} ids for {self.vectors.shape[0]} vectors -- artifact is corrupt"
            )
        self._row_by_id = {parent_asin: row for row, parent_asin in enumerate(self.ids)}

    # ----------------------------------------------------------------
    # lookup
    # ----------------------------------------------------------------

    def __len__(self) -> int:
        """Number of products in the store."""
        return len(self.ids)

    @property
    def dimension(self) -> int:
        """Length of one stored vector."""
        return int(self.vectors.shape[1])

    def row_of(self, parent_asin: str) -> int | None:
        """Row index for a product id, or None if absent."""
        return self._row_by_id.get(parent_asin)

    def vector_of(self, parent_asin: str) -> np.ndarray | None:
        """Vector for a product id, or None if absent."""
        row = self.row_of(parent_asin)
        return None if row is None else np.asarray(self.vectors[row], dtype=np.float32)

    def rows_of(self, parent_asins: list[str]) -> np.ndarray:
        """Row indices for a candidate subset; unknown ids are dropped."""
        return np.fromiter(
            (row for row in (self._row_by_id.get(item) for item in parent_asins) if row is not None),
            dtype=np.int64,
        )

    # ----------------------------------------------------------------
    # persistence
    # ----------------------------------------------------------------

    @classmethod
    def build_manifest(
        cls,
        embedder_name: str,
        dimension: int,
        count: int,
        catalog_path: str | Path | None = None,
        dtype: str = "float16",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Describe an artifact: encoder, dimensions, document format, catalog checksum.

        Dense retrieval fails quietly, so provenance is recorded at build time
        and re-checked on every load.
        """
        manifest: dict[str, Any] = {
            "manifest_version": MANIFEST_VERSION,
            "embedder": embedder_name,
            "dimension": int(dimension),
            "count": int(count),
            "dtype": dtype,
            "document_version": DOCUMENT_VERSION,
            "normalized": True,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if catalog_path is not None:
            path = Path(catalog_path)
            manifest["catalog_path"] = str(path)
            manifest["catalog_sha256"] = file_checksum(path)
            manifest["catalog_bytes"] = path.stat().st_size
        if extra:
            manifest.update(extra)
        return manifest

    def save(self, directory: str | Path) -> Path:
        """Write vectors, ids and manifest to a directory."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        np.save(directory / VECTORS_FILE, self.vectors, allow_pickle=False)
        (directory / IDS_FILE).write_text(json.dumps(self.ids), encoding="utf-8")
        (directory / MANIFEST_FILE).write_text(
            json.dumps(self.manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return directory

    @classmethod
    def load(
        cls,
        directory: str | Path,
        mmap: bool = False,
        expect_embedder: str | None = None,
        catalog_path: str | Path | None = None,
    ) -> "EmbeddingStore":
        """Read an artifact and refuse anything that does not match.

        `mmap` leaves the matrix on disk and pages it in, which keeps startup
        cheap; the search path upcasts whatever slice it touches, so this is
        purely a memory/latency trade and never changes results.

        `catalog_path` is optional and only checked when supplied -- verifying
        the checksum re-reads 50k rows, which is not worth doing on every
        agent construction.
        """

        directory = Path(directory)
        vectors_path = directory / VECTORS_FILE
        ids_path = directory / IDS_FILE
        manifest_path = directory / MANIFEST_FILE

        missing = [path.name for path in (vectors_path, ids_path, manifest_path) if not path.exists()]
        if missing:
            raise ArtifactError(
                f"Embedding artifact at {directory} is missing {', '.join(missing)}. "
                "Build it with: python -m scripts.build_embeddings"
            )

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        ids = json.loads(ids_path.read_text(encoding="utf-8"))
        vectors = np.load(vectors_path, mmap_mode="r" if mmap else None, allow_pickle=False)

        if expect_embedder and manifest.get("embedder") != expect_embedder:
            raise ArtifactError(
                f"Artifact at {directory} was built by {manifest.get('embedder')!r}, "
                f"but {expect_embedder!r} is configured. Rebuild it or switch backends."
            )
        if manifest.get("document_version") != DOCUMENT_VERSION:
            raise ArtifactError(
                f"Artifact at {directory} uses document format "
                f"{manifest.get('document_version')!r}; this checkout produces "
                f"{DOCUMENT_VERSION!r}. Rebuild with: python -m scripts.build_embeddings"
            )
        if catalog_path is not None:
            recorded = manifest.get("catalog_sha256")
            actual = file_checksum(catalog_path)
            if recorded and recorded != actual:
                raise ArtifactError(
                    f"Artifact at {directory} was built from a different catalog "
                    f"({recorded[:12]}... vs {actual[:12]}...). Rebuild it."
                )

        return cls(ids=list(ids), vectors=vectors, manifest=manifest)
