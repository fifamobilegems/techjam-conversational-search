# Handover — Role A (Harness & Integration)

## Status
partial — Phase 0.0–0.5 and the G0 baseline gate are complete. Phase 0.6 (final
bench + `docs/submission.md` + offline-path confirmation) is intentionally
deferred; it must run *after* B/C/D land so it measures the finished system.

## What I changed
- `docs/ARCHITECTURE.md`: verbatim team plan, committed so everyone reads one copy (0.0).
- `docs/handover/{TODO,REQUESTS,HANDOVER_A..D}.md`: coordination scaffolding (0.2).
- `tools/customer_sim.py`: added `EsciCustomer` (verbatim ESCI-query opener, terse
  comma-joined facet replies) + its phrasing banks (0.3).
- `tools/trace_runner.py`: added `EsciCustomerAdapter`, registered `"esci"` in
  `build_customer()` and in the `--simulator` choices (0.3).
- `tools/bench.py`: dataset × simulator matrix runner (0.4).
- `scripts/measure_recall.py`: raw-message vs `build_search_context()` BM25 recall
  gate (0.5).
- `docs/recall.md` + `docs/recall.json`: committed recall diagnostic output.
- `logs/bench/<ts>.{json,md}`: bench matrix output (local; `logs/` is gitignored).

**I did not fix the Phase 0.1 syntax errors** — they are already fixed on `main`
(commit `599ab02`, merged via PR #8). Both `starter/agent.py` and
`starter/retriever.py` `py_compile` cleanly on `0491eaf`, each with a single
`raw_constraints` binding. No unblock-merge was outstanding. See `REQUESTS.md`.
This also means I touched none of the files Role A must not edit
(`starter/extractor.py`, `starter/retriever.py`, `state/*`, `evaluator/*`).

## Contracts I introduced or changed
- `tools.customer_sim.EsciCustomer(sample, product, coarse_category, max_turns=10)`
  — subclasses `RealisticCustomer`; same `opening()/reply()/override_message()/
  override_turn/boundary_used` contract. `opening()` returns `sample["esci_query"]`
  verbatim; falls back to the realistic opener if the field is absent/blank.
- `tools.trace_runner.EsciCustomerAdapter` (`kind="esci"`) and
  `build_customer(simulator="esci", ...)`; `--simulator` now accepts `esci`.
- `tools.bench.main` / `run_matrix` — CLI: `--datasets --simulators --limit
  --select --out-dir --full-matrix`. Reuses `run_session` + `summarize`
  (`metric_summary`). Consumers: Role A final integration; anyone gating a phase.
- `scripts.measure_recall.main` — CLI: `--datasets --simulators --limit --select
  --end-of-session --out --json-out`. Reuses `CatalogRetriever._bm25_search`,
  `catalog_index`, `build_customer`.

## Bench results — before vs after
Role A adds only measurement/coordination code (`tools/`, `scripts/`, `docs/`) and
changes nothing on the agent's request→response path, so the scored numbers are
**unchanged by this branch (before == after)**. The rows below are the full
current-`main` baseline from `python3 -m tools.bench` (written to `logs/bench/`,
which is gitignored — regenerate on demand, like `results.json`); they double as
the reproduced G0 gate.

| dataset x simulator | HR@10 before | HR@10 after | technical | vs plan Context table |
|---|---|---|---|---|
| public200 x official | 0.990 | 0.990 | 0.901 | plan 0.980 / 0.889 |
| synth800 x official | 0.878 | 0.878 | 0.763 | plan 0.859 / 0.738 |
| synth800 x realistic | 0.240 | 0.240 | 0.194 | plan 0.238 / 0.193 |
| esci1000 x official | 0.857 | 0.857 | 0.742 | plan 0.839 / 0.723 |
| esci1000 x esci | 0.056 | 0.056 | 0.044 | plan 0.054 / 0.042 |

The `official` rows sit slightly **above** the plan's Context table because `main`
gained override-aware reranking and multi-turn constraint preservation after the
plan was written (PRs #5–#8); the paraphrased/real rows match, and the
`esci1000 x esci` row reconciles to the committed `logs/esci1000_esci/` (0.054 /
0.042) within noise.

Recall gate (turn 1), the diagnostic behind the whole robustness track — see
`docs/recall.md`:

| dataset x simulator | raw@500 | pipeline@500 | empty% |
|---|---|---|---|
| esci1000 x esci | 0.834 | 0.024 | 89% |
| synth800 x realistic | 0.936 | 0.600 | 10% |
| esci1000 x official | 0.850 | 0.874 | 0% |

The extractor discards the query on real phrasing: raw words would retrieve the
target (0.83), but `build_search_context()` is empty in ~89% of ESCI cases.
Role C's Phase 1.1 (never let `search_query` go empty) is the highest-value fix.

## What I could NOT do, and why
- **Phase 0.6 (final integration).** Depends on B/C/D. When they land: run
  `python3 -m tools.bench` (full), write `docs/submission.md` (model / token usage
  / cost / latency), and confirm the offline path scores with
  `TECHJAM_LLM_EXTRACTOR` unset.
- **Byte-identical reproduction of `logs/esci1000_esci/`.** The original
  `EsciCustomer` code was never committed, so I reverse-engineered it from the
  transcripts. Content (facet values, order, timing, override) reproduces
  deterministically; only the random phrasing *wrapper* per reply differs, which
  flips ~1.6% of sessions. Metric reconciles: 0.044 vs 0.042.

## Requests I filed in REQUESTS.md
- A → C/D: verification that the 0.1 syntax fix is already on `main` (no action).

## What the next person needs to know
- **Get the catalog first:** `gzip -dc catalog.jsonl.gz > data/catalog.jsonl`
  (gitignored, required by the retriever/bench/recall tools).
- **Every phase must report all three simulators.** Gate with
  `python3 -m tools.bench --limit 100` and paste before/after into your PR.
  `official` up while `realistic`/`esci` down is overfitting — reject it.
- **The esci simulator needs `esci_query`** (only `data/esci_set_1000.jsonl` has
  it); bench skips that column elsewhere unless `--full-matrix`.
- **Role C's schema freeze is the critical unblock for B and D** — add `polarity`,
  `strength`, `confidence`, `superseded`, `provenance` with safe defaults in one
  early commit after Phase 1.1.
- Commands: `python3 -m tools.bench`, `python3 -m scripts.measure_recall`,
  `python3 -m tools.trace_runner --simulator esci -v --dataset data/esci_set_1000.jsonl`.
