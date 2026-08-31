"""Fit the reranker weights on human ESCI judgments with PyTorch.

This answers the third item in `README.md`'s "given another week" list: a
reranker trained on human-labelled relevance rather than on simulators we wrote
ourselves.

What is and is not being trained
--------------------------------

`docs/competition_specification.md` puts **full-model training** out of scope,
and that boundary is respected here. The trainable object is the same 37-number
`RerankWeights` vector `scripts/calibrate_rerank.py` already searches by
coordinate descent -- a reweighting of hand-designed features, not a learned
representation. Gradient descent is a better search procedure for it than
coordinate descent, and human labels are a better objective than self-generated
ones. Neither of those changes the hypothesis class.

The score is bilinear rather than linear, because `soft_scale` multiplies a
stage instead of entering the dot product::

    s = dense @ w + w[soft_scale] * (soft @ w)

`calibrate_rerank` handles that by sweeping `soft_scale` in a separate pass.
Autograd does not need the workaround: the product is differentiable, so the
whole vector is fitted jointly, which is a real advantage of doing it this way
rather than merely a different implementation of the same search.

Objective
---------

Listwise softmax cross-entropy (ListNet top-1) over each query's candidate
pool, with graded gains -- Exact 1.0, Substitute 0.5, everything else 0::

    L = -sum_i (g_i / sum_j g_j) * log softmax(s)_i

Ranking metrics are step functions of the weights and have no useful gradient.
This is the standard smooth surrogate, and it optimises the right thing: it
pushes probability mass onto the judged-relevant product relative to *the pool
it actually competes against*, which is what MRR measures. The 62 Irrelevant
judgments carry gain 0 but stay in the denominator, where they act as human-
verified hard negatives rather than presumed ones.

Determinate violations key the production sort ahead of any score, so training
mirrors that with a large fixed penalty rather than letting the model discover
it should have been there.

Honesty about epochs
--------------------

500 epochs on 37 parameters is far past convergence; the curve is reported in
full and the checkpoint is chosen by **held-out MRR**, not by final training
loss. The split is ESCI's own `train`/`test` partition, not one invented here.
`--report` writes every epoch so the overfitting point is visible rather than
asserted.

The `--arch mlp` probe
----------------------

`linear` is the shippable model. `mlp` fits a small two-layer network over the
same per-candidate coefficients and **cannot ship**: it is not expressible as
`RerankWeights`, so `starter/retriever.py` could not evaluate it, and a learned
scoring model is a different thing from a reweighting under
`docs/competition_specification.md`. It is here to answer one question the
linear fit cannot -- whether the ceiling is the *linear form* or the *feature
set*. If a nonlinear model over identical inputs buys nothing, the features are
the limit and a bigger ranker is not the next move.

Usage::

    python3 -m scripts.train_reranker --epochs 500
    python3 -m scripts.train_reranker --epochs 500 --init default --seed 1
    python3 -m scripts.train_reranker --arch mlp     # diagnostic only, cannot ship
"""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import torch

from starter.retriever import CALIBRATED_WEIGHTS, WEIGHT_NAMES, RerankWeights


SOFT_SCALE = WEIGHT_NAMES.index("soft_scale")

# Mirrors the production sort key, where `violations` is the first element and
# no weight can trade against it. Large enough to dominate any score the
# weights can produce, applied as a constant so it contributes no gradient.
VIOLATION_PENALTY = 1e4

# `backfill_scale` never appears in a contribution -- the popularity backfill
# enters through `_generation` -- so it has no gradient and is left fixed.
FROZEN = ("backfill_scale",)


class QueryBatch:
    """One query's compiled pool as tensors."""

    __slots__ = ("dense", "soft", "violations", "target", "split", "query")

    def __init__(self, dense, soft, violations, gain, split: str, query: str) -> None:
        """Hold one query's candidate matrices and its normalized gain vector."""
        self.dense = torch.from_numpy(dense.astype(np.float32))
        self.soft = torch.from_numpy(soft.astype(np.float32))
        self.violations = torch.from_numpy(violations.astype(np.float32))
        total = float(gain.sum())
        self.target = torch.from_numpy((gain / total).astype(np.float32))
        self.split = split
        self.query = query

    def scores(self, weights: torch.Tensor) -> torch.Tensor:
        """Production score for every candidate, including the violation key."""
        raw = self.dense @ weights + weights[SOFT_SCALE] * (self.soft @ weights)
        return raw - VIOLATION_PENALTY * self.violations


def load_features(path: Path) -> list[QueryBatch]:
    """Read the .npz bundle, keeping only queries with a judged positive."""
    data = np.load(path, allow_pickle=False)
    meta = json.loads(bytes(data["__meta__"]).decode("utf-8"))
    names = list(data["__weight_names__"])
    if names != list(WEIGHT_NAMES):
        raise SystemExit(
            "feature file was built against different weight names; rebuild with "
            "python3 -m scripts.build_rerank_features"
        )

    batches = []
    for row in meta:
        if row["positives"] <= 0:
            # No judged-relevant product survived retrieval, so nothing about
            # this query can be fixed by reordering. Keeping it would only
            # teach the model to push probability onto presumed negatives.
            continue
        key = row["key"]
        batches.append(QueryBatch(
            data[f"{key}_dense"],
            data[f"{key}_soft"],
            data[f"{key}_violations"],
            data[f"{key}_gain"],
            row["split"],
            row["query"],
        ))
    return batches


def metrics(batches: list[QueryBatch], weights: torch.Tensor) -> dict:
    """Mean reciprocal rank and hit@10 of the best judged product per query."""
    reciprocal, hits = 0.0, 0
    with torch.no_grad():
        for batch in batches:
            scores = batch.scores(weights)
            relevant = batch.target > 0
            best = scores[relevant].max()
            # Rank of the best judged-relevant product: how many candidates
            # score strictly above it, plus one.
            rank = int((scores > best).sum().item()) + 1
            reciprocal += 1.0 / rank
            hits += int(rank <= 10)
    count = max(1, len(batches))
    return {
        "mrr": reciprocal / count,
        "hit_at_10": hits / count,
        "queries": len(batches),
    }


def listnet_loss(batches: list[QueryBatch], weights: torch.Tensor) -> torch.Tensor:
    """Mean listwise cross-entropy over the given queries."""
    total = torch.zeros((), dtype=torch.float32)
    for batch in batches:
        log_probability = torch.log_softmax(batch.scores(weights), dim=0)
        total = total - (batch.target * log_probability).sum()
    return total / max(1, len(batches))


class MLPScorer(torch.nn.Module):
    """Two-layer scorer over the same per-candidate coefficients.

    Diagnostic only. The input is `[dense | soft]` -- exactly the numbers the
    linear model consumes -- so any gap between the two is attributable to the
    functional form and nothing else.
    """

    def __init__(self, width: int, hidden: int = 64) -> None:
        """Build a `2*width -> hidden -> hidden -> 1` scorer."""
        super().__init__()
        self.net = torch.nn.Sequential(
            torch.nn.Linear(2 * width, hidden),
            torch.nn.ReLU(),
            torch.nn.Linear(hidden, hidden),
            torch.nn.ReLU(),
            torch.nn.Linear(hidden, 1),
        )

    def forward(self, batch: "QueryBatch") -> torch.Tensor:
        """Score every candidate in one query's pool."""
        features = torch.cat([batch.dense, batch.soft], dim=1)
        return self.net(features).squeeze(-1) - VIOLATION_PENALTY * batch.violations


def mlp_metrics(batches: list[QueryBatch], model: MLPScorer) -> dict:
    """Mean reciprocal rank and hit@10 under the MLP scorer."""
    reciprocal, hits = 0.0, 0
    with torch.no_grad():
        for batch in batches:
            scores = model(batch)
            best = scores[batch.target > 0].max()
            rank = int((scores > best).sum().item()) + 1
            reciprocal += 1.0 / rank
            hits += int(rank <= 10)
    count = max(1, len(batches))
    return {"mrr": reciprocal / count, "hit_at_10": hits / count, "queries": len(batches)}


def train_mlp(batches: list[QueryBatch], epochs: int, learning_rate: float) -> dict:
    """Fit the diagnostic MLP and return its best held-out checkpoint."""
    train_set = [batch for batch in batches if batch.split == "train"]
    test_set = [batch for batch in batches if batch.split == "test"]
    model = MLPScorer(len(WEIGHT_NAMES))
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)

    history, best = [], {"held_out_mrr": -1.0}
    for epoch in range(1, epochs + 1):
        optimizer.zero_grad()
        loss = torch.zeros((), dtype=torch.float32)
        for batch in train_set:
            loss = loss - (batch.target * torch.log_softmax(model(batch), dim=0)).sum()
        loss = loss / max(1, len(train_set))
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
        optimizer.step()

        record = {
            "epoch": epoch,
            "train_loss": float(loss.detach()),
            "train_mrr": mlp_metrics(train_set, model)["mrr"],
            **{
                f"held_out_{key}": value
                for key, value in mlp_metrics(test_set, model).items()
                if key != "queries"
            },
        }
        history.append(record)
        if record["held_out_mrr"] > best["held_out_mrr"]:
            best = dict(record)
        if epoch % 25 == 0 or epoch == 1:
            print(
                f"epoch {epoch:>4}  train_loss {record['train_loss']:.4f}  "
                f"train_mrr {record['train_mrr']:.4f}  "
                f"held_out_mrr {record['held_out_mrr']:.4f}",
                flush=True,
            )
    return {"best": best, "history": history, "parameters": sum(
        p.numel() for p in model.parameters()
    )}


def initial_weights(name: str, seed: int) -> torch.Tensor:
    """Starting vector: the shipped weights, the hand-designed ones, or noise."""
    if name == "calibrated":
        values = CALIBRATED_WEIGHTS.as_mapping()
    elif name == "default":
        values = RerankWeights().as_mapping()
    elif name == "random":
        generator = torch.Generator().manual_seed(seed)
        vector = torch.randn(len(WEIGHT_NAMES), generator=generator) * 5.0
        vector[SOFT_SCALE] = 1.0
        return vector
    else:
        raise SystemExit(f"unknown --init {name}")
    return torch.tensor([values[key] for key in WEIGHT_NAMES], dtype=torch.float32)


def train(
    batches: list[QueryBatch],
    epochs: int,
    learning_rate: float,
    l2: float,
    init: str,
    seed: int,
    anchor: float = 0.0,
    freeze: tuple[str, ...] = (),
) -> dict:
    """Fit the weight vector and return the best checkpoint plus the full curve.

    `anchor` turns the fit into a trust region around the starting vector. Free
    fitting answers "what do the human labels say?"; anchored fitting answers
    the different and more useful question "what do they say that is worth
    overruling three prior rounds of session-level measurement for?" -- because
    the shipped weights were not guessed, they were fitted against the metric
    the agent is actually scored on.
    """
    train_set = [batch for batch in batches if batch.split == "train"]
    test_set = [batch for batch in batches if batch.split == "test"]
    if not train_set or not test_set:
        raise SystemExit("need both ESCI train and test queries")

    start = initial_weights(init, seed)
    weights = start.clone().requires_grad_(True)
    held = set(FROZEN) | set(freeze)
    frozen = torch.tensor(
        [1.0 if name in held else 0.0 for name in WEIGHT_NAMES], dtype=torch.float32
    )
    optimizer = torch.optim.Adam([weights], lr=learning_rate)

    history: list[dict] = []
    best = {"held_out_mrr": -1.0}
    for epoch in range(1, epochs + 1):
        optimizer.zero_grad()
        loss = listnet_loss(train_set, weights)
        if l2:
            loss = loss + l2 * (weights ** 2).sum()
        if anchor:
            # Scale-free: each coordinate is penalised relative to its own
            # magnitude, so `fusion_scale` at 774 and `department_match` at 0.1
            # are held equally tightly instead of the large ones absorbing the
            # entire budget.
            drift = (weights - start) / (start.abs() + 1.0)
            loss = loss + anchor * (drift ** 2).sum()
        loss.backward()
        # Frozen coordinates have no gradient path anyway; zeroing is explicit
        # so a future feature that does touch them cannot drift silently.
        weights.grad.mul_(1.0 - frozen)
        torch.nn.utils.clip_grad_norm_([weights], 5.0)
        optimizer.step()

        with torch.no_grad():
            train_metrics = metrics(train_set, weights)
            test_metrics = metrics(test_set, weights)
            test_loss = float(listnet_loss(test_set, weights))
        record = {
            "epoch": epoch,
            "train_loss": float(loss.detach()),
            "test_loss": test_loss,
            "train_mrr": train_metrics["mrr"],
            "held_out_mrr": test_metrics["mrr"],
            "held_out_hit_at_10": test_metrics["hit_at_10"],
        }
        history.append(record)
        if record["held_out_mrr"] > best["held_out_mrr"]:
            best = {**record, "weights": weights.detach().clone()}
        if epoch % 25 == 0 or epoch == 1:
            print(
                f"epoch {epoch:>4}  train_loss {record['train_loss']:.4f}  "
                f"test_loss {test_loss:.4f}  train_mrr {record['train_mrr']:.4f}  "
                f"held_out_mrr {record['held_out_mrr']:.4f}",
                flush=True,
            )

    return {
        "best": best,
        "final": weights.detach().clone(),
        "history": history,
        "start": start,
        "train_queries": len(train_set),
        "test_queries": len(test_set),
    }


def as_weights(vector: torch.Tensor) -> RerankWeights:
    """Turn a fitted vector back into a `RerankWeights` instance."""
    return replace(
        RerankWeights(),
        **{name: round(float(value), 4) for name, value in zip(WEIGHT_NAMES, vector)},
    )


def emit_source(weights: RerankWeights, name: str) -> str:
    """Render a paste-ready `RerankWeights(...)` literal for the retriever."""
    mapping = weights.as_mapping()
    lines = [f"{name} = RerankWeights("]
    lines += [f"    {key}={value}," for key, value in mapping.items()]
    lines.append(")")
    return "\n".join(lines)


def main() -> None:
    """Train, report the curve, and write the fitted weights."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", default="data/rerank_features.npz")
    parser.add_argument("--epochs", type=int, default=500)
    parser.add_argument("--lr", type=float, default=0.5)
    parser.add_argument(
        "--l2", type=float, default=0.0,
        help=(
            "plain L2 on the raw weights. Note the scale: `fusion_scale` sits "
            "near 774, so its squared term is ~6e5 and even a small "
            "coefficient dominates the ranking loss. Prefer --anchor, which "
            "normalises per coordinate."
        ),
    )
    parser.add_argument(
        "--anchor", type=float, default=0.0,
        help="trust-region strength toward --init; 0 fits freely",
    )
    parser.add_argument(
        "--freeze", nargs="*", default=[],
        help=(
            "weight names to hold at their --init value. Use for coordinates "
            "whose training distribution is known to be unrepresentative -- "
            "`soft_scale` multiplies the Tier 1 constraints that only a "
            "multi-turn session accumulates, and every training example here "
            "is a turn-1 query."
        ),
    )
    parser.add_argument("--init", default="calibrated",
                        choices=("calibrated", "default", "random"))
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--arch", default="linear", choices=("linear", "mlp"))
    parser.add_argument("--report", default="docs/reranker_training.json")
    args = parser.parse_args()

    torch.manual_seed(args.seed)
    batches = load_features(Path(args.features))
    print(
        f"{len(batches)} trainable queries "
        f"({sum(1 for b in batches if b.split == 'train')} train / "
        f"{sum(1 for b in batches if b.split == 'test')} test), "
        f"{sum(b.dense.shape[0] for b in batches)} candidate rows, "
        f"{len(WEIGHT_NAMES)} parameters"
    )

    if args.arch == "mlp":
        probe = train_mlp(batches, args.epochs, args.lr / 100.0)
        print(
            f"\nMLP probe ({probe['parameters']} parameters, DIAGNOSTIC ONLY -- "
            f"not expressible as RerankWeights, cannot ship)\n"
            f"best (epoch {probe['best']['epoch']})  "
            f"held-out MRR {probe['best']['held_out_mrr']:.4f}  "
            f"hit@10 {probe['best']['held_out_hit_at_10']:.4f}"
        )
        Path(args.report).write_text(json.dumps({
            "config": vars(args), "arch": "mlp", **probe
        }, indent=2) + "\n")
        print(f"\nwrote {args.report}")
        return

    result = train(
        batches, args.epochs, args.lr, args.l2, args.init, args.seed, args.anchor,
        tuple(args.freeze),
    )

    start_metrics = {
        "train": metrics([b for b in batches if b.split == "train"], result["start"]),
        "test": metrics([b for b in batches if b.split == "test"], result["start"]),
    }
    best_epoch = result["best"]["epoch"]
    final_metrics = metrics(
        [b for b in batches if b.split == "test"], result["final"]
    )
    print(
        f"\nstart ({args.init})   held-out MRR {start_metrics['test']['mrr']:.4f}  "
        f"hit@10 {start_metrics['test']['hit_at_10']:.4f}\n"
        f"best  (epoch {best_epoch})  held-out MRR {result['best']['held_out_mrr']:.4f}  "
        f"hit@10 {result['best']['held_out_hit_at_10']:.4f}\n"
        f"final (epoch {args.epochs})  held-out MRR {final_metrics['mrr']:.4f}  "
        f"hit@10 {final_metrics['hit_at_10']:.4f}"
    )

    fitted = as_weights(result["best"]["weights"])
    report = {
        "config": vars(args),
        "queries": {
            "train": result["train_queries"], "test": result["test_queries"]
        },
        "start_metrics": start_metrics,
        "best_epoch": best_epoch,
        "best_metrics": {
            key: value for key, value in result["best"].items() if key != "weights"
        },
        "final_metrics": final_metrics,
        "weights": fitted.as_mapping(),
        "history": result["history"],
    }
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nwrote {args.report}\n")
    print(emit_source(fitted, "ESCI_TRAINED_WEIGHTS"))


if __name__ == "__main__":
    main()
