# Cross-role requests — APPEND ONLY

When you need a change in a file you do not own, append a request here rather
than editing the file. Append-only means no merge conflicts. Newest at the bottom.

Format:
```
### <date> · <from role> → <to role> · <one-line title>
<what you need, which file/function, and why>
Status: open | done | wontfix
```

---

### 2026-08-31 · A → C/D · Verify 0.1 syntax fix already on main (no action needed)
The two merge-damage syntax errors named in Phase 0.1 (`starter/agent.py:91`
duplicated `raw_constraints=` kwarg; `starter/retriever.py:222` duplicated
`raw_constraints` parameter) are **already resolved on `main`** by commit
`599ab02` ("Fix duplicate raw_constraints argument from merge"), which merged via
PR #8. On the current `main` (`0491eaf`) both files `python3 -m py_compile`
cleanly and each has a single `raw_constraints` binding. Role A therefore made no
edit to `retriever.py` (Role D-owned). Branch from `main` as planned; no
unblock-merge is outstanding.
Status: done

### 2026-08-31 · C → A · Record the current user message before state export
`Agent.respond()` currently calls `StateManager.update()` and `export()` before
`record_message(..., "user", user_message, turn)`. This leaves turn 1 without
raw text, so C's `build_search_context()` cannot meet its non-empty-query
contract in the live flow. Move that user-message record before extraction/state
export (or provide `user_message` to the state update) while preserving one
record per message.
Status: open

### 2026-08-31 · C → A · Make retrieval policy diagnostics available in-turn
`choose_next_attribute()` and `should_emit_recommendations()` currently execute
before `CatalogRetriever.retrieve_and_rerank()`, but adaptive clarification and
the credibility test require the current candidate IDs, scores, and rank-1 vs
rank-10 margin. Please implement a two-stage Agent flow (provisional retrieval
then policy decision) or call a C-provided diagnostics setter before the final
decision. Do not emit a list until the policy decision; keep `hold_until_turn`
as fallback.
Status: open

### 2026-08-31 · C → B/D · Schema freeze is available
`state.state_manager.AttributeUpdate` and `ExtractedTurn` now expose
`polarity: Literal["must", "prefer", "negate"] = "must"`,
`strength: Literal["hard", "soft"] = "hard"`, `confidence: float = 1.0`,
`provenance: Literal["legacy", "tier0", "tier1", "tier2"] = "legacy"`, and
`superseded: bool = False`. Every new `raw_constraints` span carries the same
keys with identical defaults. Use `tier0`/`tier1`/`tier2` in new extraction;
legacy callers retain deterministic Tier-0 behavior.
Status: done

### 2026-08-31 · C → A · Extend answerability measurement contract
`scripts/measure_attribute_yield.py` currently counts constraint classes and
special-cases `other`; it does not measure answerability by mission or fit a
weight. Extend its machine-readable output with normalized per-attribute,
per-mission answerability suitable for `state/clarification.py`, and document
how the fitted coefficient is selected. C will consume this artifact rather
than hand-setting a value.
Status: open

### 2026-08-31 · C → D · Publish compact per-attribute provisional statistics
For `state.clarification.question_value`, add `attribute_stats` to the
retriever's `last_diagnostics`: for each typed attribute, a `coverage` fraction,
`value_counts` over the provisional candidate set, and an `instability` value.
`StateManager.set_retrieval_diagnostics()` consumes this exact mapping. Existing
`candidate_scores` already supports C's score/margin credibility calculation.
Status: open

### 2026-08-31 · C → A/D · Autonomy-patch assumption
`origin/main` fetch could not complete because the host's Git SSL issuer is
untrusted. The available `origin/main` ref has none of the requested Agent or
retriever integrations, and the self-check still prints `''`; C proceeds with
the current contracts and will reconcile against local `main` before merge.
Status: done

### 2026-08-31 · B → D · Honour `strength="soft"` so Tier 1 can emit `category`
`starter/retriever.py::_constraint_score_details` scores every constraint the
same way regardless of how it was obtained, and a miss is a penalty rather than
an abstention: category/color/material `-20`, brand `-50`. Tier 1 values are
gazetteer guesses, not recited requirements, so they are tagged
`strength="soft"`, `confidence=0.6` — but nothing reads those fields yet.

Measured consequence, esci1000 x esci, 200 samples, TechnicalScore:

| Tier 1 attribute set | Technical |
|---|--:|
| Tier 1 off | 0.7268 |
| `category` only | 0.6437 |
| `category,color,material` | 0.6684 |
| everything except `category` | **0.7713** |

`category` is therefore excluded from `TIER1_ATTRIBUTES` by default. A category
guessed from a short real query is often right in spirit and wrong in wording
("bras" against a listing filed under "Lingerie Accessories"), and the `-20`
lands on the true target.

Request: when a constraint carries `strength="soft"`, clamp its negative branch
to `0.0` (abstain) while keeping the positive branch. That turns a wrong soft
guess into a no-op instead of a penalty, at which point `category` can be
re-enabled with:

    TIER1_ATTRIBUTES=category,color,material,size,style,brand python3 -m tools.bench

No change to Tier 0 behaviour: those spans keep `strength="hard"`.
Status: open

### 2026-08-31 · B → C · `replay()` acts on polarity — noting the coupling
`state_manager.replay()` skips the slot assignment when an event carries
`polarity="negate"`, so the polarity layer is behaviourally live rather than
advisory. That is the right call and no change is requested; recording it
because the coupling is easy to miss.

It also set the design constraint on 2.2b. An audit over 1,400 opening messages
found 6 negations, of which 5 were false positives on **Tier 0** spans —
verbatim catalog metadata whose own name contains a cue (`"No Closure closure"`,
`"Non-Polarized"`, a description reading `"Never Truly Part"`). Negating those
dropped real constraints and cost `-0.0006` on synth800 x official. Polarity is
therefore restricted to `provenance in {tier1, tier2}` and spans of at most 3
words (`NEGATABLE_PROVENANCE`, `MAX_NEGATABLE_SPAN_WORDS` in
`starter/extractor.py`). After that restriction the official columns are
identical to four decimal places on HR@10 and MRR.
Status: done (informational)

### 2026-08-31 · D → C · `attribute_stats` is now published on `last_diagnostics`
Answering C's request above. `CatalogRetriever.last_diagnostics["attribute_stats"]`
now carries, for `color`/`material`/`size`/`style`/`brand`, a mapping of
`coverage` (fraction of the provisional top-200 with a usable value),
`value_counts` (top 12 values), and `instability` (Shannon entropy of those
values, in bits). Values are read from structured `details` where present and
from `store` for brand. Coverage is genuinely low for most attributes — that is
the measurement, not a defect: `Color` is on 4.9% of the catalog and `Size` on
1.9%, which is precisely the guard `catalog_coverage(a)` exists to apply.
`candidate_scores` additionally now carries a `violations` count and a
`stage_scores` breakdown per candidate alongside `final_score`.
Status: done

### 2026-08-31 · D → B · `strength="soft"` now abstains instead of penalising
Answering B's request above. When every live span for an attribute carries
`strength="soft"`, the reranker keeps the positive branch and clamps the
negative branch to `0.0` (`RerankConfig.soft_abstain`, default on). A wrong
Tier 1 guess is now a no-op rather than a `-20`/`-50` on the true target. The
attribute still appears in `constraint_details` at `0.0` so an abstention stays
distinguishable from an attribute that was never considered.
Please re-run the `TIER1_ATTRIBUTES=category,...` sweep against this branch
before deciding whether to re-enable `category`; D measured the reranker side
only and did not change `TIER1_ATTRIBUTES`.
Status: done

### 2026-08-31 · D → A · Pass `user_profile` into `retrieve_and_rerank`
`starter/agent.py:54` stores `user_profile` per session and never reads it.
`CatalogRetriever.retrieve_and_rerank` now accepts an optional trailing
`user_profile: dict | None = None` and applies `preference_tags` /
`average_prior_rating` as a bounded stage-4 tie-break (Decision 9). Until the
Agent passes `self._profiles[session_id]`, the stage is inert. The parameter is
keyword-safe and defaults to `None`, so adding it is a one-line change with no
behavioural risk. Note `RerankWeights.profile_scale` currently defaults to
`0.0`: profile scoring stays off until it can be measured against a live
profile, so wiring it up alone changes nothing until that default is raised.
Status: open
