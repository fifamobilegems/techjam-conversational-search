# Post-merge architecture review

## What is now restored

- Opt-in JSONL traces: every agent turn can record the message, active state,
  extraction mode, query, clarification decision, results, and candidate
  counts.  With candidate tracing enabled it also records the BM25 top-500 and
  full reranked pool.
- `scripts/analyze_trace.py` joins the trace to the evaluator's `results.json`
  in evaluator order.  This matters because the evaluator deliberately hides
  the sample ID from the agent and uses random session IDs.
- Conservative normalization: `navy -> blue`, `grey -> gray`,
  `beige/tan -> brown`, and `jogging -> running`.  The normalized value goes
  into the slot; the exact phrase remains in `raw_constraints`.
- `Material:alloy` is recognized as material rather than being incorrectly
  treated as a generic feature.
- The optional LLM can see a bounded prior transcript.  Retrieval remains
  deterministic and only uses explicit constraints.

## What deliberately remains unchanged

The merged policy holds the first result list until turn two and repeatedly
asks `other` until the user says they have no additional preference.  Its own
comments record that this beat immediate retrieval on its public sweep.  Do
not replace this with the previous fixed attribute priority without rerunning
the public evaluator; that would be cargo-culting an older branch.

## Validation after this merge

With the deterministic extractor (no LLM flag enabled), the public evaluator
produced:

- Hit@10: `0.980`
- MRR: `0.762698`
- MTTC: `2.48`
- technical score: `0.889209`
- reported model tokens: `0`

The candidate trace found four misses. Every target was inside BM25's top-500
(ranks 98, 135, 273, and 376) but ended at rerank positions 28--146. That
rules out spending time on a larger candidate pool as the first next step.
The next experiment after the validated override fix is targeted boilerplate
filtering for phrases such as `Imported`, `Pull On closure`, and `Zipper
closure`; it must remain isolated and be evaluated independently.

## Validated override reranker fix

The first broad raw-phrase and boilerplate-filter experiment regressed the
score to `0.869195`, so it was rejected. The narrow version retains only
**demoted** multi-token spans after an override, at their state-manager weight
(`0.4`), and leaves ordinary historic spans alone. It produced:

- Hit@10: `0.990`
- MRR: `0.783768`
- MTTC: `2.40`
- technical score: `0.902130`
- reported model tokens: `0`

This path is on by default. Set `RERANK_RAW_PHRASES=0` only to reproduce the
older no-demoted-span baseline. `RERANK_FILTER_BOILERPLATE=1` remains an
experimental A/B switch and is off by default.

## Important unresolved gaps

1. `raw_constraints` are retained in state but are still **not passed to or
   scored by `CatalogRetriever`**.  The code comments claim the opposite.
   Trace failures first: add phrase scoring only if misses show the target is
   in BM25's pool but ranks below ten after a slot collision.
2. BM25 uses an OR query over all terms.  A target can be absent from the
   top-500 even when every individual word is common.  The new trace separates
   this from a reranking loss (`bm25_rank` absent versus `candidate_rank` poor).
   Only then test AND/exact-phrase routes or a small query ensemble.
3. Generic attributes can reward catalog boilerplate such as “Imported” or
   “Machine Wash.”  Add document-frequency downweighting only after trace
   evidence shows these terms dominate failed candidate pools.
4. The environment file currently has OpenRouter-style settings while the
   merged optional extractor is Anthropic-only (`TECHJAM_LLM_EXTRACTOR` and
   `ANTHROPIC_API_KEY`).  Those configurations do not connect.  Choose one
   provider and document it; never keep a real key in a file that might be
   committed or shared.
5. The LLM is additive extraction only.  It cannot clear a stale field,
   classify an override, choose a question, or directly retrieve.  That is
   safe for offline scoring, but it is not the “LLM policy pipeline” described
   in the earlier report.  If you extend it, force a schema of state operations
   and validate every operation in `StateManager`; never let it output ASINs.

## Debugging commands

PowerShell:

```powershell
$env:AGENT_DEBUG_LOG = "debug/deterministic_trace.jsonl"
$env:AGENT_TRACE_CANDIDATES = "1"
Remove-Item debug/deterministic_trace.jsonl -ErrorAction Ignore
python -m evaluator.local_evaluator --catalog data/catalog.jsonl --output results.json
python scripts/analyze_trace.py debug/deterministic_trace.jsonl results.json data/public_set.jsonl
```

Candidate IDs make the trace considerably larger.  Turn off
`AGENT_TRACE_CANDIDATES` for ordinary runs.  The log path is ignored by Git.
With candidate tracing enabled, each miss now includes the target's score
components and the same components for rank 1 and rank 10, plus a per-turn
extraction/state timeline. The analyzer recomputes those three explanations
from `data/catalog.jsonl`; it refuses a gz catalog path.
