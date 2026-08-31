# Handover — Role B (Extraction / NLU)

## Status

complete — 2.1, 2.2, 2.2b, 2.3 and 8 landed. One sub-item of 2.3 deferred with
a reason (see *What I could NOT do*).

## What I changed

- `scripts/build_lexicon.py` — **new.** Mines an attribute lexicon from the
  frozen catalog. Deterministic (verified by rebuilding and diffing), records
  its floors and the catalog SHA256 in the payload.
- `data/lexicon.json` — **new, committed build product.** 908 entries:
  colour 12 → **107**, material 10 → **107**, plus brand 105, category 519,
  size 29, style 41.
- `starter/extractor.py` — restructured as an explicit cascade. The old
  `extract()` body is now `_extract_tier0()`, **byte-identical**; the new
  `extract()` orchestrates tiers, applies polarity, and tags provenance. Adds
  `LexiconTagger` (Tier 1) and `PolarityScanner` (2.2b).
- `state/llm_extractor.py` — added `should_escalate()`, a structural gate, plus
  call accounting and `provenance="tier2"` on model-contributed spans.
- `tests/test_lexicon_cascade.py` — **new**, 25 tests. (Not in my ownership
  list, but a new file nobody else owns; flagging it here rather than assuming.)
- `docs/handover/TODO.md` — ticked Role B boxes only.
- `docs/handover/REQUESTS.md` — appended two entries.

## Contracts I introduced or changed

- `HeuristicTurnExtractor(lexicon_path=None)` — constructor now takes an
  optional lexicon path. `starter/agent.py` constructs it with no arguments and
  is unaffected. A missing lexicon degrades to an empty tagger, never an error.
- `HeuristicTurnExtractor.last_trace: dict` — per-turn structure record:
  `tier0_operations`, `tier1_operations`, `lexicon_matches`,
  `template_matched`, `negated_operations`. **Consumed by
  `LLMTurnExtractor.should_escalate()`**; any replacement extractor that wants
  Tier 2 must provide it (absent → the gate refuses to escalate).
- `HeuristicTurnExtractor.tier_counts: Counter` — cumulative yield.
- `starter.extractor.TIER1_ATTRIBUTES` — env-overridable ablation knob,
  default `color,material,size,style,brand`.
- `LLMTurnExtractor.should_escalate(state) -> bool`, `.calls`, `.gate_counts`,
  `.max_calls` (env `TECHJAM_LLM_MAX_CALLS`, default 250).
- Every `AttributeUpdate` now carries `provenance` ∈ `{tier0, tier1, tier2}`;
  Tier 1 and Tier 2 spans also carry `strength="soft"` and `confidence` 0.6/0.5.

## Bench results — before vs after

`python3 -m tools.bench --limit 200`, identical settings both runs.

| dataset x simulator | HR@10 before | HR@10 after | MRR before | MRR after | Technical before | Technical after | Δ |
|---|--:|--:|--:|--:|--:|--:|--:|
| public200 x official  | 0.9900 | 0.9900 | 0.7814 | 0.7814 | 0.9012 | 0.9012 | **+0.0000** |
| public200 x realistic | 0.8950 | 0.9350 | 0.5706 | 0.6048 | 0.7685 | 0.8047 | **+0.0362** |
| synth800 x official   | 0.9800 | 0.9800 | 0.7020 | 0.7020 | 0.8694 | 0.8694 | **+0.0000** |
| synth800 x realistic  | 0.8350 | 0.8850 | 0.4192 | 0.4797 | 0.6841 | 0.7361 | **+0.0520** |
| esci1000 x official   | 0.9750 | 0.9750 | 0.7347 | 0.7347 | 0.8766 | 0.8766 | **+0.0000** |
| esci1000 x realistic  | 0.7850 | 0.8350 | 0.4185 | 0.5035 | 0.6474 | 0.7059 | **+0.0585** |
| esci1000 x esci       | 0.8900 | 0.9150 | 0.4479 | 0.5372 | 0.7268 | 0.7713 | **+0.0445** |

Success criterion met: realistic and esci up, official identical to four
decimal places on both HR@10 and MRR across all three datasets.

Regression suite: 183 tests pass (158 pre-existing, 25 new).

## Per-tier extraction yield

Share of turn-1 opening messages whose operations came from each tier.

| dataset x simulator | Tier 0 | Tier 1 | still empty | Tier-2 escalation rate |
|---|--:|--:|--:|--:|
| public200 x official  | 100.0% | **0.0%** | 0.0% | 0.0% |
| public200 x realistic | 90.5% | 5.0% | 4.5% | 4.5% |
| synth800 x official   | 100.0% | **0.0%** | 0.0% | 0.0% |
| synth800 x realistic  | 87.5% | 8.0% | 4.5% | 4.5% |
| esci1000 x official   | 100.0% | **0.0%** | 0.0% | 0.0% |
| esci1000 x realistic  | 87.5% | 4.0% | 8.5% | 8.5% |
| esci1000 x esci       | 13.5% | **22.5%** | 64.0% | 64.0% |

Tier 1 firing 0.0% on every official column is the flatness guarantee made
visible: Tier 1 is gated on Tier 0 returning nothing, and on official phrasing
a template always matches.

The escalation column is the cost line for Phase 8: with the flag on, the LLM
would be called on 0% of official turns, ~4–9% of realistic turns, and 64% of
real ESCI turns.

## Token usage and cost disclosure

The deterministic path is the default and used **0 tokens** in every row above;
`TECHJAM_LLM_EXTRACTOR` is unset, no client is constructed, and no network call
is made. Every number in this handover is offline.

With the flag on, model `claude-opus-5` (override with `TECHJAM_LLM_MODEL`),
`max_tokens=1024`, one call per escalating turn, capped at 250 calls per
process. Not yet measured end-to-end — no run has been made with the flag on,
so latency and cost are unmeasured and must not be quoted as if they were.

## What I could NOT do, and why

- **The `mission` and `dialogue_act` axes (part of 2.3).** They live on
  `ExtractedTurn` in `state/state_manager.py`, which Role C owns, and the
  schema freeze declared `polarity`/`strength`/`confidence`/`provenance`/
  `superseded` but not those two. The plan also says to gate `mission` behind
  an ablation before anything consumes it, and nothing does yet, so adding it
  would have been an unmeasured field. Per-span provenance — the part 2.3 calls
  "the only way tier yield becomes measurable" — is delivered.
- **Tier 1 `category`.** Built and mined (519 entries), excluded by default on
  measurement. See the Role D request; it flips back on with one env var once
  soft constraints abstain instead of penalising.
- **Tier 2 end-to-end numbers.** Requires credentials; the gate and accounting
  are in place and tested offline.

## Requests I filed in REQUESTS.md

1. **B → D**, open — honour `strength="soft"` by clamping the negative branch
   to zero, which lets Tier 1 re-enable `category` (worth up to +0.045 more on
   esci if the penalty is what is holding it back).
2. **B → C**, informational — recording that `replay()` acts on `polarity`, and
   what that forced in the negation design.

## What the next person needs to know

1. **The one invariant.** Tier 1 runs only when Tier 0 returned zero
   operations. That single condition is why official phrasing is unchanged. If
   you relax it, re-run all three official columns before believing anything.
   `test_tier1_never_runs_when_tier0_produced_operations` guards it.

2. **Negation is not free, and it is not inert.** `replay()` drops the slot for
   a negated event, so a false positive deletes a real constraint. An audit of
   1,400 openings found 6 negations, 5 of them false positives on Tier 0 spans
   — catalog metadata whose own name contains a cue (`"No Closure closure"`,
   `"Non-Polarized"`). Hence `NEGATABLE_PROVENANCE = {tier1, tier2}` and a
   3-word span cap. Genuine negation is 1 turn in 1,400, matching the plan's
   "under 1%". Do not widen this layer expecting score.

3. **False-friend guards are in two places.** Multi-word lexicon entries
   spanning a cue (`"no show"`, mined from the catalog) and an explicit idiom
   set (`NEGATION_FALSE_FRIENDS`) for the ones the catalog does not evidence as
   entries — `"no iron"`, `"non slip"`. Both run *before* scope is computed.

4. **Rebuild the lexicon if the catalog changes.** `python3 -m
   scripts.build_lexicon`. The payload records `catalog_sha256`; nothing
   currently enforces it, which is a cheap improvement if anyone wants it.

5. **Audience words are not product types.** `men`, `women`, `kids` appear in
   every category path, so as category values they score +45 against most of
   the catalog while displacing the real product type — "hoodies for men" would
   tag `category=men`. They are stoplisted in `build_lexicon.py`.
