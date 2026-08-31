# Shopping Copilot — Conversational Search Agent

TechJam 2026, Track 4. A conversational shopping agent that finds a customer's
intended product inside a frozen 50,000-item Amazon catalog within ten turns.

Built on the official participant kit. `evaluator/local_evaluator.py` and
`data/public_set.jsonl` are byte-identical to the organizer's copies — the
scoring contract is untouched.

## Project overview

The starter agent matches keywords. That works when the customer happens to use
the catalog's own words and fails completely when they do not, which is most of
the time: on real Amazon search queries the original pipeline produced an
**empty search query for 86% of messages**, because it built the query only from
template-matched slots and real phrasing matches no template.

The system addresses that with four layers.

**1. Extraction cascade** (`starter/extractor.py`)

| Tier | What it does | When it runs |
|---|---|---|
| Tier 0 | template regex, captures verbatim catalog metadata | always, authoritative |
| Tier 1 | lexicon tagging from a catalog-mined vocabulary | only when Tier 0 found nothing |
| Tier 2 | LLM extraction, schema-constrained and verbatim-guarded | only when both are silent |

Tier 1 exists because the original extractor hardcoded 12 colours and 10
materials against a catalog holding **1,165 colours and 463 materials**.
`scripts/build_lexicon.py` mines the vocabulary from catalog metadata into
`data/lexicon.json` (908 entries), so "burgundy" and "vegan leather" are
understood without anyone typing them into a list.

Tier 1 runs *only* when Tier 0 returns nothing. That single condition is what
keeps template phrasing untouched, and it is verified by a test.

A polarity layer marks negations ("wig **without** horns"). It is deliberately
narrow: measured over 1,400 messages, genuine product-attribute negation is 1
in 1,400, while naive negation detection fires on product *names* containing a
cue — "No Closure closure", "Non-Polarized", "no show socks" — and destroys
real constraints. Guards run before scope is computed.

**2. Event-sourced dialogue state** (`state/state_manager.py`)

An append-only event log; effective state is a replay. Handles incremental slot
accumulation and intent override, where a superseded preference is *demoted*
(weight 0.4) rather than deleted — in this dataset the old and new values of an
override describe the same target product, so the old one is still evidence.

**3. Retrieval and staged reranking** (`starter/retriever.py`)

BM25 over an in-memory SQLite FTS5 index (50k products, field-weighted), then
staged reranking so hard-constraint coverage cannot be traded away by lexical
relevance. Everything runs in-process; no vector database.

**4. Dense retrieval foundation** (`retrieval/`)

A pretrained sentence encoder over the catalog, 50k × 384, brute-force cosine
in memory. Built, tested, and **not currently wired into the scored path** —
see *Limitations*.

## Setup

Python 3.10+.

```bash
git clone <this-repo> && cd techjam-conversational-search
gzip -dkc catalog.jsonl.gz > data/catalog.jsonl     # 50,000 products
pip install -r requirements.txt                      # numpy only
```

The deterministic agent needs **no** further dependencies, no API key, and no
network access.

Optional extras:

```bash
pip install -r requirements-llm.txt          # Tier 2 LLM extraction
pip install -r requirements-embeddings.txt   # dense retrieval encoder
```

For Tier 2, copy `.env_example` to `.env` and set your own key. `.env` is
gitignored; **no credentials are committed anywhere in this repository.**

## Reproducing our results

```bash
python3 -m evaluator.local_evaluator          # official scorer, public 200
python3 -m unittest discover tests            # 198 tests
```

Rebuild the committed artifacts from the frozen catalog (both deterministic):

```bash
python3 -m scripts.build_lexicon              # -> data/lexicon.json
python3 -m scripts.build_embeddings           # -> data/embeddings/ (optional)
```

We also evaluate against paraphrased and real-query phrasings, to check the
agent is not merely fitted to the simulator's templates:

```bash
python3 -m tools.bench --limit 200            # 3 datasets x 3 phrasings
python3 -m scripts.measure_recall             # BM25 recall diagnostic
```

`tools/` and the extra datasets are ours, not the organizer's. `data/synth_set_800.jsonl`
and `data/esci_set_1000.jsonl` are held-out evaluation sets we generated to test
robustness; they are never used to fit anything.

### Environment variables

All optional; defaults give the scored deterministic path.

| Variable | Default | Effect |
|---|---|---|
| `TECHJAM_LLM_EXTRACTOR` | unset | `1` enables Tier 2 |
| `TECHJAM_LLM_MODEL` | `gpt-4o-mini` | Tier 2 model |
| `TECHJAM_LLM_MAX_CALLS` | `250` | per-process call ceiling |
| `RERANK_WEIGHTS` | `default` | `calibrated` uses swept weights |
| `CLARIFY_POLICY` | `other` | clarification attribute policy |
| `TIER1_ATTRIBUTES` | `color,material,size,style,brand` | which slots Tier 1 may set |
| `AGENT_DEBUG_LOG` | unset | writes turn-level traces |

## Model choice, cost, and latency

The scored path is **fully deterministic and offline**: standard library plus
numpy, zero tokens, no network. Every result above was produced that way.

Tier 2 is optional and additive — it may only add constraint spans the rules
missed, never delete one or change a decision. With it enabled (`gpt-4o-mini`),
measured demand is **0.06 calls/session** on template phrasing and **1.61** on
real queries, so a 200-session run costs single-digit dollars at most.

## Limitations, and what we would do next

**Dense retrieval is built but not connected.** `retrieval/` has a working
in-memory index with 25 tests, and the catalog embeddings build in about four
minutes — but no route in `retrieve_and_rerank` calls it. Our own measurements
say this is the largest remaining win: on real-query phrasing **20–28% of
sessions never surface the target at all**, and BM25 cannot bridge "warm winter
coat" to "insulated fleece parka" because they share no word. Wiring the dense
channel into the existing reciprocal-rank fusion is the first thing we would do.

**Intent is detected but not acted on.** The extractor classifies buying vs
browsing and the state manager publishes it, but both tracks then run an
identical pipeline. The brief's dual-track routing is therefore only half
delivered: we know the intent, we do not yet branch on it.

**Efficiency is our weakest metric.** MTTC is 2.4 turns on the official public
set but 4.7–5.3 on harder phrasings, partly because a miss is scored as turn 11.
Fixing recall would improve this without touching the dialogue policy.

**The lexicon's filters are hand-tuned.** The 908 vocabulary entries are mined
from the catalog, but roughly 150 words of stoplists that clean them were
written by hand against this catalog. They are small and legible, but they are
fitted to this data.

**Tier 1 cannot yet set `category`.** Measured on real queries, guessing a
category *hurt* (0.6437 against 0.7268 with Tier 1 off) because the reranker
penalises a miss by −20, and a category guessed from a short query is often
right in spirit and wrong in wording. It is one environment variable away from
returning once soft constraints abstain rather than penalise.

## Team contributions

| Area | Work |
|---|---|
| Harness & evaluation | bench matrix across 3 datasets × 3 phrasings, ESCI simulator, recall diagnostic |
| Extraction / NLU | lexicon miner, Tier 0/1/2 cascade, polarity layer, LLM escalation gate |
| State & policy | event-sourced state, replay, clarification policy, emit policy |
| Retrieval | staged reranking, hard-constraint handling, weight calibration, dense foundation |

## Data and attribution

Catalog and sessions derive from Amazon Reviews 2023 (McAuley Lab, UCSD). See
`DATA_ATTRIBUTION.md`. The catalog is read-only: nothing here mutates it or
injects ASINs.
