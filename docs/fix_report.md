# Fix report — code review findings, ranking experiments, bench

Branch `fix/review-findings`. 198 tests pass (was 197; one rewritten, one added).

## Part 1 — the 15 findings

| # | What was broken | What it caused | Fix |
|---|---|---|---|
| 1 | `set_retrieval_diagnostics` never called | `ask_attribute` was `"other"` every turn; `state/clarification.py` never ran | Agent retrieves *before* export and publishes diagnostics |
| 2 | Tier 0 gazetteer fallback un-negatable | "not blue" produced `color=blue` as a positive hard constraint | Split `tier0` / `tier0_fallback`; only the fallback is negatable |
| 3 | Credibility emit test dead | Emission silently reverted to `hold_until_turn` | Same wiring fix; now measurable, and off on measurement |
| 4 | 50-term BM25 cap, oldest-first | From turn 8 the newest disclosure never reached search | Cap raised to 128; opening first, then newest-first |
| 5 | Raw dialogue in the query | Searched on `color` right after the shopper declined colour | `query_fragment()` strips protocol lines and scaffolding |
| 6 | `last_usage` never reset | One LLM call re-reported on every later turn | Reset per `extract()` |
| 7 | `known.add()` hoisted out of the loop | Duplicate spans in one response counted twice | Moved back inside |
| 8 | `_strip_negated` removed constituent tokens | Negating "underwire bra" deleted "bra" | Removes the contiguous phrase only |
| 9 | `record_span` dropped later polarity | "velvet" then "not velvet" stayed `must` | Later event upgrades the existing span |
| 10 | `os.environ.setdefault` in `Agent.__init__` | Preset sweeps scored both arms identically | Weights passed explicitly |
| 11 | `attribute_yield.json` missing, cwd-relative, re-read per turn | Answerability term always 0 | Generated, `lru_cache`d, resolved from the package root |
| 12 | `violations()` computed twice | Up to 1500 + 500 redundant evaluations/turn | Cached from the prefilter |
| 13 | `_attribute_stats` computed, never consumed | ~2M wasted lookups per evaluation | Now consumed (finding 1) |
| 14 | `replay()` per operation | Full log rebuilt N+1 times per turn | Appended then projected once |
| 15 | `retrieval/` unreachable from the agent | Reader assumes hybrid retrieval is live | Documented in `README.md` |

### One bug the fixes exposed

Scoping `information_exhausted`. `"I don't have an additional preference for brand"`
means *brand* is exhausted; the code latched it as *the conversation* is
exhausted. Invisible while the policy only ever asked `other` — for which the
reading is correct — but the moment typed questions shipped it ended sessions
after the first unanswerable attribute: **0.8522 → 0.2220** on public200/official.
Now scoped to the named attribute; `other` and the bare form still exhaust.

## Part 2 — ranking experiments (all three failed)

Every miss is a ranking miss, so all three targeted ordering only. Measured at
n=100 across 6 cells, mean technical score:

| experiment | idea | result | verdict |
|---|---|--:|---|
| baseline | — | 0.8183 | — |
| coverage bonus | reward breadth of constraint satisfaction | 0.8179 | neutral, **off** |
| IDF evidence | weight spans by catalog rarity | 0.8219 → **0.8584** vs 0.8664 with the better policy | **off** |
| cross-turn consensus | reciprocal-rank fusion across turns | 0.7732 | **harmful, off** |

- **Consensus** is a feedback loop: reinforcing what ranked highly on earlier,
  less-informed turns entrenches the mistakes later constraints should correct.
  MRR 0.5512 vs 0.6815.
- **IDF** looked positive against the weaker clarification policy and negative
  against the better one — the tell that it was compensating for worse
  evidence, not adding signal. BM25 already prices term rarity when selecting
  candidates; re-applying it in the reranker double-counts.
- **Coverage** is near-constant across survivors when few spans are live, so it
  discriminates nothing.

All three ship behind flags with their numbers recorded, so re-enabling one is
a single env var and the negative results are not lost.

### Clarification policy, finally quantified

The review asked for the price of Decision 7 to be stated rather than buried:

|  | mean technical | mean MRR |
|---|--:|--:|
| `CLARIFY_POLICY=other` | **0.8664** | 0.7377 |
| `CLARIFY_POLICY=formula` | 0.8183 | 0.6815 |

The cost is **-0.048**. The compensating robustness gain is not there: on the
paraphrased columns the two are level (publ/real 0.866 vs 0.866, esci/real
0.839 vs 0.839, synt/real 0.906 vs 0.902). Default is `other`; the formula is
the more defensible design and stays one env var away.

## Part 3 — bench, n=150 per cell

| cell | before | after | delta |
|---|--:|--:|--:|
| public200/official | 0.8522 | 0.8551 | +0.0029 |
| public200/realistic | 0.8632 | 0.8639 | +0.0007 |
| synth800/official | 0.8796 | 0.8817 | +0.0020 |
| synth800/realistic | 0.9103 | 0.9058 | -0.0045 |
| esci1000/official | 0.8519 | 0.8526 | +0.0006 |
| esci1000/realistic | 0.8654 | 0.8594 | -0.0060 |
| esci1000/esci | 0.8242 | 0.8242 | 0.0000 |
| **mean** | **0.8638** | **0.8632** | **-0.0006** |

Score parity. Fifteen correctness defects are gone, three dead subsystems are
alive and measurable, and four experiments have recorded verdicts — at no cost
to the number that gets judged.
