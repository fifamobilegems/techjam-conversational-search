"""Build the catalog embedding artifact.

    python -m scripts.build_embeddings                    # default encoder
    python -m scripts.build_embeddings --backend hashing  # no torch needed
    python -m scripts.build_embeddings --limit 2000       # quick smoke build

The artifact is written to `data/embeddings/<encoder-slug>/` and is a build
product, not source: it is gitignored, reproducible from the frozen catalog,
and re-derived by anyone who clones the repo. The catalog itself is never
modified -- the competition forbids catalog mutation, so this reads it and
writes elsewhere.

Rebuild whenever `retrieval/document.py` changes; the manifest records the
document version and the loader refuses stale artifacts.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from pathlib import Path
from typing import Iterable, Iterator

import numpy as np

from retrieval.document import build_product_document
from retrieval.embedder import DEFAULT_BACKEND, get_embedder
from retrieval.store import DEFAULT_EMBEDDINGS_ROOT, EmbeddingStore, artifact_dir
from starter.env import load_project_env


DEFAULT_CATALOG = Path("data/catalog.jsonl")

# Rows held in memory before one encode call. Large enough to keep the
# encoder busy, small enough that progress reporting stays responsive.
SHARD_ROWS = 2048


def iter_catalog(path: Path) -> Iterator[dict]:
    """Stream the catalog. Transparently handles the shipped `.gz`."""
    if not path.exists():
        raise SystemExit(
            f"Catalog not found: {path}\n"
            "Decompress the release file first:  gzip -dk catalog.jsonl.gz "
            "&& mv catalog.jsonl data/catalog.jsonl"
        )
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, mode="rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _shards(rows: Iterable[dict], size: int) -> Iterator[list[dict]]:
    """Group rows into fixed-size batches for encoding."""
    shard: list[dict] = []
    for row in rows:
        shard.append(row)
        if len(shard) >= size:
            yield shard
            shard = []
    if shard:
        yield shard


def build(
    catalog_path: Path,
    backend: str,
    model: str | None,
    batch_size: int,
    limit: int | None,
    dtype: str,
    root: Path,
    out: Path | None,
    quiet: bool = False,
) -> Path:
    """Embed the catalog and write the artifact directory."""
    def report(message: str) -> None:
        """Print progress unless running quietly."""
        if not quiet:
            print(message, flush=True)

    embedder = get_embedder(backend, model)
    report(f"encoder: {embedder.name} ({embedder.dimension}d)")

    ids: list[str] = []
    blocks: list[np.ndarray] = []
    seen: set[str] = set()
    started = time.perf_counter()

    rows = iter_catalog(catalog_path)
    if limit is not None:
        rows = (row for _, row in zip(range(limit), rows))

    for shard in _shards(rows, SHARD_ROWS):
        documents: list[str] = []
        for product in shard:
            parent_asin = str(product["parent_asin"])
            # The catalog is keyed by parent_asin and the evaluator scores
            # unique ids, so a duplicate row would just waste a vector.
            if parent_asin in seen:
                continue
            seen.add(parent_asin)
            ids.append(parent_asin)
            documents.append(build_product_document(product))
        if not documents:
            continue
        blocks.append(embedder.encode(documents, batch_size=batch_size))
        elapsed = time.perf_counter() - started
        rate = len(ids) / elapsed if elapsed else 0.0
        report(f"  encoded {len(ids):>6} products  {rate:6.1f}/s")

    if not ids:
        raise SystemExit("Catalog produced no rows to embed.")

    vectors = np.vstack(blocks).astype(dtype, copy=False)
    manifest = EmbeddingStore.build_manifest(
        embedder_name=embedder.name,
        dimension=embedder.dimension,
        count=len(ids),
        catalog_path=catalog_path,
        dtype=dtype,
        extra={
            "build_seconds": round(time.perf_counter() - started, 1),
            "batch_size": batch_size,
            "limit": limit,
        },
    )
    directory = Path(out) if out else artifact_dir(embedder.name, root)
    # float16 halves the artifact at a cosine error around 1e-3, far below
    # the gaps that decide a ranking; search upcasts before the matmul.
    EmbeddingStore(ids=ids, vectors=vectors, manifest=manifest).save(directory)

    size_mb = sum(item.stat().st_size for item in directory.iterdir()) / 1e6
    report(
        f"wrote {len(ids)} x {embedder.dimension} {dtype} vectors to {directory} "
        f"({size_mb:.1f} MB) in {manifest['build_seconds']}s"
    )
    return directory


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point for the embedding build."""
    load_project_env()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--backend", default=None, help=f"default: ${{EMBEDDING_BACKEND}} or {DEFAULT_BACKEND}")
    parser.add_argument("--model", default=None, help="sentence-transformer model id")
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--limit", type=int, default=None, help="embed only the first N products")
    parser.add_argument("--dtype", choices=("float16", "float32"), default="float16")
    parser.add_argument("--root", type=Path, default=DEFAULT_EMBEDDINGS_ROOT)
    parser.add_argument("--out", type=Path, default=None, help="explicit artifact directory")
    args = parser.parse_args(argv)

    build(
        catalog_path=args.catalog,
        backend=args.backend,
        model=args.model,
        batch_size=args.batch_size,
        limit=args.limit,
        dtype=args.dtype,
        root=args.root,
        out=args.out,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
