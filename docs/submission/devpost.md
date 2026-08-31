# Devpost submission content

Copy-paste ready. Field names match the Devpost form.

---

## Project name  *(60 char limit)*

```
Constraint Compass
```
`18 / 60`

**Alternatives if that reads too abstract:**
- `Turn Ten` — `8 / 60`
- `Shopper State Machine` — `21 / 60`
- `The Honest Shopping Agent` — `25 / 60`

---

## Elevator pitch  *(200 char limit)*

```
Finds a shopper's hidden product among 50,000 items in under 3 turns. 7.9x the baseline for $0.003 of GPT-4o-mini, and we built two adversarial shoppers to prove it isn't overfitted.
```
`182 / 200`

---

## About the project  *(Markdown)*

```markdown
## Inspiration

We started by reading the evaluator instead of the problem statement.

Buried in it was a detail that changed the whole project: the simulated customer
builds its requirements by flattening the target product's own catalog metadata.
So a "shopper" doesn't say *"something warm for hiking"* — it says
*"what matters is: Package Dimensions: 20.95 x 14.15 x 2.7 inches; Item model
number: EZ07707GD26-PZ."*

That is a near-unique database key, recited in prose. An agent that matches those
strings verbatim scores extremely well and has learned nothing about shopping.

We could not tell from a single score which of those two things we were building.
So before optimising anything, we built a way to find out.

## What it does

It plays a shopper's assistant. Each turn it reads a message, updates what it
believes the shopper wants, asks at most one question, and returns a ranked
Top-10 from a frozen 50,000-product catalog. The session ends the moment the
hidden target appears in that list.

On the 200-session public set it scores **TechnicalScore 0.8468** — Hit Rate@10
0.955, MRR 0.689, mean 2.87 turns to first hit. The provided BM25 baseline scores
0.1067.

It costs **about a third of a cent**. 18,942 tokens of `gpt-4o-mini` across all
200 sessions, because the model is gated to fire only where the deterministic
layers have nothing to say — roughly one call per two sessions. Pull the key out
and it still scores 0.8427.

## How we built it

**Extraction is a cascade, not a parser.** Tier 0 matches the structured phrasing
the simulator uses. Tier 1 is a gazetteer we *mined from the catalog itself* —
107 colours and 107 materials, against the 12 and 10 a hand-written list would
carry — which catches free text no template matches. Tier 2 is **`gpt-4o-mini`**,
which fires only when both deterministic tiers return nothing *and* the message
matched no known template — a structural gate, not a confidence score, so it is
auditable and its cost is predictable.
A separate polarity layer marks what the shopper *rejected*, so "without
underwire" removes a term from the query instead of searching for it — while a
false-friend guard keeps "no show socks" a product type rather than a negation.

**State is an append-only event log.** Every disclosure is an immutable event and
the live constraint set is *replayed* from that log rather than edited in place.
That is what makes intent overrides correct: "actually, ignore my earlier
preference" appends a superseding event, so the stale constraint leaves the
active set but survives at 0.4 weight — because an overridden preference still
describes the same target product.

**Ranking is staged so hard constraints cannot be outvoted.** BM25 over SQLite
FTS5 generates candidates, a prefilter drops determinate violators before the
budget is spent, and reranking runs in four ordered stages: hard-constraint
coverage, lexical relevance, soft preferences, then the user profile as a pure
tie-break. A hard-match floor reserves 2 of the 10 slots for the strongest
lexical matches, because a product BM25 ranks first can otherwise fall out of the
Top-10 on an unremarkable constraint total — that was 16 of our 21 remaining
misses.

## Challenges we ran into

**We caught ourselves overfitting, and it was ugly.** We wrote two more
simulators to attack our own agent: `realistic` (a paraphrasing shopper) and
`esci` (opening turns taken verbatim from real Amazon shopping queries in the
public ESCI dataset). An early version scored **0.87 on the official phrasing and
0.04 on real queries** — worse than the baseline it was beating by 8x.

The cause was a single coupling: the search query was built *only* from extracted
slots, and on real phrasing extraction matched nothing, so the query was
literally empty in 86% of cases. BM25 recall on the raw user text was 0.82; on
what we actually sent it, 0.03. We were throwing the shopper's words away.

**Three of our best ranking ideas made it worse.** Cross-turn rank fusion, a
constraint-coverage bonus, and IDF-weighted evidence all measured neutral to
harmful. Rank fusion was the worst (0.7732 vs 0.8183) — reinforcing what ranked
well on earlier turns just entrenches the mistakes made when the agent knew
least. All three ship disabled with their numbers written next to them.

**A latent bug that only surfaced when we improved something else.** Our new
clarification policy dropped the score from 0.85 to 0.22. The shopper says
*"I don't have an additional preference for brand"*, meaning *brand* is empty;
our code read it as *the conversation is over* and gave up. That bug had been
there the whole time, invisible because the old policy only ever asked one
question — for which the reading happened to be correct.

## Accomplishments that we're proud of

Not the score. The fact that we can **prove** the score isn't an artifact.

| shopper phrasing | sessions | TechnicalScore |
|---|--:|--:|
| official templates | 600 | 0.8427 – 0.8777 |
| paraphrased | 600 | 0.8796 – 0.9135 |
| **real Amazon queries** | 200 | **0.8655** |

1,400 sessions across three independent phrasings. The spread is 0.04. It was
0.83 when we started.

We also got a verification we did not plan. Two bench cells lost network
mid-matrix, fell back to the deterministic cascade, and scored **identically** to
our no-LLM baseline — 0.8869 and 0.8655, to four decimal places. The graceful
degradation is proven under real failure rather than asserted in a README.

We're also proud of what we *didn't* build. We classified every remaining miss as
"target never entered the candidate pool" versus "target was in the pool and
out-ranked" and got **100% reachability, zero recall misses**. Dense retrieval
buys candidates; there were none left to buy. The code is written and tested, and
it ships switched off with that measurement as the reason.

## What we learned

Reading the grader is a legitimate engineering activity, and so is refusing to
exploit what you find there. The measurement that mattered most all week was the
one that made us look worst.

Also: negative results are cheap to record and expensive to rediscover. Every
idea we rejected has its number in a comment next to the flag that disables it.

## What's next

An override-aware retrieval reset (intent_override is our weakest scenario at
HR@10 0.90); a reranker trained on the 234 human-labelled ESCI gold rows rather
than on simulators we wrote ourselves, since fitting to your own generator can
only teach a model to imitate it; and widening the Tier-2 gate to cover
low-confidence gazetteer guesses rather than only empty ones — right now the
model fires on ~1 call per 2 sessions and contributes +0.004, and we have not
explored what a larger role would buy.
```

---

## Built with  *(tags, max 25)*

```
python
sqlite
sqlite-fts5
bm25
information-retrieval
conversational-ai
recommender-systems
nlp
named-entity-recognition
negex
event-sourcing
rank-fusion
numpy
sentence-transformers
openai
gpt-4o-mini
amazon-reviews-2023
esci-dataset
pytest
git
```
`20 / 25`

> `openai` and `gpt-4o-mini` are live in the scored configuration.
> `sentence-transformers` and `numpy` back the dense-retrieval package, which is
> built but deliberately gated off — keep those tags only alongside the
> "deliberately unused" wording.

---

## "Try it out" links

```
https://github.com/fifamobilegems/techjam-conversational-search
```

---

## Required written-description items (Section 4.5)

Paste as a final section of "About the project", or into the Devpost
description field if your track provides a separate one.

```markdown
## Project details

**How this addresses the problem statement.** The task is to find a hidden target
product within 10 conversational turns. We built a stateful retrieval system: a
three-tier extraction cascade turns each message into typed constraints with
polarity, an append-only event log accumulates and correctly retracts them across
turns, and a four-stage reranker orders a BM25 candidate pool so hard constraints
cannot be outvoted by lexical similarity. Verified across all four evaluator
scenarios (Buying, Browsing, Intent Override, Boundary) and three independent
customer phrasings.

**Development tools.** VS Code, Claude Code, Git/GitHub (feature branches with
PR review), Python 3.13.9, unittest.

**APIs used.** **OpenAI Chat Completions — `gpt-4o-mini`**, with a
JSON-schema-constrained response, as Tier 2 of the extraction cascade. It is
additive-only: it may contribute verbatim constraint spans the deterministic
tiers missed, and can never delete a constraint, clear a slot, or change
retrieval timing. Usage for the reported 200-session run: **18,942 tokens
(18,046 prompt / 896 completion), ≈ $0.0032**, ~95 calls. `OPENAI_BASE_URL` makes
any OpenAI-protocol gateway work. Without a key, the agent degrades silently to
the deterministic cascade and scores 0.8427.

**Libraries and frameworks.** `openai` (Tier-2 extractor). Everything else is
**Python standard library** — `sqlite3` (FTS5 full-text index with BM25 ranking),
`re`, `json`, `gzip`, `math`, `dataclasses`. The deterministic path is enforced
stdlib-only in CI by importing the agent with `numpy`, `torch`, `openai` and
`sentence-transformers` blocked at the import hook. Optional and unused in the
reported run: `numpy` and `sentence-transformers` for the gated dense-retrieval
foundation.

**Datasets and assets.** The organiser's frozen 50,000-product catalog and
200-session public set, derived from **Amazon Reviews 2023** (McAuley Lab, UCSD).
We additionally built: an 800-session synthetic set and a 1,000-session set whose
opening turns come from the public **Amazon ESCI / Shopping Queries Dataset**
(query text and E/S/C/I relevance labels only, joined onto the frozen catalog);
and `data/lexicon.json`, a gazetteer of 908 canonical values and 2263 surface
forms mined from the catalog's own metadata. No external data was used to reconstruct unreleased
evaluation labels.

**Reproducing the headline number.** `python3 -m evaluator.local_evaluator`
against the unmodified official evaluator. ~97 s with the Tier-2 model active,
~35 s without. Writes `results.json`. Requires `OPENAI_API_KEY` in `.env` for the
0.8468 figure; without it the run reproduces 0.8427.
```

---

## Pre-submission checklist

- [ ] Demo video uploaded to **YouTube**, visibility set to **Public**
- [ ] Video linked in the Devpost description *and* pasted in **Video demo link**
- [ ] Video shows **at least one complete multi-turn session** (FAQ §7)
- [ ] Video contains no third-party trademarks or copyrighted content
- [ ] GitHub repository is **public**
- [ ] README covers overview, setup, reproduction, limitations, contributions
- [ ] No API keys or secrets anywhere in the repo history (`.env` is gitignored;
      only `.env_example` is tracked)
- [ ] `OPENAI_API_KEY` documented by **name only** in the README (FAQ §2)
- [ ] Model, token usage, cost, and latency disclosed (FAQ §2–3)
- [ ] `evaluator/local_evaluator.py` is byte-identical to the organiser's upstream
- [ ] Record the **submitted commit hash** — it is the frozen solution
- [ ] After the final package drops: run the unmodified evaluator, **keep
      `results.json`**, the commit hash, and environment details (FAQ §1)
- [ ] Do not modify Agent, prompts, indexes, or model config after the deadline
