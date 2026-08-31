# TODO — robustness-first build

Master checklist, one heading per role. Phases are from `docs/ARCHITECTURE.md`.

**Rule: tick only boxes under your own heading. Never reorder, reformat, or edit
another role's section.** Line-level edits in disjoint sections merge cleanly.

---

## Role A · Harness & Integration
Owns: `tools/`, `scripts/measure_recall.py`, `docs/`, `starter/agent.py`, `starter/debug.py`

- [x] 0.0 Commit `docs/ARCHITECTURE.md` (everyone reads it)
- [x] 0.1 Fix the two merge-damage syntax errors — **verified already fixed on
  `main`** (commit `599ab02`); `agent.py` and `retriever.py` both `py_compile`
  clean, one `raw_constraints` each. See `REQUESTS.md`.
- [x] 0.2 Create `docs/handover/` scaffolding (`TODO`, `REQUESTS`, `HANDOVER_A..D`)
- [x] 0.3 `EsciCustomer` + register `esci` simulator (`tools/customer_sim.py`,
  `tools/trace_runner.py`)
- [x] 0.4 `tools/bench.py` — {public200, synth800, esci1000} × {official, realistic, esci}
- [x] 0.5 `scripts/measure_recall.py` — BM25 recall@{100,500}, raw message vs
  `build_search_context()` columns side by side
- [x] G0 Reproduce the five baseline rows (esci recon 0.044 ≈ 0.042; public200
  official 0.901 vs plan 0.889 — main improved since; others match committed logs)
- [ ] 0.6 FINAL (after B/C/D land): full bench + `docs/submission.md`
  (model/token/cost/latency) + confirm offline path scores with
  `TECHJAM_LLM_EXTRACTOR` unset

## Role B · Extraction
Owns: `starter/extractor.py`, `state/llm_extractor.py`, `scripts/build_lexicon.py`, `data/lexicon.json`

- [x] 2.1 Mine lexicon from catalog → `data/lexicon.json` (committed, deterministic)
- [x] 2.2 Restructure `extractor.py` as explicit Tier 0 / Tier 1 cascade
- [x] 2.2b Polarity/negation layer + multi-word false-friend guard
- [x] 2.3 Per-span provenance (axes deferred — see HANDOVER_B.md)
- [x] 8 Wire LLM tier behind structural gate; tune on provenance; disclose usage

## Role C · State & Policy
Owns: `state/state_manager.py`, `state/clarification.py`

- [ ] 1.1 Never let `search_query` go empty — put raw message into the BM25 query
  (**highest measured value**)
- [x] Schema freeze (first commit after 1.1): add `polarity`, `strength`,
  `confidence`, `superseded`, `provenance` with safe defaults so B/D can code
  against a stable shape
- [x] 3 Event-sourced state (append-only log authoritative, replay, keep demotion)
- [ ] 4 Adaptive clarification `question_value(a)` (answerability fitted, not guessed)
- [ ] 5 Credibility emit policy (score floor / rank1–rank10 margin, re-swept)

## Role D · Retrieval
Owns: `starter/retriever.py`, `scripts/build_embeddings.py`

- [x] 6.1 Hard-constraint prefilter (filter where field present, penalty otherwise)
  — 3x BM25 over-fetch, survivor floor, three-valued predicates; negated spans
  excluded and stripped from the query. Measures 0.0000 on 5 of 6 cells: the
  existing penalties already decided these cases. See HANDOVER_D.md.
- [x] 6.2 Dense channel — **gate not met, deliberately not built**. Re-measured
  raw recall@500 = 0.830 on esci while end-to-end HR@10 = 0.915; multi-turn
  accumulation already exceeds turn-1 recall, so there is no residual for a
  dense channel to recover.
- [x] 7.1 Staged rerank (coverage → relevance → soft → profile tie-break)
- [x] 7.2 Profile tie-breaker (Decision 9; `user_profile` currently unread)
  — implemented as a bounded stage-4 term and measured; Agent must pass
  `user_profile` for it to be live (filed with A in REQUESTS.md).
- [x] 7.4 Weight calibration by coordinate search (not a trained model)
  — `scripts/calibrate_rerank.py`, held out **by generator**: fit on
  synth800/realistic, evaluate on the 234 ESCI provenance=="gold" rows.
