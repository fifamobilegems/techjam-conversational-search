# Conversational E-Commerce Search Agent — TechJam 2026, Track 4

A stateful conversational retrieval agent that finds a shopper's hidden target
product in a frozen 50,000-item catalog within 10 turns.

**TechnicalScore `0.8468` on the 200-session public set — 7.9× the provided BM25
baseline (`0.1067`)** — for **18,942 tokens and about $0.003** of model spend
across all 200 sessions.

| | HR@10 | MRR | MTTC | Efficiency | TechnicalScore | Tokens |
|---|--:|--:|--:|--:|--:|--:|
| **This agent** (LLM tier on) | 0.9550 | 0.6890 | 2.870 | 0.8130 | **0.8468** | 18,942 |
| Deterministic fallback (no network) | 0.9500 | 0.6840 | 2.875 | 0.8125 | 0.8427 | 0 |
| Provided baseline | 0.1250 | 0.0680 | 9.810 | 0.1190 | 0.1067 | 0 |

The middle row matters: the agent is fully functional with no key, no network,
and no `openai` package. The LLM is upside, never a dependency.

---

## Project overview

The starter agent treats each turn as a fresh keyword search. That loses the two
things a shopping conversation is actually made of: constraints accumulate, and
sometimes they get retracted. We built the agent around those two facts.

**1. Extraction is a three-tier cascade.** Tier 0 is a template matcher for the
structured phrasing the simulator uses. Tier 1 is a gazetteer mined from the
catalog itself — 107 colours and 107 materials against the 12 and 10 a
hand-written list would carry — which catches free text no template matches.
**Tier 2 is `gpt-4o-mini`**, and it fires only when both deterministic tiers
return nothing *and* the message matched no known template. That gate is
structural, not a confidence score: it is auditable, reproducible, and it keeps
model spend at roughly one call per two sessions.

A separate polarity layer marks what the shopper *rejected*, so "without
underwire" removes a term from the query instead of searching for it — while a
false-friend guard keeps "no show socks" a product type rather than a negation.

**2. State is an append-only event log.** Every disclosure is an immutable event
and the live constraint set is *replayed* from that log rather than edited in
place. That is what makes intent overrides correct: "actually, ignore my earlier
preference" appends a superseding event, so the stale constraint leaves the
active set but survives at 0.4 weight — because an overridden preference still
describes the same target product.

**3. Ranking is staged so hard constraints cannot be outvoted.** BM25 over SQLite
FTS5 generates candidates, a prefilter drops determinate violators before the
budget is spent, and reranking runs in four ordered stages: hard-constraint
coverage, lexical relevance, soft preferences, then the user profile as a pure
tie-break. A hard-match floor reserves 2 of the 10 returned slots for the
strongest lexical matches, because a product BM25 ranks first can otherwise fall
out of the Top-10 on an unremarkable constraint total — that was 16 of our 21
remaining misses.

### What we think is the interesting part

We built two more customer simulators to attack our own agent.

The official simulator has the shopper recite the target product's own catalog
metadata verbatim. An agent tuned only against it learns to exploit that, and we
could not tell from a single score whether we had built a shopping agent or a
template matcher. So we wrote `realistic` (a paraphrasing shopper) and `esci`
(opening turns taken verbatim from real Amazon shopping queries in the public
ESCI Shopping Queries dataset), and we score every change against all three.

An early version scored 0.87 on official phrasing and **0.04** on real queries.
That gap was the single most useful number in the project.

| dataset × simulator | n | HR@10 | MRR | MTTC | TechnicalScore | Tokens |
|---|--:|--:|--:|--:|--:|--:|
| public200 × official | 200 | 0.9550 | 0.6850 | 2.855 | **0.8459** | 17,995 |
| public200 × realistic | 200 | 0.9950 | 0.7318 | 2.520 | 0.8866 | 13,835 |
| synth800 × official | 200 | 0.9700 | 0.7491 | 2.600 | 0.8777 | 11,284 |
| synth800 × realistic | 200 | 1.0000 | 0.8003 | 2.290 | 0.9143 | 21,379 |
| esci1000 × official | 200 | 0.9500 | 0.7909 | 2.785 | 0.8766 | 12,008 |
| esci1000 × realistic | 200 | 0.9950 | 0.7257 | 2.415 | 0.8869 | 0 † |
| **esci1000 × esci** (real queries) | 200 | 0.9850 | 0.6818 | 2.575 | **0.8655** | 0 † |
| | | | | **mean** | **0.8791** | 76,501 |

1,400 sessions. The spread across the three phrasings is 0.04, not 0.83.

† **These two cells lost network mid-run**, and that turned into the most useful
accident of the project: they fell back to the deterministic cascade and scored
**identically** to our no-LLM baseline (0.8869 and 0.8655). The fallback is
verified under real failure, not asserted.

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

Expected: 50,000 rows.

### Dependencies

```bash
pip install -r requirements-llm.txt      # openai — the Tier-2 extractor
```

Optional, unused by the scored path:

```bash
pip install -r requirements.txt              # numpy — dense-retrieval package
pip install -r requirements-embeddings.txt   # sentence-transformers (~2 GB)
```

**The deterministic path needs none of these.** It is Python standard library
only — `sqlite3` FTS5 for the index, `re` for extraction. This is enforced, not
claimed: the agent is imported in CI with `numpy`, `torch`, `openai` and
`sentence-transformers` blocked at the import hook.

### Credentials

Copy `.env_example` to `.env` and set your key. `.env` is gitignored and no
secret value is committed anywhere in this repository.

```bash
cp .env_example .env
# then edit .env:
OPENAI_API_KEY=sk-...            # required for the Tier-2 extractor
TECHJAM_LLM_MODEL=gpt-4o-mini    # default
```

Without a key the agent runs the deterministic path and scores 0.8427.

---

## Reproducing our results

One command, the unmodified official evaluator:

```bash
python3 -m evaluator.local_evaluator
```

Writes `results.json` with per-session results and the aggregate metrics quoted
at the top. Runtime ~97 s with the LLM tier active, ~35 s without.

The robustness matrix (3 datasets × 3 simulators):

```bash
python3 -m tools.bench --limit 200
```

Other reproducible artifacts:

```bash
python3 -m unittest discover tests            # 209 tests
python3 -m scripts.measure_recall             # BM25 recall gate -> docs/recall.md
python3 -m scripts.calibrate_rerank           # weight calibration + miss classification
python3 -m scripts.measure_attribute_yield    # answerability -> docs/attribute_yield.json
python3 -m scripts.measure_gate               # Tier-2 escalation rate -> docs/gate_sweep.json
python3 -m tools.trace_runner --simulator esci -v   # full turn-by-turn transcripts
```

Offline experiments (need `requirements-training.txt`; nothing here is imported
by the agent):

```bash
python3 -m scripts.build_esci_gold            # 770 human ESCI judgments -> data/
python3 -m scripts.build_rerank_features      # coefficient matrices -> data/
python3 -m scripts.train_reranker --epochs 500   # -> docs/reranker_training.json
```

### Environment variables

| Variable | Default | Effect |
|---|---|---|
| `OPENAI_API_KEY` | unset | **Required for Tier 2.** Without it the agent falls back to deterministic. |
| `TECHJAM_LLM_EXTRACTOR` | `1` | `0` disables Tier 2 entirely |
| `TECHJAM_LLM_MODEL` | `gpt-4o-mini` | Any OpenAI-protocol chat model |
| `OPENAI_BASE_URL` | unset | Point at a compatible gateway (e.g. OpenRouter) |
| `TECHJAM_LLM_MAX_CALLS` | `250` | Per-process cost guard |
| `TECHJAM_LLM_GATE` | `empty` | `low_confidence` also escalates thin Tier-1 reads (`docs/tier2_gate.md`) |
| `TECHJAM_LLM_GATE_COVERAGE` / `_RESIDUAL` | `0.5` / `2` | Thresholds for the widened gate |
| `RERANK_WEIGHTS` | `calibrated` | `default` restores pre-calibration weights; `esci`, `esci_soft`, `esci_anchored` are the human-label fits, all measured worse (`docs/esci_reranker.md`) |
| `CLARIFY_POLICY` | `other` | `formula` uses the candidate-reduction question policy |
| `RERANK_HARD_FLOOR` / `_RESERVE` | `1` / `2` | Top-K slots reserved for best BM25 matches |
| `RERANK_STAGED`, `RERANK_PREFILTER`, `RERANK_EXCLUDE_NEGATED`, `RERANK_SOFT_ABSTAIN`, `RERANK_DEPARTMENT_PENALTY`, `RERANK_PROFILE_TIEBREAK` | `1` | Individual reranker ablations |
| `RERANK_COVERAGE`, `RERANK_IDF`, `RERANK_CONSENSUS` | `0` | Ranking experiments that measured neutral-to-harmful |
| `EMIT_CREDIBILITY` | `0` | Score-based emit policy (measured worse than the swept turn threshold) |
| `AGENT_DEBUG_LOG`, `AGENT_TRACE_CANDIDATES` | unset | Opt-in JSONL tracing |

---

## Disclosure: model, network, cost, latency

Required by `docs/submission_rules.md` §Model Policy and
`docs/final_evaluation_faq.md` §2–3.

| | |
|---|---|
| **Model** | **OpenAI `gpt-4o-mini`**, via the Chat Completions protocol with a JSON-schema-constrained response. Any OpenAI-protocol gateway works via `OPENAI_BASE_URL`. |
| **Where it is used** | Tier 2 of the extraction cascade only. It never ranks, never scores, and never decides retrieval timing. |
| **Network dependency** | Required only for Tier 2. Everything else is local. |
| **Token usage** | **18,942** total for the 200-session public run (18,046 prompt / 896 completion). ~95 escalations, i.e. roughly one call per two sessions. Across the full 1,400-session matrix: 76,501 tokens. |
| **Estimated cost** | **≈ $0.0032** for the 200-session public run at `gpt-4o-mini` list pricing ($0.15/1M input, $0.60/1M output) — about **$0.000016 per session**. The full 1,400-session matrix cost ≈ $0.013. |
| **Latency** | Index build ~5.4 s once at startup. Turns with no escalation: **median 65 ms**. Turns that escalate: **~1.9–3.2 s**. Full 200-session evaluation: 97 s with Tier 2, 35 s without. |
| **Hardware / Python** | Apple Silicon laptop, CPU only, Python 3.13.9. No GPU. |
| **Fallback behaviour** | Total and silent. Missing key, missing `openai` package, no network, malformed JSON, or rate limit → `_build_client()` or `_call()` returns and the deterministic cascade is the entire agent. **Verified under real network loss**: two bench cells lost connectivity mid-matrix and scored identically to the deterministic baseline. |
| **Cost guard** | `TECHJAM_LLM_MAX_CALLS` (default 250) caps calls per process independently of the gate. |

Reported results were produced with the unmodified official evaluator
(`evaluator/local_evaluator.py`, byte-identical to the organiser's upstream copy).

---

## Limitations, and what we would do with more time

**Where we are weakest.** `intent_override` is our lowest scenario at HR@10 0.90
and MTTC 4.37. The override arrives mid-session and every turn spent recovering
is a turn not spent converging. We handle the retraction correctly, but we do not
yet *anticipate* it.

**The LLM tier is deliberately small.** It fires on ~1 call per 2 sessions and
contributes +0.004 on the public set. That is by design — the structural gate
only escalates where the deterministic cascade has nothing to say — but it means
we have not explored what a larger role for the model would buy. A reranking or
query-rewriting role is the obvious next experiment.

**Negation is correct but barely exercised.** We built NegEx-style scope
detection with a false-friend guard, so "no show socks" stays a product type
while "without underwire" becomes an exclusion. Measured on real queries, true
attribute negation is under 1% — correctness work, not score work.

**Three ranking ideas we tried and rejected.** Cross-turn rank fusion, a
constraint-coverage bonus, and IDF-weighted span evidence all measured neutral to
harmful (consensus worst: 0.7732 vs 0.8183). They ship disabled with their
numbers recorded in the source, so nobody re-tries them blind. Rank fusion across
turns is a feedback loop — it entrenches the mistakes made when the agent knew
least.

**Dense retrieval is built and deliberately unused.** `retrieval/` implements a
local embedding channel. We classified every miss as either "target never entered
the candidate pool" or "target was in the pool and out-ranked" and got **100%
reachability, zero recall misses**. A dense channel buys candidates; there were
none left to buy.

**Given another week**, in priority order: (1) an override-aware retrieval reset
that re-ranks from scratch rather than re-weighting, targeting the 0.90;
(2) widening the Tier-2 gate to cover low-confidence Tier-1 guesses, not just
empty ones; (3) a reranker trained on the 234 human-labelled ESCI gold rows
rather than on simulators we wrote ourselves, since fitting to your own generator
can only teach a model to imitate it.

**(2) and (3) were built.** Both are documented in full, neither changes a
default, and the interesting outcome is not the one we expected.

*The widened gate is inert where it matters.* `TECHJAM_LLM_GATE=low_confidence`
escalates on residual coverage — how much of the message the cascade left
unexplained — rather than on the one bit "did it emit anything". Tier 0 still
blocks unconditionally, and that is measured: **all three official cells escalate
57 / 34 / 59 times at every threshold setting**, identical to the shipped gate.
It therefore cannot damage the official column and cannot lift it either. With
the LLM on, the mean across five cells moves +0.0004 for +34% tokens — inside the
noise floor, which the same run measures at ±0.001–0.005 from model sampling
alone. Kept, off. `docs/tier2_gate.md`.

*The reranker failed, and one weight inside it is worth more than the whole LLM
tier.* The 234 rows turned out to be near the ceiling, not a sample: all 2.62M
ESCI judgments join to 357 catalog products by ASIN. Adding a normalized-title
join for child ASINs reaches **770 judgments over 748 real queries** — 3.3×, and
the limit. Fitting the 37 reranker weights on them in PyTorch improves held-out
single-turn MRR by 22% (peaking at **epoch 41 of 500** — past that, memorisation)
and **regresses every bench cell by −0.14**.

Attributing that per weight showed it is not diffuse: `feature_boost` going
negative accounts for −0.1411 of it, an artifact of `feature` being the
extraction cascade's junk drawer. Meanwhile `popularity_scale` 0.24 → 3.05 is
positive on its own and takes the **official public-set score from 0.8427 to
0.8883** (MRR 0.684 → 0.808), offline and free — roughly ten times what the
entire Tier 2 tier contributes.

Why: the public set's targets are real purchase records, with a median review
count of **6,846 against the catalog's 12**. Popularity is the strongest true
prior available about how targets were drawn. Our own `synth800` sampled targets
uniformly (median 13), carries no such signal, and is the one cell that
disagrees — the adversarial simulator we built to keep ourselves honest was
itself wrong about a real distribution, and the human labels are what caught it.
`RERANK_WEIGHTS=esci_popularity`, `docs/esci_reranker.md`.

---

## Repository layout

```text
starter/agent.py           Agent API entry point — reset() / respond()
starter/extractor.py       Tier 0 templates, Tier 1 gazetteer, polarity layer
starter/retriever.py       BM25 candidates, prefilter, 4-stage reranker
state/state_manager.py     Append-only event log, replay, dialogue policy
state/clarification.py     Candidate-reduction question scoring
state/llm_extractor.py     Tier 2 — gpt-4o-mini, structurally gated
retrieval/                 Dense-retrieval foundation (built, gated off)
evaluator/local_evaluator.py   Official scorer — UNMODIFIED
tools/bench.py             3 datasets x 3 simulators matrix
tools/customer_sim.py      `realistic` and `esci` adversarial simulators
tools/trace_runner.py      Turn-by-turn transcripts
scripts/                   Lexicon build, recall gate, calibration, yield, gate sweep
scripts/build_esci_gold.py     ESCI human judgments joined onto the frozen catalog
scripts/train_reranker.py      PyTorch fit of the 37 reranker weights (offline)
docs/tier2_gate.md         Escalation-rate sweep for the widened Tier-2 gate
docs/esci_reranker.md      Human-label reranker: why it did not ship
docs/fix_report.md         Code-review fixes and ranking ablations
docs/ARCHITECTURE.md       Full design rationale
tests/                     209 tests
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
