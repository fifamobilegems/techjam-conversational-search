# Widening the Tier-2 gate to low-confidence Tier-1 reads

`README.md` listed this second under "given another week":

> widening the Tier-2 gate to cover low-confidence Tier-1 guesses, not just
> empty ones

This is what it took, what it costs, and — the part worth reading — where it
cannot help by construction.

Reproduce the cost side with no key and no spend:

```bash
python3 -m scripts.measure_gate --limit 200
```

---

## 1. The question the old gate could not ask

The Phase 8 gate escalated when Tier 0 produced nothing, Tier 1 produced
nothing, and no template matched. Three observable facts, auditable and
reproducible — the design is right and it stays.

Its limitation is that "Tier 1 produced something" is one bit. Consider:

    "a burgundy top"                                    -> color=burgundy
    "burgundy waterproof commuting jacket, pannier rack" -> color=burgundy

Both are Tier-1 turns with one operation. The first is a complete reading of the
message. The second read one word of five and dropped *waterproof*, *commuting*
and *pannier rack* — three requirements the gazetteer has no entry for, and
exactly the kind of span the model is good at. The old gate cannot tell them
apart, so it blocks both.

## 2. The signal: residual coverage

`HeuristicTurnExtractor.last_trace` now also reports what the cascade explained:

| field | meaning |
|---|---|
| `content_tokens` | message tokens after stopwords and conversational filler |
| `residual_tokens` | content tokens no operation and no lexicon match covered |
| `coverage` | `1 - residual/content` |
| `tier1_max_df` | document frequency of the commonest surface Tier 1 relied on |
| `tier1_max_words` | width of the widest Tier 1 gazetteer hit |

Coverage counts **unemitted lexicon matches as covered** on purpose. Tier 1
emits one operation per attribute, so a dropped second colour is still
vocabulary the cascade recognised and a model would add nothing there. It is
computed over token *strings* rather than offsets, because an operation carries
the shopper's wording in `raw_text` without a position — a template span is
assembled, not sliced. A repeated token therefore counts as covered wherever it
appears, which errs toward *less* escalation.

`TECHJAM_LLM_GATE=low_confidence` adds one opening:

```
tier0_operations == 0        # unchanged: Tier 0 blocks unconditionally
and not template_matched     # unchanged
and residual_tokens >= 2     # TECHJAM_LLM_GATE_RESIDUAL
and coverage <= 0.5          # TECHJAM_LLM_GATE_COVERAGE
```

Both conditions are load-bearing. Coverage alone escalates `"a burgundy top"` —
coverage 0.5, one residual token — which is a complete reading of a short
request, not a gap. The residual floor is what separates them. Filler-only
messages ("hi there thanks") report `content_tokens == 0` and `coverage == 1.0`,
so a greeting never costs a call.

**This is still a structural gate, not a learned confidence score.** Residual
coverage is a count, so the same message always produces the same decision and
the escalation rate — the cost line in the submission disclosure — stays
predictable and can be measured offline. `scripts/measure_gate.py` subclasses the
production `LLMTurnExtractor` and amputates the model rather than reimplementing
the gate, so the measurement cannot drift from the code that spends money.

## 3. Tier 0 still blocks, and that is the whole safety argument

`README.md` names the official column "the constraint to not destroy". The
widened gate cannot touch it, and this is measured rather than hoped.

On **public200 × official at n=200**, escalations by gate setting:

| gate | coverage | escalations |
|---|--:|--:|
| `empty` (shipped) | — | **57** |
| `low_confidence` | ≤ 0.5 | **57** |
| `low_confidence` | ≤ 0.67 | **57** |

Identical, because template phrasing means Tier 0 fires, and Tier 0 blocks
unconditionally in both modes. There is no thin Tier-1 read on the official
column to widen into.

That cuts both ways and should be stated plainly: **the widened gate cannot
improve the official score either.** It is a robustness change, not a
leaderboard change. Everything it does happens on paraphrased and real-query
phrasing.

## 4. Escalation cost across the matrix

All 200 sessions per cell, LLM tier off, gate answers counted rather than acted
on. Escalations = model calls that would have been made.

| dataset × simulator | `empty` (shipped) | c≤0.34, r≥2 | c≤0.5, r≥2 | c≤0.67, r≥2 | c≤0.34, r≥3 | c≤0.5, r≥3 | c≤0.67, r≥3 |
|---|--:|--:|--:|--:|--:|--:|--:|
| public200 × official | **57** | **57** | **57** | **57** | **57** | **57** | **57** |
| public200 × realistic | 48 | 84 | 96 | 101 | 79 | 88 | 92 |
| synth800 × official | **34** | **34** | **34** | **34** | **34** | **34** | **34** |
| synth800 × realistic | 78 | 113 | 126 | 133 | 104 | 109 | 111 |
| esci1000 × official | **59** | **59** | **59** | **59** | **59** | **59** | **59** |
| esci1000 × realistic | 76 | 126 | 144 | 151 | 119 | 131 | 132 |
| esci1000 × esci | 226 | 265 | 284 | 286 | 238 | 246 | 246 |
| **total** | **578** | **738** | **800** | **821** | **690** | **724** | **731** |

Read the three `official` rows first. **57 / 34 / 59, identical in every
column** — three datasets, six threshold settings, 1,200 sessions each. The
widened gate is provably inert on template phrasing.

Everything it costs lands on paraphrased and real-query phrasing. At the default
(`c≤0.5, r≥2`) the realistic columns roughly double (48→96, 78→126, 76→144) and
`esci × esci` rises 226→284, a 1.26× because those turns were *already*
escalating — real one-line queries frequently defeat the cascade outright, so
the `empty` opening had them first.

`r≥3` is the cheap setting and it is cheap for a reason worth knowing: real ESCI
queries are short, so demanding three unexplained content words filters most of
them out (`esci × esci` 284 → 246 while realistic barely moves). Raising the
residual floor does not trade cost against quality uniformly; it trades away
short real queries specifically.

## 5. Does it help?

Both arms, LLM tier **on**, n=200, `RERANK_WEIGHTS=calibrated`, real
`gpt-4o-mini` calls. This is the measurement the offline sweep cannot make.

| cell | `empty` | `low_confidence` | Δ | tokens `empty` → `low_confidence` |
|---|--:|--:|--:|--:|
| public200 × official | 0.8459 | 0.8471 | +0.0012 | 18,000 → 18,071 |
| public200 × realistic | 0.8866 | 0.8859 | −0.0007 | 13,827 → 28,891 |
| esci1000 × official | 0.8885 | 0.8878 | −0.0007 | 16,738 → 16,758 |
| esci1000 × realistic | 0.8963 | **0.9020** | **+0.0057** | 21,044 → 37,189 |
| esci1000 × esci | 0.8520 | 0.8489 | −0.0031 | 60,744 → 74,008 |
| **mean** | **0.8739** | **0.8743** | **+0.0004** | 130,353 → 174,917 |

**The verdict is neutral, and one row tells you why to believe that.**
`public200 × official` moved **+0.0012 on identical escalation counts** — 57
calls in both arms, the same 57 turns, the same prompts. The only difference is
that `gpt-4o-mini` is sampled, not seeded. So ±0.001–0.005 is this experiment's
noise floor, and the +0.0004 mean sits well inside it.

The one cell that clears the floor is `esci1000 × realistic` at **+0.0057**,
which is the cell the widening was aimed at: paraphrased shopper language where
the gazetteer gets a partial read. `esci1000 × esci` moves the other way
(−0.0031) — those turns were mostly escalating already through the `empty`
opening, so widening mainly adds calls on turns where Tier 1 had in fact read
enough.

Cost is **+34% tokens** (130,353 → 174,917) for a mean inside the noise. On the
public run that is roughly $0.0032 → $0.0043.

### Recommendation

**Do not flip the default.** `TECHJAM_LLM_GATE=empty` stays. The widened gate is
correct, cheap to audit, and provably harmless to the official column — but the
evidence that it *helps* is one cell above the noise floor, and a 34% cost
increase for +0.0004 is not a trade worth making before a submission.

It is worth keeping and worth re-running if the extraction cascade changes. The
mechanism is sound: the model does return the spans the gazetteer misses
(§6 below). What the numbers say is that on these simulators, the turns where
Tier 1 gets a *partial* read are mostly turns the ranker recovers from anyway —
the shopper discloses the same constraint again on the next turn, and MTTC
absorbs it. That is a property of multi-turn dialogue, not a flaw in the gate,
and it would not hold for a single-shot search box.

## 6. What a low-confidence escalation actually returns

For `"burgundy waterproof jacket for commuting with pannier rack"` — a turn the
old gate blocks because Tier 1 matched `burgundy` — the model returns:

```json
[{"text": "burgundy",     "attribute": "color"},
 {"text": "waterproof",   "attribute": "feature"},
 {"text": "jacket",       "attribute": "category"},
 {"text": "commuting",    "attribute": "use_case"},
 {"text": "pannier rack", "attribute": "feature"}]
```

186 prompt + 53 completion tokens. `burgundy` is discarded as already known; the
other four are added at `strength="soft"`, `confidence=0.5`, `provenance="tier2"`.

The additive contract is unchanged. The model may only add spans the rules
missed — it still cannot delete a span, clear a slot, or change retrieval
timing, so the offline score never depends on it and the deterministic fallback
remains the entire agent when the key, the package or the network is missing.

## 7. Configuration

| variable | default | effect |
|---|---|---|
| `TECHJAM_LLM_GATE` | `empty` | `low_confidence` adds the widened opening |
| `TECHJAM_LLM_GATE_COVERAGE` | `0.5` | escalate at or below this coverage |
| `TECHJAM_LLM_GATE_RESIDUAL` | `2` | minimum unexplained content words |

`empty` remains the default. Unknown values fall back to `empty` rather than
erroring, on the same principle as `RERANK_WEIGHTS`.
