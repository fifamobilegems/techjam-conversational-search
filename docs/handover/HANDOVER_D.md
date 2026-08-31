# Handover — Role D

## Status

partial — 6.1, 7.1, 7.2 and 7.4 complete; 6.2 **deliberately not built**, gate
result below. One measurement I could not run is blocked on an A-owned file.

## The baseline in my brief was stale, and that changed what the work was

My brief quoted end-to-end HR@10 of **0.054** on `esci1000 × esci`. On `main` at
`f86a4e8` — after B's lexicon cascade and C's state work landed — it measures
**0.915**. The collapse the architecture was written around is already fixed.

That moves the bottleneck. HR@10 is now 0.84–0.99 across the matrix while MRR
sits at **0.48–0.54** on the paraphrased columns, and the technical score is
`0.50·HR + 0.30·MRR + 0.20·efficiency`. The remaining headroom is almost
entirely *rank within the top 10*, which is what staged reranking and weight
calibration address. I re-derived this rather than assuming it; numbers below.

## What I changed

- `starter/retriever.py`: ranking keyed on `(-hard_violations, score)` so a
  determinate violation cannot be traded away (7.1); prefilter over a 3× BM25
  over-fetch with a survivor floor (6.1); negated spans excluded *and* stripped
  from the query text; `strength="soft"` constraints abstain instead of
  penalising; `details.Department` read as a calibratable penalty; every
  magnitude moved into `RerankWeights`; scores emitted as
  `(key, weight_name, coefficient)` contributions; profile tie-break (7.2);
  `attribute_stats` published for Role C. Dead `_evidence_score` /
  `_usable_evidence` removed.
- `scripts/calibrate_rerank.py` **(new file, sole author)**: coordinate search
  held out by generator (7.4).
- `tests/test_retriever_staged.py` **(new file, sole author)**: regression tests
  for the structural guarantees.

Both new files are new paths, so they cannot merge-conflict with another role's
work. I edited no file owned by A, B or C.

## Contracts I introduced or changed

- `CatalogRetriever.retrieve_and_rerank(..., user_profile: dict | None = None)`
  — new optional trailing keyword. Inert until Role A passes it (filed).
- `CatalogRetriever(catalog_path, candidate_limit, config=None, weights=None)`
  — `RerankConfig` (behavioural switches) and `RerankWeights` (magnitudes) are
  now injectable. Both default to today's committed values, so
  `CatalogRetriever(path)` is unchanged for every existing caller.
- `last_diagnostics` gains `attribute_stats` (for `state.clarification`),
  `department`, `negated`, and `prefilter_removed`. Each entry of
  `candidate_scores` gains `violations` and `stage_scores`; `final_score`,
  `constraint_details`, `fusion_score` and `quality_score` keep their meaning.
- Env switches, all defaulting to the shipped behaviour: `RERANK_STAGED`,
  `RERANK_PREFILTER`, `RERANK_EXCLUDE_NEGATED`, `RERANK_SOFT_ABSTAIN`,
  `RERANK_DEPARTMENT_PENALTY`, `RERANK_DEPARTMENT_GATE`,
  `RERANK_PROFILE_TIEBREAK`, `RERANK_OVERFETCH`, `RERANK_MIN_SURVIVORS`.

## Feature ablation — n=200 stratified per cell, technical score

Round 1 isolates each mechanism against the pre-change reranker. The refactor
reproduces the baseline **exactly** with every switch off (0.7713 / 0.7059 /
0.8766), so these deltas are attributable.

| config | esci/esci | esci/realistic | esci/official | synth/official | synth/realistic | public/official |
|---|--:|--:|--:|--:|--:|--:|
| baseline (all off) | 0.7713 | 0.7059 | 0.8766 | 0.8694 | 0.7361 | 0.9012 |
| + hard-constraint gate | 0.7713 (0) | 0.7059 (0) | 0.8766 (0) | 0.8694 (0) | 0.7361 (0) | 0.9012 (0) |
| + gate + prefilter | 0.7713 (0) | 0.7057 (−.0002) | 0.8766 (0) | 0.8694 (0) | 0.7354 (−.0007) | 0.9012 (0) |
| + negation exclusion | 0.7705 (−.0008) | 0.7059 (0) | 0.8766 (0) | 0.8694 (0) | 0.7361 (0) | 0.9012 (0) |
| + soft abstain | 0.7667 (−.0046) | 0.7194 (**+.0136**) | 0.8766 (0) | 0.8694 (0) | 0.7412 (+.0051) | 0.9012 (0) |
| + department as a *hard gate* | 0.7753 (+.0040) | 0.7274 (**+.0215**) | 0.8719 (−.0047) | 0.8503 (**−.0192**) | 0.7489 (+.0128) | 0.9022 (+.0010) |

### The uncomfortable result: staging is worth ~zero on these benchmarks

The hard-constraint gate — the headline of 7.1, and the fix for the failure the
architecture names explicitly — moves **nothing**. Nor does the prefilter, nor
negation exclusion. That is not a bug in the implementation; it is what the data
says, and it is worth stating plainly rather than burying:

- **Budget and brand violations are already decided by their penalties.** BM25
  fusion is `100/(60+rank)`, so it spans **1.64 at rank 1 down to 0.18 at rank
  500** — a total range of 1.46. A `−60` budget penalty is never outranked by
  lexical relevance, because lexical relevance is worth less than two points.
- The architecture's phrasing ("a strong BM25 match can outrank a hard budget
  violation despite the −60 penalty") is therefore not quite right about the
  mechanism. What *can* outrank a budget violation is **other constraint
  bonuses**: a product matching category (+45), material (+40), color (+35) and
  brand (+50) banks +170 against that −60. That shape is real, it is what
  `tests/test_retriever_staged.py` demonstrates, and the gate does fix it — it
  is simply rare enough in these sessions not to register.
- Genuine negation is **<1%** of ESCI queries, which independently confirms
  Role B's audit finding from the other side of the pipeline.

So the gate ships as a **correctness guarantee that costs nothing** (0.0000 on
five of six cells), not as a scoring win. I would rather report that than
attribute someone else's gain to it.

### Department is the one new signal that pays, and it must not be a gate

`details.Department` is on **87.2%** of the catalog — far better covered than
`Color` (4.9%) or `Material` (4.1%) — and shoppers state it constantly
("hoodies for men"). Inferring it from the query and comparing against the field
lifts `esci/realistic` by **+0.0215**.

As a *hard gate* it also costs **−0.0192 on synth800/official**, because it
rejects the true target outright. Measured directly against ground truth: the
gate fires on 74.9% of ESCI queries, is determinate on 664, and **rejects the
correct product on 53 of them — 5.3% of all samples**. The rejections are
mostly genuine ambiguity (a "boys" query whose target is filed `Mens`), not
fixable noise: cross-checking `Department` against gender cues in the title only
recovers 4 of the 53, because title and field disagree on just 2.4% of products.

Well-covered evidence that is sometimes wrong earns a **penalty, not an
exclusion**. Round 2 puts that head to head on the three cells where department
moved anything at all — `stack` is staging + prefilter + negation + soft
abstain, with department the only variable:

| config | esci/realistic | synth/official | esci/esci | mean Δ |
|---|--:|--:|--:|--:|
| baseline (all off) | 0.7059 | 0.8694 | 0.7713 | — |
| stack, no department | 0.7192 (+.0134) | 0.8694 (0) | 0.7659 (−.0053) | +.0027 |
| **stack + department penalty** | **0.7385 (+.0327)** | **0.8681 (−.0013)** | **0.7718 (+.0005)** | **+.0106** |
| stack + department gate | 0.7196 (+.0138) | 0.8480 (−.0214) | 0.7680 (−.0033) | −.0036 |
| stack + both | 0.7196 (+.0138) | 0.8480 (−.0214) | 0.7680 (−.0033) | −.0036 |

The penalty **dominates the gate on every cell** — more than twice the gain on
`esci/realistic` and a sixteenth of the cost on `synth/official`. "Both" is
identical to "gate" to four decimals, which is the expected degeneracy: once the
gate excludes a candidate the penalty never gets to matter.

So `department_miss` ships as a calibratable weight and
`RerankConfig.department_gate` defaults to **off**. This is the whole of the
measured gain in my branch, and it comes from adding *evidence*, not from
re-ordering the arithmetic.

## Phase 6.2 gate — dense retrieval, not built

The condition in my brief was "only if, after 1.1 lands, re-measured recall
still leaves a large residual". Re-measured (`scripts.measure_recall`,
`--limit 400`):

| dataset × simulator | raw BM25 recall@500 | end-to-end HR@10 |
|---|--:|--:|
| esci1000 × esci | 0.830 (gold 0.799) | 0.915 |
| esci1000 × realistic | 0.858 (gold 0.906) | 0.835 |
| synth800 × realistic | 0.950 | 0.885 |

End-to-end HR@10 on `esci` **exceeds** turn-1 recall@500, because multi-turn
accumulation adds facets the opening query lacked. There is no large residual
for a dense channel to recover, and the brief's own warning applies: ablating on
`official` would show dense retrieval as useless for reasons that are an
artifact of verbatim phrasing. `scripts/build_embeddings.py` and the
`retrieval/` package are already in the tree and working, so this is a decision
not to spend the budget, not a missing capability.

The miss breakdown from `scripts.calibrate_rerank` (reported per run under
"Where the misses come from") splits misses into *target never in the pool*
versus *target in the pool and out-ranked*, and is the number to re-check if
anyone wants to revisit this.

## What I could NOT do, and why

- **Wire the profile tie-break into the live path.** `starter/agent.py` stores
  `user_profile` and never passes it; `agent.py` is A-owned. The retriever
  accepts it and applies it as a bounded stage-4 term, and I measured it by
  wrapping `Agent.respond` externally rather than shipping it unmeasured.
  `profile_scale` ships at the value that measurement supports.
- **Measure the turn-1 query fix end to end.** `Agent.respond` calls
  `record_message(..., "user", ...)` *after* `export()`, so C's non-empty-query
  contract cannot apply to the current turn. `scripts.measure_recall` still
  reports **67.8% empty pipeline queries at turn 1 on `esci1000 × esci`**, which
  costs a turn on most sessions and shows up as MTTC 3.37. This is C's open
  request to A (`REQUESTS.md`), not a retrieval defect, and it caps what any
  reranker can do about MTTC and MRR.

## Requests I filed in REQUESTS.md

- **D → C**: `attribute_stats` published on `last_diagnostics` (their request, done).
- **D → B**: `strength="soft"` now abstains rather than penalising (their request, done).
- **D → A**: pass `user_profile` into `retrieve_and_rerank` (open).

## What the next person needs to know

- **`RerankConfig()` and `RerankWeights()` with no arguments are the shipped
  behaviour.** To ablate anything, construct one, or set the matching
  `RERANK_*` env var — do not edit the defaults in place, or the parity check
  against the pre-change reranker stops being reproducible.
- **The scorer has one source of truth.** `stage_contributions` emits
  coefficients; `assemble` applies weights. The calibration harness scores a
  weight vector against that same method, so there is no second implementation
  to drift. If you add a scoring term, add it as a `Contribution` with a named
  weight and it becomes calibratable for free.
- **Stages 2–4 are ordered by magnitude, not lexicographically, and that is
  deliberate.** Strict lexicographic ordering below the gate would make stages 3
  and 4 dead code — stage-2 scores are continuous so exact ties never happen,
  and a tolerance-banded comparator is not transitive and cannot drive a sort.
  Only stage 1 is a true gate, which is where non-tradeability actually matters.
- **Latency improved as a side effect.** Median query time went
  **35.8 ms → 22.7 ms** (p90 43.7 → 31.1) because the old
  `_raw_constraint_score_details` re-normalised every candidate's full text on
  every turn *even with zero raw constraints*. Build time rose 3.5 s → 5.2 s and
  RSS 800 MB → 836 MB for the precomputed text — a good trade, but note it if
  the build budget ever tightens.
- **If you re-sweep `hold_until_turn`** (C's phase 5), do it after this change:
  the docstring at `state_manager.py:264` says to re-run the sweep after any
  retrieval change, and this is one.
