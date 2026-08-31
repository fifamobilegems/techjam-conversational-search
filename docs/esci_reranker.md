# A reranker trained on human labels, and why it did not ship

`README.md` listed this third under "given another week":

> a reranker trained on the 234 human-labelled ESCI gold rows rather than on
> simulators we wrote ourselves, since fitting to your own generator can only
> teach a model to imitate it.

It was built. The premise held — human labels *are* better supervision than our
own generator, by 22% held-out MRR on the task they describe. The conclusion did
not: every fitted variant is a **measured regression on all seven bench cells**,
and the reason is the more useful result.

Reproduce with:

```bash
python3 -m scripts.build_esci_gold          # 770 judgments, needs ~1.2 GB download
python3 -m scripts.build_rerank_features    # ~1 min, needs only the catalog
python3 -m scripts.train_reranker --epochs 500
RERANK_WEIGHTS=esci python3 -m tools.bench --limit 200
```

---

## 1. The data ceiling is the catalog, not the fetch

234 looked like a small sample of something larger. It is not — it is most of
what exists.

The full Shopping Queries release is 2.62M judgments over 1.22M US products.
Joined against the frozen 50,000-product catalog by exact `product_id` →
`parent_asin`, **357 judgments survive**. The catalog is a 50k slice of
`Clothing_Shoes_and_Jewelry`; ESCI spans all of Amazon retail. The intersection
is the whole story and no amount of additional downloading moves it.

One join path does move it. ESCI `product_id` is frequently a *child* ASIN — one
size or colour of a listing whose parent is in the catalog — and a child ASIN
never equals its parent, so the exact join silently drops it. Amazon variants
carry the parent's title verbatim, so a normalized-title join recovers them.
Titles shared by two or more catalog products are excluded outright rather than
resolved: an ambiguous title would attach a human judgment to an arbitrary one
of several products, which is worse than not having the row.

| join path | judgments | catalog products |
|---|--:|--:|
| exact `product_id` = `parent_asin` | 356 | 262 |
| unambiguous normalized title | 414 | 318 |
| **combined, deduplicated** | **770** | **442** |

770 judgments over **748 real queries**, at 560 Exact / 145 Substitute /
62 Irrelevant / 3 Complement. **3.3× the 234**, and the ceiling.

Two things worth noting about the composition. The existing 234 rows carry only
E and S labels — no negatives at all. The wider join brings in **62 Irrelevant
judgments**, which are products a human looked at and rejected *for that query*.
Those are the only human-verified negatives in this project; everything else we
have ever treated as a negative was merely unlabelled.

And only 21 queries have two or more judged catalog products, so there are
almost no within-query human *pairs*. That rules out learning directly from
human preference comparisons and forces the listwise formulation below.

## 2. Features come from the production scorer

`starter/retriever.py` already emits its score as coefficients rather than
numbers — `stage_contributions` returns `(key, weight_name, coefficient)` — which
is what makes fitting a dot product over the real code path instead of a
reimplementation that can drift.

`scripts/build_rerank_features.py` runs each ESCI query through the real
`Agent.respond` (recording retriever) so the plan carries whatever the real
extraction cascade, polarity layer and state manager actually produce from that
phrasing, then compiles the pool into `(candidates × 37)` matrices.

**BM25 reachability on real single-turn queries is 65.1%** — 501 of 770 judged
products enter the candidate pool at all. 457 queries keep at least one judged
positive and are trainable, split 378/79 by ESCI's own `train`/`test` partition
rather than one we invented.

That 65.1% deserves its own note next to `README.md`'s "100% reachability, zero
recall misses". Both are true and they measure different things: 100% is a
*session* number, after ten turns of accumulated constraints; 65.1% is what one
real shopper sentence retrieves before the conversation has happened.

## 3. Training

Listwise softmax cross-entropy (ListNet top-1) with graded gains — Exact 1.0,
Substitute 0.5, Complement and Irrelevant 0:

    L = -Σᵢ (gᵢ / Σⱼ gⱼ) · log softmax(s)ᵢ

Ranking metrics are step functions of the weights with no useful gradient; this
is the standard smooth surrogate and it pushes mass onto the judged product
relative to the pool it actually competes against. The 62 Irrelevant judgments
carry gain 0 but stay in the denominator, doing their work as hard negatives.

The score is *bilinear*, not linear, because `soft_scale` multiplies a stage
instead of entering the dot product. `scripts/calibrate_rerank.py` handles that
by sweeping `soft_scale` in a separate pass; autograd does not need the
workaround, so all 37 coordinates are fitted jointly. That is a genuine
advantage of gradient descent here rather than a reimplementation of the same
search.

`docs/competition_specification.md` puts **full-model training** out of scope and
that line is respected: the trainable object is the same 37-number
`RerankWeights` vector the repo already searches by coordinate descent. Nothing
learns a representation, and nothing here is imported by the agent —
`requirements-training.txt` is offline-only.

### What 500 epochs actually bought

| | held-out MRR | held-out hit@10 |
|---|--:|--:|
| start (`CALIBRATED_WEIGHTS`) | 0.2220 | 0.3671 |
| **best, epoch 41** | **0.2708** | **0.4937** |
| final, epoch 500 | 0.2422 | 0.4810 |

Training loss falls monotonically all the way to epoch 500 (10.18 → 4.69) while
held-out MRR peaks at epoch 41 and then decays. On 37 parameters and 378
queries, epochs past ~50 buy memorisation. The full per-epoch curve is in
`docs/reranker_training.json`; the checkpoint is selected on held-out MRR, never
on final training loss.

### Two negative results that cost nothing to collect

**From random initialisation, the labels are not enough.** Three seeds converge
to held-out MRR 0.1970 / 0.2098 / 0.2064 — all *below* the 0.2220 that
`CALIBRATED_WEIGHTS` already achieves without seeing a single ESCI label. 378
real queries cannot fit 37 weights from scratch. The human labels are useful as
a *refinement* of a hand-designed scorer and useless as a replacement for one.

**The ceiling is the data, not the functional form.** A 9,025-parameter MLP over
byte-identical features (`--arch mlp`) reaches train MRR 0.3624 against the
linear model's 0.2791, and held-out MRR **0.2434** against the linear model's
0.2708. Ten times the training-set fit, worse generalisation, 244× the
parameters. This is the cheapest available answer to "should we just build a
bigger reranker": no.

## 4. The result that matters: it does not transfer

Every fitted variant, benched on the full matrix at n=200 with the LLM tier off,
against the shipped `calibrated` control:

| cell | calibrated | `esci` | `esci_soft` | `esci_anchored` |
|---|--:|--:|--:|--:|
| public200 × official | 0.8427 | 0.6938 | 0.6938 | — |
| public200 × realistic | 0.8796 | 0.7732 | 0.7880 | — |
| synth800 × official | 0.8777 | 0.7180 | 0.7180 | — |
| synth800 × realistic | 0.9135 | 0.7675 | 0.7762 | — |
| esci1000 × official | 0.8766 | 0.6767 | 0.6767 | — |
| esci1000 × realistic | 0.8869 | 0.7364 | 0.7572 | — |
| esci1000 × esci | 0.8655 | 0.7654 | 0.7835 | — |
| **mean** | **0.8775** | 0.7330 | 0.7419 | — |

−0.14 mean. It regresses on `esci1000 × esci` too — the cell whose queries the
weights were fitted on.

### Ruling out the obvious suspect

The free fit collapses `soft_scale` from 1.0 to 0.0009, effectively deleting the
soft stage. Since every training example is a **turn-1** query and `soft_scale`
multiplies exactly the Tier 1 gazetteer constraints that only a multi-turn
session accumulates, that coordinate is the one whose training distribution is
obviously unrepresentative. `esci_soft` is the identical vector with
`soft_scale` held at 1.0.

It recovers **+0.009 of the −0.14**. Not the culprit.

Two cells are byte-identical between `esci` and `esci_soft` — both official
columns, 0.6938 and 0.6767. That is the internal consistency check working: on
template phrasing Tier 0 fires, so there are no soft constraints for
`soft_scale` to scale, and the two vectors are the same vector.

### The actual diagnosis

The supervision unit does not match the evaluation unit.

A human ESCI judgment attaches to **one query and one product**. The bench
measures **one session over up to ten turns**, scoring first-hit-turn and rank
within the top ten. Weights that maximise the first are pushing the target from
rank 400 to rank 40 on a single sentence, which is a job BM25 rank and
popularity do. Weights that maximise the second are separating rank 12 from
rank 8 after four constraints have accumulated, which is the job the
attribute-boost machinery exists for.

The fitted vector says exactly that, read as one sentence: trust BM25 and
popularity more (`popularity_scale` 0.24 → 3.05), stop penalising absent
metadata (`vocabulary_miss` −12.8 → +5.3, `budget_unpriced` −8.0 → +11.2), and
mostly stop scoring attributes (`color_boost` 18.9 → 1.1, `feature_boost`
3.6 → −14.0). Every one of those is defensible for a cold single-turn query and
wrong for turn 6 of a conversation that has disclosed a colour, a material and a
budget.

`README.md`'s premise — that fitting to your own generator can only teach a
model to imitate it — is correct. What this measured is the other half of it:
human labels describing a *different task* teach a model to be good at that
task. The generator was never the only thing that had to match.

## 5. What ships

Nothing, by default. `RERANK_WEIGHTS=calibrated` remains the shipped preset.
`esci`, `esci_soft` and `esci_anchored` are selectable presets carrying their
own numbers in the source, on the same principle as the disabled ranking
experiments in `RerankConfig`: recorded so nobody re-runs them blind.

What is worth keeping from the exercise:

- `data/esci_gold_relevance.jsonl` — 770 human judgments, 3.3× the previous
  ceiling, with 62 real negatives that did not exist before. It is a held-out
  evaluation set even where it failed as a training set.
- The 65.1% single-turn reachability number, which is a real gap that the
  session-level 100% figure conceals.
- The measured answer that a bigger reranker is not the next move.

The honest next experiment is not more capacity or more labels. It is
supervision whose unit is a **session**: human relevance on the target, replayed
through accumulated multi-turn state. `scripts/calibrate_rerank.py` already has
the tape machinery for it. That is a different week's work.
