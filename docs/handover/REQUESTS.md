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
Status: done — Role A 2026-08-31 (see A → B/C/D entry at end)

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
Status: done — Role A 2026-08-31 passes `self._profiles.get(session_id)`; stage stays inert until D raises `profile_scale`

### 2026-08-31 · D → A · Decide whether to adopt `RERANK_WEIGHTS=calibrated`
`scripts/calibrate_rerank.py` fitted the ~30 reranker weights on
`synth800/realistic` and held them out on the 234 ESCI `provenance=="gold"`
rows (real human E/S/C/I labels). They transfer: held-out technical
0.7651 → 0.8120. Validated on the real agent loop, n=200 stratified:

| dataset x simulator | default | calibrated | delta |
|---|--:|--:|--:|
| esci1000/realistic | 0.7385 | 0.8440 | +0.1055 |
| synth800/realistic | 0.7495 | 0.8690 | +0.1195 |
| esci1000/esci | 0.7718 | 0.8161 | +0.0443 |
| synth800/official | 0.8681 | 0.8668 | −0.0012 |
| esci1000/official | 0.8754 | 0.8616 | −0.0138 |
| public200/official | 0.9012 | 0.8474 | **−0.0548** |
| | | mean | +0.0332 |

`docs/ARCHITECTURE.md` calls the ~0.89 official column "the constraint to not
destroy", so D did **not** make this the default: it is a deliberate
robustness-for-official trade and belongs to whoever owns final integration.
D recommends adopting it — under robustness-first the two realistic columns are
the primary objective and they gain +0.11 each — but the branch ships with
`RERANK_WEIGHTS=default` so the choice stays explicit and reversible. Enable
with `RERANK_WEIGHTS=calibrated`. Re-run
`python3 -m scripts.calibrate_rerank` after any extractor or query-builder
change; the fitted values are not portable across those.
Status: done — Role A 2026-08-31 ADOPTED. `Agent.__init__` sets
`os.environ.setdefault("RERANK_WEIGHTS","calibrated")` (overridable). Re-measured
on this branch with items 1&3 applied, n=150 head: realistic/esci +0.076 mean
technical, official −0.030 mean (public200-official −0.063). Robustness-first.

### 2026-08-31 · D → A · Measured cost of the `record_message` ordering bug
Reinforcing C's open request with a number, since it caps what retrieval can do.
`Agent.respond` calls `record_message(..., "user", ...)` *after* `export()`, so
`build_search_context()` cannot see the current turn's text.
`python3 -m scripts.measure_recall --limit 400` still reports **67.8% empty
pipeline queries at turn 1** on `esci1000 × esci` (raw recall@500 0.830 vs
pipeline 0.152). Turn 1 is therefore wasted on most real-query sessions, which
shows up as MTTC 3.37 and caps both MRR (30% of the score) and efficiency (20%).
No reranker change can recover a turn that retrieved nothing. This is the
highest-value remaining item D can see and it is not in a D-owned file.
Status: done — Role A 2026-08-31. Fixed in `agent.py` (record user message
before export). `measure_recall` now updated to mirror the live flow: esci×esci
turn-1 pipeline recall@500 0.152 → 0.833, empty 67.8% → 0.0%. Live-agent score
gain is smaller than the recall gain because `hold_until_turn=2` already skips
turn-1 emission; item 3 mainly adds the current turn's words immediately and
unblocks a credibility-based turn-1 emit (C's Phase 5).

### 2026-08-31 · C → D · `attribute_stats` reconciliation
Verified D's published `attribute_stats` contract is present on
`CatalogRetriever.last_diagnostics`. C now normalizes D's Shannon-entropy
`instability` field against observed value support and consumes the mapping
directly. The original C → D request is satisfied; no D action remains.
Status: done

### 2026-08-31 · A → B · LLM tier switched to the OpenAI protocol (I edited your file)
**Heads-up: Role A edited `state/llm_extractor.py`, which is B-owned.** Done on
explicit instruction from the team lead, who has an OpenRouter key and no
Anthropic one. Flagging it rather than hiding it; revert freely if you disagree.

Only the transport changed — 7 lines across `_build_client` and `_call`. The
extraction contract, structural gate, verbatim guard, additive-merge logic,
`OUTPUT_SCHEMA`, and cost guard are all untouched.

- `import anthropic` → `import openai`; `anthropic.Anthropic(...)` →
  `openai.OpenAI(...)` (SDK reads `OPENAI_API_KEY` / `OPENAI_BASE_URL`).
- `messages.create` → `chat.completions.create`; `SYSTEM_PROMPT` moves into the
  messages array as a `system` role; `output_config` → `response_format`
  with `{"type":"json_schema","json_schema":{"name","strict","schema"}}`.
- usage `input_tokens`/`output_tokens` → `prompt_tokens`/`completion_tokens`.
- parsing `response.content[].text` → `response.choices[0].message.content`.
- `DEFAULT_MODEL` `claude-opus-5` → `gpt-4o-mini`.
- New optional `requirements-llm.txt` (`openai>=1.40`); `.env_example` updated.

`OUTPUT_SCHEMA` needed no change — it already met OpenAI strict mode
(`additionalProperties:false` everywhere, all properties in `required`).

Verified: client constructs; request carries system+user roles and a strict
json_schema; response parsing and token accounting correct (mocked, no live key
available); `OPENAI_BASE_URL` honoured for OpenRouter; **flag off leaves the
scored path byte-identical** (esci1000×esci 0.8238, 0 tokens) and 197 tests green.

Two things B should know. The tier had **never actually run** — neither
`anthropic` nor `openai` was installed, so `_build_client` always returned `None`
at the `ImportError`; every measured number is pure-offline. And the bare
`except Exception` in `extract()` means a wrong key, wrong model id, or an
unsupported `response_format` are all indistinguishable from success: no error,
0 tokens. On a gateway the model id must be namespaced (`openai/gpt-4o-mini`) or
it 404s into that silent no-op. Verify by asserting non-zero token usage, never
by "it ran clean". Plan and gotchas: `docs/plan_openai_migration.md`.
Status: done

### 2026-08-31 · A → B/C/D · Second-part integration landed (branch role_A_second_part)
Addressed the four A-facing requests above, all in Role-A-owned files
(`starter/agent.py`, `scripts/measure_recall.py`, `.env_example`), no edits to
`extractor.py` / `retriever.py` / `state/*` / `evaluator/*`.

1. **Item 3 — record user message before export** (`agent.py`). `record_message`
   moved ahead of extraction/state export; assistant record unchanged (one per
   message). `measure_recall` updated to mirror the live ordering. Effect:
   esci×esci turn-1 pipeline recall@500 0.15 → 0.83, empty% 68 → 0. Live score
   +0.012 technical on esci×esci (smaller than recall gain — `hold_until_turn=2`
   skips turn-1 emit; this unblocks C's credibility emit / Phase 5).
2. **Item 1 — user_profile passed** into `retrieve_and_rerank` (`agent.py`).
   Inert until D raises `RerankWeights.profile_scale` (currently 0.0). No risk.
3. **Item 2 — RERANK_WEIGHTS=calibrated adopted** via overridable setdefault.
4. **`.env_example`** added at repo root documenting all env vars by owner.

Combined before→after vs pristine main@458fdee (n=150 head, technical):
esci×esci 0.775→0.824, synth800-realistic 0.762→0.911, esci1000-realistic
0.740→0.865, public200-realistic 0.800→0.867; official cost: public200-official
0.900→0.846, esci1000-official 0.869→0.848, synth800-official ≈flat. 197 tests
green. Robustness-first trade is deliberate and reversible (`RERANK_WEIGHTS=default`).
Status: done

### 2026-08-31 · A → B · LLM tier measured and NOT recommended for scoring runs
Role B's Tier-2 layer now works end to end against the OpenAI protocol (see the
migration entry above). Measured on the real agent loop, esci1000 x esci,
identical head-selected samples, `RERANK_WEIGHTS=calibrated`:

| n | flag | technical | HR@10 | MRR | tokens | est. cost | wall clock |
|--:|---|--:|--:|--:|--:|--:|--:|
| 50 | on | 0.7911 | 0.900 | — | 46,893 | $0.009 | 96s |
| 50 | off | 0.7572 | 0.860 | — | 0 | $0 | 7s |
| 200 | on | 0.8206 | 0.920 | 0.6817 | 154,265 | $0.029 | 289s |
| 200 | off | **0.8256** | **0.930** | 0.6754 | 0 | $0 | 18s |

**The n=50 result (+0.034) did not replicate.** It rested on two extra sessions
out of fifty. At n=200 the tier is neutral-to-slightly-negative: −0.005
technical, −0.010 HR@10, +0.006 MRR, for 16x the wall clock and ~$0.14 per 1000
sessions. Anyone re-running this: n=50 is far too small for a 0.03-scale claim
on this metric.

Recommendation: keep `TECHJAM_LLM_EXTRACTOR` unset for scoring and benchmarking.
The gate, the verbatim guard and the additive-only design are all sound — the
model simply is not adding spans that change ranking on this catalog. Nothing to
fix in B's code; this is a deployment decision, and it is also a defensible
submission story (tried, measured at n=200, rejected on evidence).
Status: done (informational)
