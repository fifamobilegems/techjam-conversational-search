# Dense Retrieval Foundation

This document is the handover for the `retrieval/` package. It describes what
is built, what is deliberately **not** built, and how to add the dense route
to the agent without breaking the scored path.

## Status

| Piece | State |
| --- | --- |
| Product/query document format (`retrieval/document.py`) | done |
| Encoder backends + registry (`retrieval/embedder.py`) | done |
| Artifact format, manifest, staleness checks (`retrieval/store.py`) | done |
| In-memory cosine index (`retrieval/index.py`) | done |
| Build CLI (`scripts/build_embeddings.py`) | done |
| Diagnostics CLI (`scripts/probe_embeddings.py`) | done |
| **Dense route inside `CatalogRetriever`** | **not started -- see Work Items** |
| **Hybrid fusion / dense reranking** | **not started -- see Work Items** |
| **Intent-conditioned routing (buying vs browsing)** | **not started -- see Work Items** |

Nothing in `starter/` or `state/` imports `retrieval/` yet. The agent's
behaviour and its public-set score are byte-for-byte unchanged by this
foundation; every number in `docs/baseline_results.json` still reproduces.

## Build the artifact

```bash
pip install -r requirements-embeddings.txt
python -m scripts.build_embeddings
```

Measured at 4m38s for the 50,000-product catalog on an M-series laptop with
`EMBEDDING_DEVICE=mps --batch-size 64` (~180 products/s); plain CPU is slower
for the bulk build but *faster* for single queries at serving time, so set
`EMBEDDING_DEVICE=cpu` when running the agent. Output:

```text
data/embeddings/sentence-transformer-sentence-transformers-all-minilm-l6-v2/
    vectors.npy     50000 x 384 float16   (~38 MB)
    ids.json        row -> parent_asin
    manifest.json   encoder, dims, document version, catalog SHA256
```

The artifact is a **build product**: gitignored, reproducible from the frozen
catalog, and never committed. Whoever packages the submission must either
ship the directory as a local asset or run the build in setup -- say which in
the submission README, because official scoring may run without network.

No-torch alternative, useful in CI and on a laptop that will not host a 2 GB
install:

```bash
pip install -r requirements.txt
EMBEDDING_BACKEND=hashing python -m scripts.build_embeddings
```

It writes to its own directory, so both artifacts can coexist. It builds the
full catalog in 15 s and scores 0.950 on the oracle ceiling below against
MiniLM's 0.990 -- lower, as expected from hashed n-grams that cannot handle
paraphrase, but every API behaves identically, so fusion work developed
against it transfers unchanged.

## Check it before trusting it

```bash
python -m scripts.probe_embeddings --query "black leather ankle boots" --top-k 10
python -m scripts.probe_embeddings --ceiling
```

`--ceiling` builds an oracle query from each public session's hidden target
and reports how often the dense index surfaces that target in the top K. It
is the **upper bound** on what any dialogue policy built on this index can
achieve for the Coverage pillar. If it is weak at K=500, the problem is the
document format or the encoder, and no amount of fusion weighting will
recover it.

Current result, MiniLM artifact, 200 public sessions:

```text
  hit rate @10    0.990        by scenario @10:  buying           1.000
  hit rate @50    0.995                          intent_override  1.000
  hit rate @100   1.000                          browsing         0.988
  hit rate @500   1.000                          boundary         0.900
  median rank when found: 1
```

Read that carefully before celebrating. It says the target is **reachable** --
the index is not the bottleneck, and the remaining work is entirely in the
dialogue policy and the fusion. It does not say the agent will hit 0.99: the
oracle query is assembled from the target's own intent card, i.e. from
constraints the simulated customer will only disclose across several turns,
and some of them never at all. The gap between 0.990 here and the current
public score is the score the dialogue policy is leaving on the table.

## API

```python
from retrieval import VectorIndex

index = VectorIndex.load_optional()        # None when no artifact exists
if index is not None:
    hits = index.search("black leather ankle boots", top_k=50)
    hits[0].parent_asin, hits[0].score, hits[0].rank
```

| Call | Use |
| --- | --- |
| `index.search(query, top_k, candidates=, exclude=, min_score=)` | Nearest products. `query` is text or a vector. `candidates` restricts scoring to an id subset -- this is how you rescore a BM25 slate instead of running a second independent route. |
| `index.score_pairs(query, ids)` | Cosine for specific ids, unranked. Feed as one more term into the existing linear score without letting dense choose the candidate set. |
| `index.similar_to(parent_asin, top_k)` | Catalog neighbours; needs no encoder. Natural fit for browsing-track diversification. |
| `index.encode_state(state)` | Encode `StateManager.export()` directly. Prefer this over hand-built query strings -- it guarantees the query text matches the format the catalog was embedded with. |
| `index.encode_query(text)` | One unit vector, when you want to cache or blend vectors yourself. |

Scores are cosine similarities on unit vectors, so they are in `[-1, 1]` and
comparable across queries -- unlike the BM25 rank fusion score already in
`CatalogRetriever`, which is only meaningful within one query.

Measured on the full 50k artifact (M-series laptop):

| Step | Cost |
| --- | --- |
| Load artifact (`VectorIndex.load`) | 0.08 s, 38 MB resident |
| Construct the encoder, first use only | ~4.5 s from the local cache |
| Encode one query, `EMBEDDING_DEVICE=cpu` | 4 ms |
| Encode one query, `EMBEDDING_DEVICE=mps` | 29 ms -- MPS loses on batch-of-one |
| Exact search, 50k rows, top 50 | 3.5 ms |

So a dense turn costs about 8 ms, and a full 200-session public run adds
roughly 15 s. The encoder is constructed lazily on the first text query;
build it during `Agent.__init__` if you would rather not pay the one-off
model load inside a scored turn.

**Offline behaviour.** `SentenceTransformerEmbedder` loads with
`local_files_only=True` first and only contacts the Hub when the model is not
cached at all. That is deliberate: the default loader revalidates the
snapshot over the network on *every* construction, which is a few seconds on
a good link and an unbounded stall on a bad or blocked one -- a hazard inside
a scored run. Once `scripts/build_embeddings.py` has run on a machine, the
model is cached and nothing here needs the network again. Belt and braces for
a submission: export `HF_HUB_OFFLINE=1`.

## Where the dense route plugs in

`CatalogRetriever.retrieve_and_rerank` currently does:

```text
query text -> BM25 top 500 -> RRF score -> + constraint score -> + quality -> sort -> top 10
```

The seam is the candidate-generation block. Dense retrieval is a **second
route into the same RRF pool**, not a replacement for the constraint scorer,
which is what enforces hard filters (`brand` mismatch is -50, an out-of-budget
price is -60) and is the reason the Buying track works at all.

Sketch, for `starter/retriever.py`:

```python
def __init__(self, ..., dense_index=None):
    # Optional and lazy: a checkout with no artifact keeps today's behaviour.
    self.dense_index = dense_index if dense_index is not None else VectorIndex.load_optional()

def retrieve_and_rerank(self, ...):
    ...
    bm25_ids = self._bm25_search(query_text)
    for rank, parent_asin in enumerate(bm25_ids, start=1):
        ...  # unchanged

    if self.dense_index is not None:
        dense_hits = self.dense_index.search(query_text, top_k=self.candidate_limit)
        for hit in dense_hits:
            candidate = candidate_scores.setdefault(hit.parent_asin, {...})
            candidate["dense_rank"] = hit.rank
            candidate["dense_score"] = hit.score
            candidate["fusion_score"] += DENSE_WEIGHT * 100.0 / (60.0 + hit.rank)
```

Two things to preserve when you do this:

1. **`self.products` must contain every id you return.** `_sanitize` drops
   ids outside `valid_ids`, and `_constraint_score_details` indexes
   `self.products[parent_asin]`. Both hold for anything the index returns as
   long as the artifact was built from the same catalog -- the manifest
   checksum is there to catch the case where it was not.
2. **Do not let dense candidates skip the constraint scorer.** Every id in
   `candidate_scores` goes through it today; keep it that way, or budget and
   brand constraints stop being enforced on exactly the candidates that dense
   contributed.

## Work items

Roughly one pull request each, and 2/3/4 can proceed in parallel once 1 lands.

1. **Dense route into the candidate pool.** The sketch above, behind an env
   flag (`RETRIEVAL_DENSE=1`) so an A/B is one command. Report public-set
   TechnicalScore with and without.
2. **Fusion weighting.** `DENSE_WEIGHT` and the RRF constant `60.0` are
   guesses. Sweep them on the public set. Watch MRR specifically: the
   evaluator locks in the rank at the first hit, so a change that raises
   Hit Rate@10 while pushing the target from rank 2 to rank 6 is a net loss.
3. **Intent-conditioned routing.** `state["intent"]` and `state["scenario"]`
   already distinguish Buying from Browsing. Buying should stay filter-heavy
   (keyword precision, hard constraints); Browsing is where dense earns its
   keep -- cross-category scenario matching, plus `similar_to` for diversity.
   This is the Dual-Track Routing pillar and it is currently unimplemented.
4. **Dense rescoring of the BM25 slate.** Cheaper alternative to route 1:
   `index.score_pairs(query, bm25_ids)` as one extra term in the linear
   score. Sometimes beats full fusion because it cannot damage recall. Worth
   measuring against 1 rather than assuming.
5. **Query-side experiments.** `build_query_document` currently drops
   `no_preference` attributes and demotes superseded spans by ordering only.
   Weighted vector blending (encode active and demoted evidence separately,
   average with weights) is a natural next step and needs no rebuild.

## Rules that constrain the design

- **In-memory only.** No FAISS server, no external vector DB
  (`docs/competition_specification.md`). The brute-force matmul here is exact
  and fast enough at 50k rows; an ANN index would add a dependency and a
  recall cliff for no measurable gain.
- **The catalog is read-only.** Never write to `data/catalog.jsonl`, never
  invent ASINs. Artifacts go to `data/embeddings/`.
- **Offline-capable.** Scoring may run without network. Anything requiring a
  live API needs a documented fallback, and the token counts in the `usage`
  field must stay honest.
- **10 turns hard cap**, zero score past it.

## Rebuild triggers

Rebuild the artifact whenever any of these changes, or the loader will reject
it (by design -- a stale index fails quietly and expensively otherwise):

- `retrieval/document.py`, in which case also bump `DOCUMENT_VERSION`
- the encoder backend or model id
- the catalog file
