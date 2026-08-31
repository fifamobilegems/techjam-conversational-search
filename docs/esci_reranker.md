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
have ever treated as a negative was merely unlabelled. (30 of the 62 survive
retrieval into a candidate pool and so are the ones that actually train.)

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
Of the 62, **30 actually reach a candidate pool** — the rest were never
retrieved for their query, so they influence nothing.

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

| epoch | train loss | test loss | train MRR | **held-out MRR** |
|--:|--:|--:|--:|--:|
| 1 | 10.1770 | 9.3227 | 0.2517 | 0.2270 |
| 10 | 8.5834 | 8.2517 | 0.2643 | 0.2347 |
| 20 | 7.4039 | 6.5631 | 0.2657 | 0.2538 |
| **41** | 6.5660 | 5.5293 | 0.2684 | **0.2708** ← peak |
| 50 | 6.3621 | 5.4902 | 0.2693 | 0.2520 |
| 100 | 5.3865 | 5.1464 | 0.2718 | 0.2611 |
| 200 | 5.1014 | 4.9911 | 0.2821 | 0.2432 |
| 300 | 4.9471 | 4.8556 | 0.2800 | 0.2439 |
| 400 | 4.8108 | 4.7354 | 0.2795 | 0.2509 |
| 500 | 4.6949 | 4.6331 | 0.2791 | 0.2422 |

Against the `CALIBRATED_WEIGHTS` starting point of held-out MRR 0.2220 /
hit@10 0.3671, epoch 41 is **0.2708 / 0.4937** — +22% MRR, +35% hit@10.

Read the last two columns together. Training loss falls monotonically for all
500 epochs and train MRR keeps creeping up, while held-out MRR peaks at epoch 41
and never recovers. On 37 parameters and 378 queries, epochs past ~50 buy
memorisation and nothing else. Note also that *test loss* keeps falling
alongside training loss even as held-out MRR decays — the surrogate and the
metric come apart, which is exactly why the checkpoint is selected on held-out
MRR and never on a loss. The full per-epoch curve is in
`docs/reranker_training.json`.

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

| cell | calibrated | `esci` | `esci_soft` | `esci_anchored` | `esci_popularity` |
|---|--:|--:|--:|--:|--:|
| public200 × official | 0.8427 | 0.6938 | 0.6938 | 0.8582 | **0.8883** |
| public200 × realistic | 0.8796 | 0.7732 | 0.7880 | 0.8900 | 0.8980 |
| synth800 × official | 0.8777 | 0.7180 | 0.7180 | 0.8828 | 0.8690 |
| synth800 × realistic | 0.9135 | 0.7675 | 0.7762 | 0.9055 | 0.8861 |
| esci1000 × official | 0.8766 | 0.6767 | 0.6767 | 0.8796 | 0.8753 |
| esci1000 × realistic | 0.8869 | 0.7364 | 0.7572 | 0.8776 | 0.8822 |
| esci1000 × esci | 0.8655 | 0.7654 | 0.7835 | 0.8713 | 0.8598 |
| **mean** | **0.8775** | 0.7330 | 0.7419 | **0.8807** | **0.8798** |
| **Δ mean** | — | **−0.1445** | −0.1356 | +0.0032 | +0.0023 |

The free fit is −0.14. It regresses on `esci1000 × esci` too — the cell whose
queries the weights were fitted on. The last two columns are §5 and §6; read
those before concluding the exercise failed.

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

## 5. The −0.14 is one weight, and it is an artifact

Applying the learned coordinates to `CALIBRATED_WEIGHTS` **one at a time**,
n=100, two cells (baseline 0.8551 / 0.8678):

| weight | from | to | public200 × official | esci1000 × esci |
|---|--:|--:|--:|--:|
| `feature_boost` | 3.60 | **−13.97** | **−0.1411** | −0.0027 |
| `soft_scale` | 1.00 | 0.001 | +0.0000 | −0.0022 |
| `rating_coefficient` | 0.070 | 0.060 | +0.0000 | −0.0020 |
| `budget_unpriced` | −8.00 | 11.20 | +0.0000 | −0.0008 |
| `brand_store` | 30.0 | 26.1 | +0.0000 | −0.0004 |
| `material_boost` | 40.0 | 29.1 | +0.0000 | +0.0000 |
| `size_boost` | 35.0 | 22.8 | +0.0000 | +0.0000 |
| `style_boost` | 70.4 | 58.0 | +0.0000 | +0.0000 |
| `vocabulary_miss` | −12.8 | 5.35 | +0.0000 | +0.0000 |
| `budget_within` | 30.0 | 10.8 | +0.0000 | +0.0000 |
| `department_miss` | −55.0 | −34.5 | +0.0000 | +0.0000 |
| `color_boost` | 18.9 | 1.11 | +0.0000 | +0.0002 |
| `fusion_scale` | 774.4 | 754.0 | +0.0015 | +0.0000 |
| `popularity_scale` | 0.24 | **3.05** | **+0.0349** | **+0.0039** |

The regression is not diffuse. **`feature_boost` alone is −0.1411 of it.**

The fit drove it *negative* — products matching a stated feature get penalised —
and that is an artifact of the training distribution rather than a discovery.
The Tier 2 prompt says "prefer `feature` when unsure", and Tier 0's fallback
sweep lands unclassified spans there too, so `feature` is the extraction
cascade's junk drawer. On a real one-line query it holds noise, and the labels
correctly learn that matching it predicts nothing. On official phrasing, where
templates put genuine catalog metadata in `feature`, the same weight is
catastrophic.

That single coordinate is why the free fit looks like a total failure. It is not
a total failure.

## 6. One coordinate transfers, and it is worth +0.046 on the public set

`popularity_scale` is the opposite case: the only change that is positive on
*both* probe cells on its own. Sweeping it with everything else at
`CALIBRATED_WEIGHTS`, on the **unmodified official evaluator**, full 200-session
public set, LLM off:

| `popularity_scale` | TechnicalScore | HR@10 | MRR | MTTC |
|--:|--:|--:|--:|--:|
| **0.24** (shipped) | 0.8427 | 0.9500 | 0.6840 | 2.875 |
| 1.0 | 0.8624 | 0.9500 | 0.7465 | 2.830 |
| 1.5 | 0.8774 | 0.9550 | 0.7856 | 2.790 |
| 2.0 | 0.8760 | 0.9550 | 0.7790 | 2.760 |
| **3.05** (the ESCI fit) | **0.8883** | 0.9600 | 0.8084 | 2.710 |
| 4.5 | 0.9011 | 0.9750 | 0.8165 | 2.565 |
| 6.0 | 0.9072 | 0.9850 | 0.8127 | 2.455 |

**0.8427 → 0.8883 at the value the human labels chose.** MRR carries it:
0.684 → 0.808. For reference, the shipped LLM-on configuration scores 0.8468, so
one weight fitted on human relevance is worth ten times the entire Tier 2 tier,
offline and for free.

Smooth and monotonic to 6.0, so 3.05 sits on a broad trend rather than a spike —
it was not fitted to noise.

### Why it works, and why `synth800` disagrees

Median `rating_number` of the target product, by dataset:

| | median | p90 | mean |
|---|--:|--:|--:|
| the catalog itself | 12 | 260 | — |
| **public200 targets** | **6,846** | 40,492 | 16,179 |
| synth800 targets | 13 | 244 | 190 |
| esci1000 targets | 24 | 514 | 332 |

`docs/competition_specification.md`: "The hidden target is based on a real
purchase record." Real purchases are overwhelmingly of popular products, so the
public set's targets sit at **570× the catalog's median review count**. A prior
that says "popular products are more likely to be the target" is not a benchmark
exploit — it is the single strongest true fact about how the targets were drawn.

`synth800` sampled its targets near-uniformly from the catalog (median 13, i.e.
the catalog's own median), so it carries no popularity signal at all and pays
for the prior with nothing in return. Its −0.027 is not evidence against
popularity; it is a property of how we built it. The adversarial simulator we
wrote to keep ourselves honest was itself wrong about a real distribution, and
the human labels are what caught it.

That also means the effect should hold on the **hidden 800**, which the
specification says is drawn by the same process as the public 200 — the
strongest transfer argument available without seeing it.

### Which value, and with what provenance

This is the part to be careful about. Three defensible answers with three
different provenances:

| value | chosen by | public200 | 7-cell mean |
|--:|---|--:|--:|
| 0.24 | shipped | 0.8427 | 0.8775 |
| 1.5 | the bench-matrix mean optimum | 0.8774 | **0.8814** |
| **3.05** | **the human ESCI labels, having never seen public200** | **0.8883** | 0.8798 |
| 6.0 | sweeping the scored set itself | 0.9072 | lower |

**`esci_popularity` ships 3.05.** Not because it is the highest number — 6.0 is
— but because it is the only one of the four whose provenance is independent of
the set it is scored on. Picking 6.0 by sweeping public200 is fitting to the
evaluation set, which is the failure mode this whole document exists to avoid.
1.5 is the honest choice if the 7-cell mean is the objective rather than the
official column.

The residual risk is a distributional bet: this pays off if the hidden 800 is
drawn like the public 200, and the specification says it is.

## 7. What ships

**No default changes.** `RERANK_WEIGHTS=calibrated` is still what the Agent
constructs. Every fit is a selectable preset carrying its numbers in the source,
on the same principle as the disabled ranking experiments in `RerankConfig` —
recorded so nobody re-runs them blind:

| preset | what it is | verdict |
|---|---|---|
| `esci` | the free 500-epoch fit | **−0.1445 mean.** Recorded as a negative |
| `esci_soft` | same, `soft_scale` restored | −0.1356. Rules out the obvious suspect |
| `esci_anchored` | trust-region fit, `soft_scale` frozen | +0.0032 mean, +0.0155 public |
| `esci_popularity` | **one coordinate: `popularity_scale` 0.24 → 3.05** | **+0.0456 public, +0.0023 mean** |

### The recommendation

`RERANK_WEIGHTS=esci_popularity` is worth the team's attention before the
deadline. It is a one-line change to a single float, its provenance is human
relevance labels that never saw the public set, the mechanism is understood
(targets are real purchases and real purchases are popular), and the
distributional evidence says it should hold on the hidden 800.

It is still a trade, and the team should take it deliberately rather than have
it taken silently — which is why this branch does not flip the default. It costs
−0.027 on `synth800 × realistic`, and §6 argues that cell is wrong rather than
that the trade is bad, but "our own simulator disagrees" deserves a human
looking at it.

### What is worth keeping regardless

- `data/esci_gold_relevance.jsonl` — 770 human judgments, 3.3× the previous
  ceiling, including the first 62 human-verified negatives this project has had.
  It is a useful held-out evaluation set even where it failed as a training set.
- **The target-popularity distribution table in §6.** `synth800` carries no
  popularity signal because we sampled its targets uniformly. That is a
  correctable flaw in an artifact we rely on to keep ourselves honest, and it is
  the most actionable line in this document.
- The 65.1% single-turn reachability figure, a real gap the session-level 100%
  number conceals.
- The measured answer that a bigger reranker is not the next move.

### The honest next experiment

Not more capacity and not more labels. Two things, in order:

1. **Rebuild `synth800` with popularity-weighted target sampling.** It currently
   disagrees with the official set about the single strongest prior available,
   and every ablation scored against it inherits that.
2. **Supervision whose unit is a session** — human relevance on the target,
   replayed through accumulated multi-turn state, so the training objective and
   the scoring objective are the same shape. `scripts/calibrate_rerank.py`
   already has the tape machinery. That is a different week's work.
