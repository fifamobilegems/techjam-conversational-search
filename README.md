# Conversational E-Commerce Search Agent — TechJam 2026, Track 4

A stateful conversational retrieval agent that finds a shopper's hidden target
product in a frozen 50,000-item catalog within 10 turns.

**TechnicalScore `0.8427` on the 200-session public set — 7.9× the provided
BM25 baseline (`0.1067`).** Zero tokens, zero API cost, ~65 ms per turn, and the
scored path runs on the Python standard library alone.

| | HR@10 | MRR | MTTC | Efficiency | TechnicalScore |
|---|--:|--:|--:|--:|--:|
| **This agent** | 0.9500 | 0.6840 | 2.875 | 0.8125 | **0.8427** |
| Provided baseline | 0.1250 | 0.0680 | 9.810 | 0.1190 | 0.1067 |

---

## Project overview

The starter agent treats each turn as a fresh keyword search. That loses the two
things a shopping conversation is actually made of: constraints accumulate, and
sometimes they get retracted. We built the agent around those two facts.

**1. Extraction is a cascade, not a parser.** Three tiers run in order and stop
as soon as one produces something. Tier 0 is a template matcher for the
structured phrasing the simulator uses. Tier 1 is a gazetteer mined from the
catalog itself — 107 colours and 107 materials against the 12 and 10 a hand-written
list would carry — which catches free-text phrasing that no template matches.
Tier 2 is an optional LLM, off by default, gated on both earlier tiers returning
nothing. A separate polarity layer marks what the shopper *rejected*, so "without
underwire" removes a term from the query instead of searching for it.

**2. State is an append-only event log.** Every disclosure is an immutable event;
the constraint set is *replayed* from that log rather than mutated in place. This
is what makes intent overrides correct: "actually, ignore my earlier preference"
appends a superseding event, so the old constraint drops out of the active set
but survives as weak evidence at 0.4 weight — because an overridden preference
still describes the same target product.

**3. Retrieval is staged so hard constraints cannot be outvoted.** BM25 over
SQLite FTS5 generates candidates, a prefilter drops determinate violators before
the candidate budget is spent, and reranking runs in four ordered stages: hard
constraint coverage, then lexical relevance, then soft preferences, then the user
profile as a pure tie-break. A hard-match floor reserves 2 of the 10 returned
slots for the strongest lexical matches, because a product BM25 ranks first can
otherwise finish outside the Top-10 on an unremarkable constraint total.

### What we think is the interesting part

We built a second and third customer simulator to attack our own agent.

The official simulator has the shopper recite the target product's own catalog
metadata verbatim. An agent tuned only against it learns to exploit that, and we
could not tell from a single score whether we had built a shopping agent or a
template matcher. So we wrote `realistic` (a paraphrasing shopper) and `esci`
(opening turns taken verbatim from real Amazon shopping queries in the ESCI
Shopping Queries dataset), and we score every change against all three.

An early version scored 0.87 on official phrasing and **0.04** on real queries.
That gap was the single most useful number in the project, and closing it drove
almost every architectural decision that followed.

| dataset × simulator | n | HR@10 | MRR | MTTC | TechnicalScore |
|---|--:|--:|--:|--:|--:|
| public200 × official | 200 | 0.9500 | 0.6840 | 2.875 | **0.8427** |
| public200 × realistic | 200 | 0.9950 | 0.7084 | 2.520 | 0.8796 |
| synth800 × official | 200 | 0.9700 | 0.7491 | 2.600 | 0.8777 |
| synth800 × realistic | 200 | 1.0000 | 0.7981 | 2.295 | 0.9135 |
| esci1000 × official | 200 | 0.9500 | 0.7909 | 2.785 | 0.8766 |
| esci1000 × realistic | 200 | 0.9950 | 0.7257 | 2.415 | 0.8869 |
| **esci1000 × esci** (real queries) | 200 | 0.9850 | 0.6818 | 2.575 | **0.8655** |
| | | | | **mean** | **0.8775** |

1,400 sessions. The spread across the three phrasings is now 0.04, not 0.83.

---

## Setup

**Python 3.10+** (developed and measured on **3.13.9**).

```bash
git clone https://github.com/fifamobilegems/techjam-conversational-search.git
cd techjam-conversational-search
```

### The catalog

Not committed — download `catalog.jsonl.gz` from the GitHub Release, then:

```bash
gzip -dkc catalog.jsonl.gz > data/catalog.jsonl
```

Expected: 50,000 rows. Verify against the published `SHA256SUMS` if present.

### Dependencies

**The scored agent path requires no third-party packages.** It is Python standard
library only — `sqlite3` FTS5 for the index, `re` for extraction, `json`/`gzip`
for the catalog. This is verified in CI-style by importing the agent with
`numpy`, `torch`, `openai` and `sentence-transformers` blocked at the import
hook.

Optional extras, none of which the scored path uses:

```bash
pip install -r requirements.txt              # numpy — dense-retrieval package only
pip install -r requirements-llm.txt          # openai — optional Tier-2 extractor
pip install -r requirements-embeddings.txt   # sentence-transformers (~2 GB)
```

---

## Reproducing our results

One command, the unmodified official evaluator:

BM25 over an in-memory SQLite FTS5 index (50k products, field-weighted), then
staged reranking so hard-constraint coverage cannot be traded away by lexical
relevance. Everything runs in-process; no vector database.

Writes `results.json` with per-session results and the aggregate metrics quoted
at the top of this README. Runtime ~35 s for all 200 sessions on an Apple M-series
laptop.

The wider robustness matrix (3 datasets × 3 simulators):

```bash
python3 -m tools.bench --limit 200
```

Other reproducible artifacts:

```bash
python3 -m unittest discover tests            # 198 tests
python3 -m scripts.measure_recall             # BM25 recall gate -> docs/recall.md
python3 -m scripts.calibrate_rerank           # weight calibration + miss classification
python3 -m scripts.measure_attribute_yield    # answerability -> docs/attribute_yield.json
python3 -m tools.trace_runner --simulator esci -v   # full turn-by-turn transcripts
```

### Environment variables

**None are required.** All defaults are the measured-best configuration.

| Variable | Default | Effect |
|---|---|---|
| `RERANK_WEIGHTS` | `calibrated` | `default` restores pre-calibration weights |
| `CLARIFY_POLICY` | `other` | `formula` uses the candidate-reduction question policy |
| `RERANK_HARD_FLOOR` / `_RESERVE` | `1` / `2` | Top-K slots reserved for best BM25 matches |
| `RERANK_STAGED`, `RERANK_PREFILTER`, `RERANK_EXCLUDE_NEGATED`, `RERANK_SOFT_ABSTAIN`, `RERANK_DEPARTMENT_PENALTY`, `RERANK_PROFILE_TIEBREAK` | `1` | Individual reranker ablations |
| `RERANK_COVERAGE`, `RERANK_IDF`, `RERANK_CONSENSUS` | `0` | Ranking experiments that measured neutral-to-harmful |
| `EMIT_CREDIBILITY` | `0` | Score-based emit policy (measured worse than the swept turn threshold) |
| `TECHJAM_LLM_EXTRACTOR` | unset | `1` enables the optional Tier-2 LLM extractor |
| `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `TECHJAM_LLM_MODEL`, `TECHJAM_LLM_MAX_CALLS` | unset | Credentials and model id for Tier 2 only |
| `AGENT_DEBUG_LOG`, `AGENT_TRACE_CANDIDATES` | unset | Opt-in JSONL tracing |

No API key is needed to reproduce any number in this README.

---

## Disclosure: model, network, cost, latency

Required by `docs/submission_rules.md` and `docs/final_evaluation_faq.md`.

| | |
|---|---|
| **Model used for the reported results** | **None.** Fully deterministic — BM25 over SQLite FTS5 plus a rule-and-gazetteer extractor. |
| **Network dependency** | **None.** The reported run makes no network calls. |
| **Token usage** | **0** prompt, **0** completion (`reported_token_usage` in `results.json`). |
| **Estimated model cost** | **$0.00** |
| **Latency** | Index build ~5.4 s once at startup; **median 65 ms**, p95 77 ms per turn. Full 200-session evaluation ~35 s. |
| **Hardware / Python** | Apple Silicon laptop, CPU only, Python 3.13.9. No GPU. |
| **Optional LLM tier** | `state/llm_extractor.py` speaks the OpenAI Chat Completions protocol (so any compatible gateway works). **Disabled by default and unused in all reported results.** It is additive-only: it may contribute verbatim spans the deterministic tiers missed, and can never delete a constraint, clear a slot, or change retrieval timing. Any failure — no key, no network, bad JSON, rate limit — silently falls back to the deterministic result. Enabling it requires `TECHJAM_LLM_EXTRACTOR=1` and `OPENAI_API_KEY`; per-call usage is reported through the standard `usage` field. |
| **Fallback behaviour** | The deterministic path *is* the primary path, so there is nothing to fall back from. |

Reported results were produced with the unmodified official evaluator
(`evaluator/local_evaluator.py`, byte-identical to the organiser's upstream copy).

---

## Limitations, and what we would do with more time

**Where we are weakest.** `intent_override` is our lowest scenario at HR@10 0.90
and MTTC 4.37. The override arrives mid-session and every turn spent recovering
is a turn not spent converging. We handle the retraction correctly, but we do not
yet *anticipate* it.

**Negation is correct but barely exercised.** We built NegEx-style scope
detection with a false-friend guard, so "no show socks" stays a product type
while "without underwire" becomes an exclusion. Measured on real queries, true
attribute negation is under 1% — so this is correctness work, not score work, and
we deliberately kept it small.

**Three ranking ideas we tried and rejected.** Cross-turn rank fusion, a
constraint-coverage bonus, and IDF-weighted span evidence all measured neutral to
harmful (consensus was worst: 0.7732 vs 0.8183). They ship disabled with their
numbers recorded in the source, so nobody re-tries them blind. Rank fusion across
turns is a feedback loop — it entrenches the mistakes made when the agent knew
least.

**Dense retrieval is built and deliberately unused.** `retrieval/` implements a
local embedding channel. We classified every miss as either "target never entered
the candidate pool" or "target was in the pool and out-ranked" and got **100%
reachability, zero recall misses**. A dense channel buys candidates; there were
none left to buy. Kept, tested, and gated behind that measurement.

**Given another week**, in priority order: (1) an override-aware retrieval reset
that re-ranks from scratch rather than re-weighting, targeting the 0.90; (2) a
learned reranker trained on the 234 human-labelled ESCI gold rows rather than on
our own generators, since fitting to a simulator we wrote can only teach the
model to imitate it; (3) enabling the Tier-2 LLM on the free-text path, where the
gate says it fires on roughly the queries our gazetteer cannot reach.

---

## Repository layout

```text
starter/agent.py           Agent API entry point — reset() / respond()
starter/extractor.py       Tier 0 templates, Tier 1 gazetteer, polarity layer
starter/retriever.py       BM25 candidates, prefilter, 4-stage reranker
state/state_manager.py     Append-only event log, replay, dialogue policy
state/clarification.py     Candidate-reduction question scoring
state/llm_extractor.py     Optional Tier-2 LLM (off by default)
retrieval/                 Dense-retrieval foundation (built, gated off)
evaluator/local_evaluator.py   Official scorer — UNMODIFIED
tools/bench.py             3 datasets x 3 simulators matrix
tools/customer_sim.py      `realistic` and `esci` adversarial simulators
tools/trace_runner.py      Turn-by-turn transcripts
scripts/                   Lexicon build, recall gate, calibration, yield
docs/fix_report.md         Code-review fixes and ranking ablations
docs/ARCHITECTURE.md       Full design rationale
tests/                     198 tests
```

---

## Team contributions

| Member | Contribution |
|---|---|
| **LEE JIN TIMOTHY** | Catalog indexing, lexical retrieval, and semantic candidate prototyping |
| **RAVICHANDRAN GOKUL** | Constraint-aware ranking and scoring |
| **MAX LIM HAO YAN** | Conversation state, clarification, and intent overrides |
| **CHEN DONG JUN** | Agent API orchestration, caching, and integration |
| **SHANANTH SIVAKUMAR** | Evaluation, reproducibility, Git workflow, and submission documentation |

---

## Data attribution

Catalog and sessions derive from **Amazon Reviews 2023** (McAuley Lab, UCSD).
The `esci` simulator's opening queries come from the public **Amazon ESCI /
Shopping Queries Dataset**; only query text and relevance labels are used, joined
onto the frozen competition catalog. No external data was used to reconstruct
unreleased evaluation labels. See `DATA_ATTRIBUTION.md`.
