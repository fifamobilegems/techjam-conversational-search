# Handover — Role C

## Status
partial

## What I changed
- `state/state_manager.py`: raw user transcript is included in search context;
  immutable events replay into slots, no-preference, and raw-span caches.
- `state/state_manager.py`: added policy diagnostics setter, credibility test,
  and exported event/demoted/negated retrieval views.
- `state/clarification.py`: added typed, catalog-driven question-value policy;
  it deliberately excludes evaluator-privileged `other` when diagnostics exist.
- `state/clarification.py`: reconciled D's live `attribute_stats` contract by
  normalizing its entropy-in-bits instability signal; supports future measured
  `answerability_by_mission` artifacts keyed by the current intent.

## Contracts I introduced or changed
- `AttributeUpdate` / `ExtractedTurn` / raw span: `polarity="must"`,
  `strength="hard"`, `confidence=1.0`, `provenance="legacy"`,
  `superseded=False`; B supplies tier provenance and D consumes effective views.
- `StateManager.set_retrieval_diagnostics(session_id, diagnostics)`: Agent must
  call after provisional retrieval; diagnostics need `attribute_stats` for
  clarification and existing `candidate_scores` for credibility.

## Bench results — before vs after
| dataset x simulator | HR@10 before | HR@10 after |
|---|---:|---:|
| all three simulators | not run | not run |

Recall@500 expected from 1.1 is 0.030 → 0.823 (+0.793), but it is not yet
measurable in the live path because Agent records the current user message after
state export. No fabricated before/after result is reported.

## What I could NOT do, and why

- Run valid end-to-end before/after benches: A-owned Agent ordering prevents
  current-turn raw-query, diagnostics, and credibility behavior from executing.
- Fit answerability by mission: A-owned measurement script currently exposes
  only class yields, not the specified fitted artifact.
- Re-sweep credibility floors: it requires the two-stage Agent path and live
  current-turn scores.
- The local bench/evaluator invocations did not return results and were stopped
  after leaving orphan Python processes; no benchmark outcome is claimed.

## Requests I filed in REQUESTS.md

- A: record user text before export; provide two-stage policy orchestration;
  extend answerability measurement.
- D: publish per-attribute provisional candidate statistics.

## What the next person needs to know

The compatibility fallback retains `other` only while Agent has not provided
`attribute_stats`. Once it does, the adaptive policy takes over and the official
simulator score decrease must be reported explicitly as the accepted trade.

Autonomy re-check: the local `origin/main` ref still lacks A/D integration, the
network fetch was blocked by SSL issuer validation, and the prescribed raw-query
self-check still returned `''`. Role C's commits were rebased against available
local `main` and fast-forward merged there; A and D have unfinished TODO items,
so Role C is not the final integrator.
