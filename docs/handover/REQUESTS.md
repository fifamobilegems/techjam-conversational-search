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

### 2026-08-31 · C → team · Request-completion audit (approval to delete)
Checked against the current working tree and commit history. The two requests
already marked done are ready to delete if the team wants a shorter log:
`A → C/D Verify 0.1 syntax fix` and `C → B/D Schema freeze`.

The four remaining C requests are **not complete**: Agent still records the
user message after export; Agent still decides policy before retrieval; the
answerability script has no per-mission/fitted output; and retriever diagnostics
have no `attribute_stats`. Keep all four open.
Status: done
