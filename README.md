# Constraint Compass — Conversational E-Commerce Search Agent

**A stateful shopping-search agent for the way shoppers actually decide.** It
helps a TikTok Shop-style shopper move from an incomplete request to a ranked
set of products, while preserving new preferences, explicit rejections, and
later corrections. For TechJam, we evaluate that behaviour against the frozen
50,000-item competition catalog and must surface the hidden target within ten
turns.

**TechnicalScore `0.8468` on the 200-session public set — 7.9× the provided BM25
baseline (`0.1067`)** — using **18,942 tokens and about $0.003** of model spend
across all sessions.

| | HR@10 | MRR | MTTC | Efficiency | TechnicalScore | Tokens |
|---|--:|--:|--:|--:|--:|--:|
| **This agent** (LLM tier on) | 0.9550 | 0.6890 | 2.870 | 0.8130 | **0.8468** | 18,942 |
| Deterministic fallback (no network) | 0.9500 | 0.6840 | 2.875 | 0.8125 | 0.8427 | 0 |
| Provided baseline | 0.1250 | 0.0680 | 9.810 | 0.1190 | 0.1067 | 0 |

The middle row matters: the agent remains fully functional without a key,
network connection, or `openai` package. The LLM improves extraction at the
edges; it is not a dependency for retrieval or dialogue state.

---

## What a conversational search needs

We started with the starter agent and found that it effectively treated every
turn as a fresh keyword search. That is a poor model of shopping. People often
begin with a vague outcome ("something comfortable for walking"), add constraints
as they remember them, reject options, and change their minds. A system that
simply appends words to a query either forgets useful context or keeps stale
context after a correction.

Before designing the agent, we reviewed e-commerce search and conversational
shopping research, including practitioner research on query behaviour and
academic work on clarification and preference elicitation. We translated those
findings into three testable design requirements rather than treating them as
generic chatbot advice:

- Shopping queries are multi-faceted: product type, feature, style, use case,
  budget, and subjective need can coexist. We retain each recognised facet
  instead of forcing a message into one intent label.
- A clarification question has a cost. We return useful candidates early and
  ask only one concise question when it can materially reduce the candidate set;
  we do not run a scripted questionnaire.
- Shoppers value a useful outcome more than chat. The agent stays
  action-oriented: ranked results and at most one decision-relevant follow-up.

This is not a claim of a production TikTok Shop integration. It is a buildable
retrieval-and-dialogue core, evaluated on the competition's Amazon-derived
catalog, for the same conversational discovery problem.

## Design: three independently testable layers

We decomposed the agent into **extraction**, **state management**, and
**retrieval/ranking**. This was both our architecture and our way of dividing
the work: each layer exposes typed data to the next rather than reaching into
another layer's internals. The stable `Agent.reset()` / `Agent.respond()`
competition interface remains the integration boundary. That made experiments
replaceable: we could change an extractor, replay state, or ranking policy
without rewriting the rest of the agent.

### 1. Extraction: structured signal first, model only for ambiguity

Each message becomes typed operations—set, clear, no-preference, or demote—for
individual attributes. Extraction is a three-tier cascade:

1. **Tier 0: template matching** captures the structured evaluator phrasing.
2. **Tier 1: catalog-derived gazetteer** catches ordinary free text. A gazetteer
   is a lookup of known values; ours is mined from catalog metadata, providing
   107 colours and 107 materials rather than a brittle hand-written list of 12
   and 10.
3. **Tier 2: `gpt-4o-mini`** is used only if both deterministic tiers find no
   constraint and no known template was matched. Its schema-constrained prompt
   receives the current message, compact prior state, allowed operations, and
   asks for grounded verbatim spans—not a free-form shopping answer.

The gate is structural rather than a vague confidence threshold: it is easy to
audit, reproducible, and limits spend to roughly one call every two sessions.
The model may add a span the rules missed; it never clears state, ranks products,
or decides whether to retrieve. A polarity layer captures rejected attributes,
so "without underwire" removes a term from the query. A false-friend guard keeps
"no-show socks" as a product type rather than incorrectly treating it as a
negation.

### 2. State: corrections are events, not destructive edits

The state manager records every shopper disclosure in an append-only event log,
then replays that log into the live constraint set each turn. This is a deliberate
answer to the hardest conversational failure mode: intent override.

1. A disclosure creates an immutable event with its attribute, action, polarity,
   strength, source turn, confidence, provenance, and raw text.
2. Replay builds active slots, rejected values, explicit no-preference fields,
   and raw lexical evidence used by retrieval.
3. On "actually, ignore my earlier preference," the old constraint is
   superseded rather than silently overwritten. It leaves the active hard set
   but remains weak evidence (0.4 weight), preserving useful history without
   allowing a stale preference to dominate results.

This produces inspectable turn-by-turn state, isolates sessions, and makes it
possible to trace whether a bad result began in extraction, state transition, or
ranking.

### 3. Retrieval and ranking: hard needs must not lose to a fluent match

The ranking pipeline is staged to keep explicit requirements in control:

1. **Generate candidates.** SQLite FTS5/BM25 searches accumulated shopper words
   and extracted evidence.
2. **Prefilter definite violations.** Products contradicting a determinable hard
   constraint are removed before reranking.
3. **Rerank in priority order.** Hard-constraint coverage comes first, then
   lexical relevance, then soft preferences; the anonymised profile is only a
   tie-breaker and can never override what the shopper stated.
4. **Protect lexical evidence.** Two of ten slots are reserved for the strongest
   BM25 matches. We added this floor after tracing 16 of 21 remaining misses to
   top lexical products being displaced by an unremarkable constraint score.

The agent runs provisional retrieval on every turn. It can return
recommendations and ask a question in the same response, rather than wasting a
turn before showing any value.

## How we challenged our own solution

The official simulator often expresses requirements using the target product's
catalog metadata. Optimising only for that format risks building a template
matcher instead of a shopping agent. We therefore built two adversarial customer
simulators and scored changes across official, paraphrased, and real-query
conditions:

- `realistic` paraphrases product needs instead of repeating catalog fields.
- `esci` starts with public Amazon ESCI Shopping Queries and joins them to the
  frozen competition catalog without accessing unreleased labels.

An early version scored 0.87 on official phrasing and **0.04** on real queries.
We traced the gap to a concrete failure: the search query only contained
extracted slots. When extraction failed on natural phrasing, it discarded the
shopper's actual words. Raw-text retrieval had 0.82 BM25 recall; the generated
empty query had 0.03. We fixed the coupling by retaining lexical evidence
alongside structured state.

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

Across 1,400 sessions, the observed TechnicalScore spread across these tested
phrasings is 0.04, not 0.83. This does not prove universal real-world quality;
it is evidence that the agent is materially less dependent on the official
template wording.

† **These two cells lost network mid-run**, and that turned into the most useful
accident of the project: they fell back to the deterministic cascade and scored
**identically** to our no-LLM baseline (0.8869 and 0.8655). The fallback is
verified under an actual connectivity failure, not merely asserted.

### Experiments we rejected

We recorded negative results rather than burying them:

- **Cross-turn rank fusion:** worst result, 0.7732 versus 0.8183. It reinforced
  mistakes made before the agent had enough evidence.
- **Constraint-coverage bonus and IDF-weighted spans:** neutral or harmful in
  ablations, so they remain disabled with measurements recorded in the source.
- **Dense retrieval:** implemented but not used. A miss audit found 100% target
  reachability in the candidate pool; remaining errors were ranking errors, so
  another candidate generator did not justify its runtime or dependency cost.

### Buying-versus-browsing routing: a plausible idea that regressed

The problem statement suggests separate buying and browsing policies, so we
tested full routing (early buying results and typed browsing questions) and an
emit-only version against the shipped control on the same deterministic,
stratified 100-session public subset with the LLM disabled.

The control scored **0.8551**. Full routing fell to **0.7934** (HR@10 0.9600 →
0.9300; MTTC 2.770 → 4.270) and emit-only routing to **0.8470**. On a paired
real-query ESCI sample, all policies tied at 0.8678: strong lexical opening
queries left no measurable value for the dialogue branch.

This may partly reflect limits in our implementation, but the central problem is
structural: `buying` and `browsing` are too coarse. Constraint specificity,
candidate entropy, hard-constraint coverage, rank separation, and likelihood of
a useful answer are better decision signals. The evaluator also privileges its
open `other` question, while typed questions can spend a turn on little
information. We keep override replay, disable buying/browsing routing, and will
instead pursue a calibrated turn-level policy.

## Why this is practical beyond the benchmark

The core design avoids prototype-only dependencies: SQLite FTS5 is local and
portable, the catalog-derived lexicon is reproducible, conversational state is
session-isolated, and the optional model has a tested deterministic fallback.
On the measured public run, no-escalation turns have a 65 ms median latency and
the full LLM-assisted run costs about $0.003 for 200 sessions. The system is
therefore suitable as a low-cost retrieval component behind a shopping UI; a
production rollout would still need live catalog ingestion, safety and quality
monitoring, and UX evaluation with actual TikTok Shop shoppers.

| Judging dimension | Evidence in this project |
|---|---|
| Technical execution | Typed layer contracts, SQLite FTS5 retrieval, event-sourced state, 198 automated tests, official-evaluator reproduction, and tested network failure fallback. |
| Innovation and insight | The distinctive decision is not "add an LLM." It is treating preference changes as replayable state and using adversarial phrasing tests to reject score-only optimisation. |
| Impact and relevance | It addresses a real discovery problem: shoppers often reveal needs incrementally, and recommendations remain useful while the system learns more. |
| Feasibility and practicality | Standard-library deterministic path, bounded optional-model cost, local index, explicit failure behaviour, and documented limitations keep the design proportionate. |

---

## Setup

**Python 3.10+** (developed and measured on **3.13.9**).

```bash
git clone https://github.com/fifamobilegems/techjam-conversational-search.git
cd techjam-conversational-search
```

### The catalog

Not committed, per `docs/final_evaluation_faq.md` §4 — large assets ship as
documented downloads. Fetch it from the participant-kit Release, verify it, then
decompress:

```bash
curl -LO https://github.com/TechJam2026/techjam-conversational-search/releases/download/participant-kit/catalog.jsonl.gz
curl -LO https://github.com/TechJam2026/techjam-conversational-search/releases/download/participant-kit/SHA256SUMS
shasum -a 256 -c SHA256SUMS --ignore-missing
gzip -dkc catalog.jsonl.gz > data/catalog.jsonl
wc -l data/catalog.jsonl        # expected: 50000
```

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

The robustness suite across official, paraphrased, and real-query conditions:

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

| Variable | Default | Effect |
|---|---|---|
| `OPENAI_API_KEY` | unset | **Required for Tier 2.** Without it the agent falls back to deterministic. |
| `TECHJAM_LLM_EXTRACTOR` | `1` | `0` disables Tier 2 entirely |
| `TECHJAM_LLM_MODEL` | `gpt-4o-mini` | Any OpenAI-protocol chat model |
| `OPENAI_BASE_URL` | unset | Point at a compatible gateway (e.g. OpenRouter) |
| `TECHJAM_LLM_MAX_CALLS` | `250` | Per-process cost guard |
| `RERANK_WEIGHTS` | `calibrated` | `default` restores pre-calibration weights |
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
scripts/                   Lexicon build, recall gate, calibration, yield
docs/recall.md             BM25 recall gate results
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
