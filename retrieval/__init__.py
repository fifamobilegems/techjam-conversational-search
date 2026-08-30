"""Dense-retrieval foundation for the shopping agent.

The pieces here build, persist, verify, and query catalog embeddings. They do
NOT decide what the agent retrieves -- no route is wired into
`starter/retriever.py` yet. See `docs/rag_foundation.md` for the integration
contract and the open work items.

    from retrieval import VectorIndex

    index = VectorIndex.load_optional()          # None until the artifact exists
    hits = index.search("black leather ankle boots", top_k=20)
"""

from retrieval.document import (
    DOCUMENT_VERSION,
    build_product_document,
    build_query_document,
)
from retrieval.embedder import (
    DEFAULT_BACKEND,
    DEFAULT_MODEL,
    Embedder,
    HashingEmbedder,
    SentenceTransformerEmbedder,
    embedder_slug,
    get_embedder,
    l2_normalize,
)
from retrieval.index import CHUNK_ROWS, ScoredProduct, VectorIndex
from retrieval.store import (
    DEFAULT_EMBEDDINGS_ROOT,
    ArtifactError,
    EmbeddingStore,
    artifact_dir,
    file_checksum,
)

__all__ = [
    "ArtifactError",
    "CHUNK_ROWS",
    "DEFAULT_BACKEND",
    "DEFAULT_EMBEDDINGS_ROOT",
    "DEFAULT_MODEL",
    "DOCUMENT_VERSION",
    "Embedder",
    "EmbeddingStore",
    "HashingEmbedder",
    "ScoredProduct",
    "SentenceTransformerEmbedder",
    "VectorIndex",
    "artifact_dir",
    "build_product_document",
    "build_query_document",
    "embedder_slug",
    "file_checksum",
    "get_embedder",
    "l2_normalize",
]
