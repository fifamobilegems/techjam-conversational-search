# Demo video — script and shot list

**Target length:** 2:45–3:15. **Track:** backend/NLP, so this is a walkthrough of
CLI usage, inference examples and result analysis — no front-end required.

**Film on `role_A_hard_floor`** (PR #16). That branch enables the hard-match
floor, which is where the 0.861 on real queries comes from; on `main` that cell
is 0.816 and section 5 loses its payoff.

Every number below was verified on that branch on 2026-09-01 by running the exact
commands shown. Re-run the pre-flight block before filming; if a number has moved,
change the script, not the number.

**Trademark note (submission rule 4).** The catalog is Amazon Reviews 2023 data, so
product titles contain real brands. The two sessions chosen below keep brand names
out of the *narration*, but `esci_0000`'s turn 3 contains a brand in the customer's
reply. Either accept it as visible dataset content, blur that one line, or swap to a
brand-free session. Do not put a brand in the thumbnail or title.

---

## 0:00–0:20 · The problem

> **Narration.** "A shopper types four words into a search box. Our agent has ten
> turns to find the one product they actually want, out of fifty thousand — by
> asking questions, tracking what it learns, and re-ranking as it goes. Everything
> you're about to see runs offline, on the Python standard library, with zero API
> calls."

**SHOT 1 — title card.** Repo name, track, team. 3 seconds.

**SHOT 2 — screen recording, terminal.** `README.md` open, scrolled to the task
description and the metrics block:

```bash
sed -n '14,22p;70,80p' README.md
```

Hold on `TechnicalScore = 0.50 × HitRate@10 + 0.30 × MRR + 0.20 × Efficiency`.

---

## 0:20–0:50 · Architecture

> **Narration.** "Four components. The extractor turns a message into typed
> constraints. The state manager accumulates them across turns and handles
> overrides. The retriever runs BM25 over a SQLite full-text index of the whole
> catalog, then re-ranks in stages. And an evaluation harness scores every change
> against three different simulated shoppers, so we can tell a real improvement
> from an accident."

**SHOT 3 — code, `starter/agent.py` lines 85–140.** This is the whole turn loop in
one screen: record message → extract → update state → retrieve → decide → respond.

```bash
sed -n '85,140p' starter/agent.py
```

Highlight the comment block at lines 98–107 explaining why retrieval runs *before*
the policy decision.

**SHOT 4 — code, `state/state_manager.py`,** the `ConversationState` dataclass
(`slots`, `no_preference`, `asked_attributes`, event log). ~15 seconds.

**SHOT 5 — code, `starter/retriever.py`,** the FTS5 table creation and
`_bm25_search`. Show `FIELD_WEIGHTS`.

---

## 0:50–1:45 · Live demo — a real session

> **Narration.** "Here's a real session, driven by a genuine Amazon search query
> from the ESCI dataset. Turn one: 'eighteen-w women evening gown'. Too vague to
> answer, so the agent asks a question instead of guessing. Turn two, the shopper
> adds colour and occasion — the state now holds gold, and the search query is the
> shopper's own words plus everything learned so far. Turn three adds a style, and
> the target lands at rank one. Three turns, twenty-seven milliseconds a turn, zero
> tokens."

**SHOT 6 — screen recording, run it live.** This is the money shot; let it play at
real speed, it takes about a second:

```bash
python3 -m tools.trace_runner --dataset data/esci_set_1000.jsonl \
  --simulator esci --sample-ids esci_0000 --out-dir logs/demo -v
```

**SHOT 7 — the transcript, on screen as text.** Either `logs/demo/conversations.md`
or a slide. Annotate the STATE line growing turn over turn:

```
Turn 1  USER : 18w women evening gown
        STATE: {}
        AGENT: [asks a question]            TOP3: (holds — not confident yet)

Turn 2  USER : gold, prom
        STATE: {'color': 'gold'}
        QUERY: 18w women evening gown gold, prom gold
        TOP3 : B07QPR3J97, B09YGNSS7P, B01DBR8DCK

Turn 3  USER : embroidered, [brand]
        STATE: {'color': 'gold', 'style': 'embroidered'}
        TOP3 : B07ZH9ZWCJ  <-- TARGET, rank 1        ** HIT **
```

**SHOT 8 — optional, 10 seconds — the override case.** Only if the video is running
short. `esci_0019` shows the shopper reversing a preference mid-session
(`"scratch that, instead nickel free"`) and the agent holding rank 1 through it.

---

## 1:45–2:20 · Results

> **Narration.** "We score against three simulated shoppers: the official
> template-based one, a paraphrasing shopper, and real ESCI search queries. The
> provided baseline scores 0.107. We score 0.84 on the official public set, and
> 0.86 on real shopper queries — where the baseline effectively collapses."

**SHOT 9 — screen recording, run the official scorer:**

```bash
python3 -m evaluator.local_evaluator
```

Hold on the output block: `hit_rate_at_10: 0.95`, `mrr: 0.684`,
`recommended_technical_score: 0.8427`, and `reported_token_usage: 0`.

**SHOT 10 — the bench matrix.** Either run `python3 -m tools.bench --limit 100` on
camera or show a prepared table:

Full-size figures, verified on `role_A_hard_floor`:

| dataset × simulator | n | technical | HR@10 |
|---|--:|--:|--:|
| public200 × official | 200 | 0.843 | 0.950 |
| synth800 × official | 800 | 0.841 | 0.930 |
| esci1000 × official | 1000 | 0.827 | 0.918 |
| esci1000 × realistic | 1000 | 0.861 | 0.987 |
| **esci1000 × esci** (real queries) | 1000 | **0.861** | **0.983** |
| provided BM25 baseline | 200 | 0.107 | 0.125 |

If you run `tools.bench` live, pass `--limit 100` for speed and say "a hundred
sessions per cell" — the numbers shift slightly from the full-size table above.

---

## 2:20–2:50 · How we found the biggest win

> **Narration.** "One diagnostic drove the largest single improvement. We traced
> every failure and found that of twenty-one misses, only one was a retrieval
> failure — sixteen had the correct product ranked *first* by BM25, and the
> re-ranker was pushing it out of the top ten. Constraint scores span seventy-five
> points; rank fusion spans one point six. So we reserve two slots for the
> strongest lexical matches. On real queries that moved us from 0.816 to 0.861,
> and nothing else regressed."

**SHOT 11 — the evidence, as text on screen:**

```
21 misses on esci1000 × esci
  1  genuine recall failure
 16  target was BM25 rank #1, ejected by the re-ranker

esci_0007  TARGET: bm25#1  constraint=+20.0  final=22.7  -> rank 92
           RANK-1: bm25#3  constraint=+75.0  final=77.4
```

**SHOT 12 — code, `starter/retriever.py`, `_apply_hard_floor`** plus the measurement
comment above `hard_floor`. This shows the fix and its evidence in one frame.

```bash
grep -n "_apply_hard_floor" -A 20 starter/retriever.py
```

---

## 2:50–3:05 · Cost, latency, close

> **Narration.** "Model cost: zero. Token usage: zero. The scored path is
> deterministic and standard-library only, so it runs with no network at all —
> which matters, because official scoring may be run offline. Median latency is
> twenty-seven milliseconds per turn after a five-second index build. An optional
> LLM extraction tier exists behind a flag; we measured it at two hundred sessions,
> it did not improve the score, and it ships disabled."

**SHOT 13 — split screen or two quick cuts:**
- `docs/handover/REQUESTS.md`, the LLM A/B table (tried, measured, rejected)
- `.env_example`, the LLM tier block showing it defaults off

**SHOT 14 — closing card.** Repo URL, the four headline numbers:

```
Technical score   0.843 official  ·  0.861 real queries
Baseline          0.107
Cost              $0.00  ·  0 tokens  ·  fully offline
Latency           27 ms median per turn
```

---

## Pre-flight checklist

```bash
# 1. correct branch, catalog present
git checkout role_A_hard_floor
ls data/catalog.jsonl || gzip -dc catalog.jsonl.gz > data/catalog.jsonl

# 2. LLM tier OFF so the demo is deterministic and free
grep TECHJAM_LLM_EXTRACTOR .env    # must be 0 or absent

# 3. rehearse the two commands that run on camera
python3 -m tools.trace_runner --dataset data/esci_set_1000.jsonl \
  --simulator esci --sample-ids esci_0000 --out-dir logs/demo -v
python3 -m evaluator.local_evaluator

# 4. confirm the numbers still match the script
python3 -m unittest discover tests
```

**Recording tips.** Terminal at ~16pt, dark theme, window cropped to remove
personal paths. Editor at ~14pt with the minimap off. Never show `.env` with a real
key on screen — it is gitignored for a reason. Keep every code shot on screen for at
least four seconds; viewers cannot read faster than that.
