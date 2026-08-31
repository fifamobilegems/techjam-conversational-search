# Handover — Role D

## Status

complete — 6.1, 7.1, 7.2 and 7.4 delivered and measured; 6.2 **deliberately not
built**, with the gate measurement that says so. One decision is left open for
Role A on purpose (whether to adopt the calibrated weights), and one measurement
is blocked on an A-owned file.

**One-paragraph version.** The staged rerank and the prefilter are correct,
tested, and worth almost nothing on these benchmarks — the datasets do not
contain enough determinate constraint violations to exercise them. What did pay
was adding a signal nobody had listed (`details.Department`, 87% covered,
applied as a *penalty* rather than a filter) and calibrating the existing
weights. Shipped defaults move the paraphrased columns +0.0327 and +0.0134
technical with the verbatim columns flat, and lift hard-constraint
coverage in the Top-10 by **18 points** on `esci/realistic`. The calibrated
weight preset is worth a further +0.11 on both realistic columns but costs
−0.0548 on `public200/official`, so it ships **off by default** for Role A to
decide at integration.

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

- `starter/retriever.py`: `CALIBRATED_WEIGHTS` preset (opt-in, see 7.4); ranking keyed on `(-hard_violations, score)` so a
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
  `RERANK_PROFILE_TIEBREAK`, `RERANK_OVERFETCH`, `RERANK_MIN_SURVIVORS`, and
  `RERANK_WEIGHTS=default|calibrated`.

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

## Bench results — before vs after

n=200 stratified per cell. **before** = every switch off (bit-identical to the
pre-change reranker); **after** = shipped defaults, default weights.

| dataset x simulator | HR@10 before | after | MRR before | after | MTTC before | after | technical before | after | delta |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| esci1000/esci | 0.9150 | 0.9150 | 0.5372 | 0.5386 | 3.370 | 3.365 | 0.7713 | 0.7718 | +0.0005 |
| esci1000/realistic | 0.8350 | 0.8750 | 0.5035 | 0.5204 | 4.135 | 3.755 | 0.7059 | **0.7385** | **+0.0327** |
| esci1000/official | 0.9750 | 0.9750 | 0.7347 | 0.7308 | 2.565 | 2.565 | 0.8766 | 0.8754 | −0.0011 |
| synth800/official | 0.9800 | 0.9800 | 0.7020 | 0.6979 | 2.560 | 2.565 | 0.8694 | 0.8681 | −0.0013 |
| synth800/realistic | 0.8850 | 0.9000 | 0.4797 | 0.4899 | 3.515 | 3.375 | 0.7361 | **0.7495** | **+0.0134** |
| public200/official | 0.9900 | 0.9900 | 0.7814 | 0.7847 | 2.410 | 2.410 | 0.9012 | 0.9022 | +0.0010 |

Robustness-first shape: the paraphrased columns gain, the verbatim columns are
flat to within a thousandth.

## Hard-constraint coverage in the Top-10

HR@10 asks whether the one labelled product appeared. It says nothing about
whether the other nine respected the request, which is what staging exists to
protect — so this is the diagnostic that actually measures my change.

- **violation-free@10** — share of returned products with zero determinate hard
  violations.
- **satisfied@10** — share of stated hard constraints each returned product
  positively satisfies, averaged over the Top-10.

Both arms are scored against **one fixed yardstick**
(`RerankConfig(department_gate=True)`, a measurement choice, not the shipped
ranking config). Judging each arm by its own definition of "violation" would
make the before column perfect by construction, which is the first version of
this metric I wrote and had to throw away.

| dataset x simulator | violation-free@10 before | after | satisfied@10 before | after |
|---|--:|--:|--:|--:|
| esci1000/esci | 0.8475 | **0.9982** | 0.9553 | 0.9820 |
| esci1000/realistic | 0.8084 | **0.9882** | 0.8214 | 0.8876 |
| esci1000/official | 0.9782 | 0.9981 | 0.9897 | 0.9893 |
| synth800/official | 0.9604 | 0.9922 | 0.9903 | 0.9903 |
| synth800/realistic | 0.8600 | **0.9831** | 0.8538 | 0.8790 |
| public200/official | 0.9675 | 0.9979 | 0.9954 | 0.9954 |

**This is the real result of the branch.** On `esci/realistic`, HR@10 moves 4
points while violation-free@10 moves **18** — the competition metric was blind
to most of what the list was doing wrong, because it only ever looks at one
product. A shopper who says "for men" was previously shown women's listings in
roughly one slot in five.

## Phase 7.4 — weight calibration, held out by generator

`scripts/calibrate_rerank.py`. Coordinate search over the ~30 named weights,
fitted on `synth800/realistic` and evaluated on the **234 `provenance=="gold"`
ESCI rows** carrying real human E/S/C/I judgments — a different generator, which
is the whole point of the experiment.

| set | spec | n | technical before | after | delta |
|---|---|--:|--:|--:|--:|
| fit | synth800/realistic | 200 | 0.7495 | 0.8692 | +0.1197 |
| **eval (held out)** | **esci1000/esci, gold only** | **234** | **0.7651** | **0.8120** | **+0.0469** |

**Verdict: it transfers.** Roughly 40% of the fitted gain survives the change of
generator, which is the signature of real signal — pure circularity transfers at
zero, which is exactly what a 25-session pilot showed before there were enough
sessions to fit anything.

*Fidelity check:* the harness reports the fit set's "before" as **0.7495**,
matching the real bench's `synth800/realistic` at **0.7495** to four decimals.
The replay reproduces the official evaluator rather than approximating it.

### What the search learned

Two directions, both the same lesson:

- **Trust BM25 rank far more.** `fusion_scale` 100 → 774, `popularity_scale`
  1 → 0.24, `rating_coefficient` 0.4 → 0.07. The old weights let a popularity
  bonus spanning 0–1.9 compete against a fusion range of **1.46 across the
  entire 500-candidate pool**; rating count was quietly reordering BM25's
  output.
- **Stop punishing absent metadata.** `generic_miss` −12 → 0,
  `budget_loose_miss` −20 → 0, `vocabulary_miss` −20 → −12.8. On a catalog where
  `Color` is present on 4.9% of products, penalising "no color match" penalises
  **sparse metadata, not bad products**. The one penalty the search made
  *stronger* is `department_miss` (−25 → −55) — the only one backed by a
  well-covered field.

That is the department finding again, reached independently by search: **a
penalty should scale with how reliably the field is populated.**

### Validated on the real agent loop — and NOT made the default

The harness prunes candidate pools, so nothing it finds is believed until it
reproduces in the loop the evaluator actually runs:

| dataset x simulator | technical default | calibrated | delta |
|---|--:|--:|--:|
| esci1000/realistic | 0.7385 | **0.8440** | **+0.1055** |
| synth800/realistic | 0.7495 | **0.8690** | **+0.1195** |
| esci1000/esci | 0.7718 | **0.8161** | **+0.0443** |
| synth800/official | 0.8681 | 0.8668 | −0.0012 |
| esci1000/official | 0.8754 | 0.8616 | −0.0138 |
| public200/official | 0.9012 | 0.8474 | **−0.0548** |
| | | mean | **+0.0332** |

The gain is large and it holds outside the harness. But `docs/ARCHITECTURE.md`
names the ~0.89 official column **"the constraint to not destroy"**, and −0.0548
on the closest proxy to the official leaderboard is a trade the team should take
deliberately. I have therefore shipped the weights as a named preset,
**default off**:

```bash
RERANK_WEIGHTS=calibrated python3 -m tools.bench --limit 200
```

Recommendation: take it. Under robustness-first the two realistic columns are
the primary objective and they gain +0.11 each. But that is Role A's call at
integration, not one for the retriever to make silently — which is also why the
`before`/`after` bench table above is reported on the *default* weights, so the
two decisions stay separable.

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

The calibration harness settles it beyond argument. Splitting every miss into
*target never in the pool* versus *target in the pool and out-ranked*:

| set | sessions | target reachable | misses | ranking misses | recall misses |
|---|--:|--:|--:|--:|--:|
| synth800/realistic | 200 | **1.000** | 20 | 20 | **0** |
| esci1000/esci (gold) | 234 | **1.000** | 21 | 21 | **0** |

The target is in the candidate pool in **100%** of sessions, and **every single
miss is a ranking miss**. A dense channel buys candidates; there are no
candidates left to buy. Re-run `python3 -m scripts.calibrate_rerank` if the
extractor or the query builder changes materially — that table is the gate.

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
