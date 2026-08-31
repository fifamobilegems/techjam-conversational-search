"""Coordinate search over the reranker weights — calibration, not training.

`docs/competition_specification.md:13` rules out full-model training, and for
self-generated queries a learned reranker would be circular anyway: the
simulators build their queries from the target product's own metadata, so the
training pairs are *(query derived from P, product P)* and a model can only
learn "products whose metadata overlaps the query text are relevant" — which is
what BM25 already computes. The generator's function is the ceiling. Fitting a
few dozen hand-designed magnitudes is a different proposition: the hypothesis
class is small enough that it cannot memorise a generator.

**The design that makes the result mean something is holding out by generator,
not by sample.** Fitting on synth-realistic and scoring on ESCI-gold — the 234
rows carrying real human E/S/C/I relevance judgments rather than anything
produced here — asks whether the calibrated weights carry information about
shopper intent or only about the generator that wrote the queries. Transfer is
evidence of real signal. No transfer confirms circularity, which is itself a
useful and much cheaper finding than a training pipeline would have been.

How it works
------------

1.  **Tape capture.** Each session is replayed for the full turn budget with
    retrieval stubbed out, recording the exact ``retrieve_and_rerank`` arguments
    per turn. The customer simulators branch only on ``ask_attribute``, which
    comes from state and not from ranking, so the recorded turn sequence is
    identical to the one a scoring run would produce — a live run merely stops
    early on a hit, and stopping early only truncates the tape.

2.  **Feature compilation.** Every turn's candidate pool is scored once into
    ``(candidates x weights)`` coefficient matrices via the *production*
    ``stage_contributions``. There is no second scorer to drift.

3.  **Replay.** A weight vector becomes two matrix products, from which the
    target's rank, the first hit turn, and the official technical score follow
    exactly as ``evaluator.local_evaluator`` computes them.

Candidate pools are pruned to the top ``--prune`` by baseline score (plus the
target). Anything the search finds is re-measured with the real bench before it
is believed; this harness is for search speed, not for the final number.

Usage::

    python3 -m scripts.calibrate_rerank --limit 300
    python3 -m scripts.calibrate_rerank --fit synth800/realistic --eval esci1000/esci
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from evaluator.local_evaluator import MAX_TURNS, TOP_K, catalog_index, load_jsonl
from starter.agent import Agent
from starter.retriever import WEIGHT_NAMES, CatalogRetriever, RerankConfig, RerankWeights
from tools.trace_runner import run_session, select_samples


DATASETS: dict[str, str] = {
    "public200": "data/public_set.jsonl",
    "synth800": "data/synth_set_800.jsonl",
    "esci1000": "data/esci_set_1000.jsonl",
}

WEIGHT_INDEX = {name: index for index, name in enumerate(WEIGHT_NAMES)}
SOFT_SCALE = WEIGHT_INDEX["soft_scale"]

# Weights the search may move. `soft_scale` multiplies a stage rather than
# entering the dot product, and is swept separately in its own pass.
SEARCHABLE: tuple[str, ...] = tuple(
    name for name in WEIGHT_NAMES if name not in {"backfill_scale"}
)


# =============================================================================
# TAPE CAPTURE
# =============================================================================


class _RecordingRetriever:
    """Stands in for the retriever so a session runs its full turn budget.

    Returning no recommendations is what keeps the tape complete: the session
    loop breaks on a hit, and a tape truncated at the hit could not be replayed
    under weights that would have hit later.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.last_diagnostics: dict = {}

    def retrieve_and_rerank(
        self,
        search_query: str,
        constraints: dict,
        no_preference=(),
        top_k: int = 10,
        raw_constraints=(),
        user_profile: dict | None = None,
    ) -> list[str]:
        self.calls.append({
            "search_query": search_query,
            "constraints": dict(constraints),
            "no_preference": [str(item) for item in no_preference],
            "raw_constraints": [dict(span) for span in raw_constraints],
            "user_profile": user_profile,
        })
        return []


def capture_tapes(agent: Agent, samples, catalog_ids, categories, products, simulator: str):
    """Replay every session with retrieval stubbed, returning per-turn requests."""
    real = agent.retriever
    recorder = _RecordingRetriever()
    agent.retriever = recorder  # type: ignore[assignment]
    tapes = []
    try:
        for sample in samples:
            recorder.calls = []
            trace = run_session(agent, sample, catalog_ids, categories, products, simulator)
            turns = []
            call_index = 0
            for record in trace["turns"]:
                emitted = record["recommendations"] is not None and call_index < len(recorder.calls)
                # The agent only calls the retriever on turns it means to emit.
                request = None
                if emitted and call_index < len(recorder.calls):
                    request = recorder.calls[call_index]
                    call_index += 1
                turns.append({"scored": bool(record["scored"]), "request": request})
            tapes.append({
                "sample_id": trace["sample_id"],
                "provenance": sample.get("provenance"),
                "scenario_type": trace["scenario_type"],
                "target": trace["target"]["parent_asin"],
                "turns": turns,
            })
    finally:
        agent.retriever = real
    return tapes


# =============================================================================
# FEATURE COMPILATION
# =============================================================================


class CompiledTurn:
    __slots__ = ("scored", "dense", "soft", "violations", "target_index")

    def __init__(self, scored: bool, dense, soft, violations, target_index: int) -> None:
        self.scored = scored
        self.dense = dense
        self.soft = soft
        self.violations = violations
        self.target_index = target_index


def compile_tapes(
    retriever: CatalogRetriever,
    tapes: list[dict],
    config: RerankConfig,
    prune: int,
) -> list[list[CompiledTurn]]:
    """Turn recorded requests into per-turn coefficient matrices."""
    previous = retriever.config
    retriever.config = config
    baseline = np.array(
        [RerankWeights().as_mapping()[name] for name in WEIGHT_NAMES], dtype=np.float64
    )
    compiled: list[list[CompiledTurn]] = []
    try:
        for tape in tapes:
            target = tape["target"]
            turns: list[CompiledTurn] = []
            for turn in tape["turns"]:
                request = turn["request"]
                if request is None:
                    turns.append(CompiledTurn(turn["scored"], None, None, None, -1))
                    continue
                plan = retriever.build_plan(
                    request["search_query"],
                    request["constraints"],
                    request["no_preference"],
                    request["raw_constraints"],
                    request["user_profile"],
                )
                pool = retriever._candidate_pool(plan)
                ids = list(pool)
                if not ids:
                    turns.append(CompiledTurn(turn["scored"], None, None, None, -1))
                    continue

                width = len(WEIGHT_NAMES)
                dense = np.zeros((len(ids), width), dtype=np.float32)
                soft = np.zeros((len(ids), width), dtype=np.float32)
                violations = np.zeros(len(ids), dtype=np.int16)
                for row, parent_asin in enumerate(ids):
                    record = retriever.products[parent_asin]
                    stages = retriever.stage_contributions(record, plan)
                    generation = pool[parent_asin]
                    dense[row, WEIGHT_INDEX[generation.weight_name]] += generation.coefficient
                    for stage in ("relevance", "demoted", "profile"):
                        for _, name, coefficient in stages[stage]:
                            dense[row, WEIGHT_INDEX[name]] += coefficient
                    for _, name, coefficient in stages["soft"]:
                        soft[row, WEIGHT_INDEX[name]] += coefficient
                    if config.staged:
                        violations[row] = retriever.violations(record, plan)

                keep = _prune(ids, dense, soft, violations, baseline, target, prune)
                target_index = ids.index(target) if target in ids else -1
                if target_index >= 0:
                    target_index = int(np.where(keep == target_index)[0][0])
                turns.append(CompiledTurn(
                    turn["scored"], dense[keep], soft[keep], violations[keep], target_index
                ))
            compiled.append(turns)
    finally:
        retriever.config = previous
    return compiled


def _prune(ids, dense, soft, violations, baseline, target, prune: int):
    """Keep the top ``prune`` by baseline rank, plus the target if present."""
    if prune <= 0 or len(ids) <= prune:
        return np.arange(len(ids))
    scores = dense @ baseline + baseline[SOFT_SCALE] * (soft @ baseline)
    order = np.lexsort((-scores, violations))[:prune]
    if target in ids:
        target_index = ids.index(target)
        if target_index not in order:
            order = np.concatenate([order, [target_index]])
    return np.sort(order)


# =============================================================================
# REPLAY
# =============================================================================


def evaluate(compiled: list[list[CompiledTurn]], weights: np.ndarray) -> dict:
    """Reproduce the official metrics for one weight vector."""
    soft_scale = weights[SOFT_SCALE]
    hits = 0
    reciprocal = 0.0
    turn_total = 0.0
    for session in compiled:
        first_hit: int | None = None
        rank_at_hit = 0
        for index, turn in enumerate(session, start=1):
            if turn.dense is None or turn.target_index < 0 or not turn.scored:
                continue
            scores = turn.dense @ weights + soft_scale * (turn.soft @ weights)
            target_violations = turn.violations[turn.target_index]
            target_score = scores[turn.target_index]
            better = int(np.count_nonzero(
                (turn.violations < target_violations)
                | ((turn.violations == target_violations) & (scores > target_score))
            ))
            rank = better + 1
            if rank <= TOP_K:
                first_hit = index
                rank_at_hit = rank
                break
        if first_hit is None:
            turn_total += MAX_TURNS + 1
        else:
            hits += 1
            reciprocal += 1.0 / rank_at_hit
            turn_total += first_hit
    count = max(1, len(compiled))
    hit_rate = hits / count
    mrr = reciprocal / count
    mttc = turn_total / count
    efficiency = max(0.0, min(1.0, (11.0 - mttc) / 10.0))
    return {
        "hit_rate_at_10": hit_rate,
        "mrr": mrr,
        "mttc": mttc,
        "efficiency": efficiency,
        "technical": 0.50 * hit_rate + 0.30 * mrr + 0.20 * efficiency,
    }


def coverage_report(compiled: list[list[CompiledTurn]], weights: np.ndarray) -> dict:
    """Split the misses into "never retrieved" and "retrieved but out-ranked".

    This is the Phase 6.2 gate. Dense retrieval buys candidates; it cannot help
    a session whose target was already in the pool and lost on score. If almost
    every miss is a ranking miss, a dense channel is answering a question
    nobody asked.
    """
    metrics = evaluate(compiled, weights)
    reachable = 0
    misses = 0
    reachable_misses = 0
    for session in compiled:
        in_pool = any(
            turn.dense is not None and turn.target_index >= 0 and turn.scored
            for turn in session
        )
        reachable += int(in_pool)
        hit = False
        soft_scale = weights[SOFT_SCALE]
        for turn in session:
            if turn.dense is None or turn.target_index < 0 or not turn.scored:
                continue
            scores = turn.dense @ weights + soft_scale * (turn.soft @ weights)
            better = int(np.count_nonzero(
                (turn.violations < turn.violations[turn.target_index])
                | ((turn.violations == turn.violations[turn.target_index])
                   & (scores > scores[turn.target_index]))
            ))
            if better + 1 <= TOP_K:
                hit = True
                break
        if not hit:
            misses += 1
            reachable_misses += int(in_pool)
    total = max(1, len(compiled))
    return {
        **metrics,
        "sessions": len(compiled),
        "target_reachable": reachable / total,
        "misses": misses,
        "ranking_misses": reachable_misses,
        "recall_misses": misses - reachable_misses,
    }


# =============================================================================
# COORDINATE SEARCH
# =============================================================================


def _candidates(name: str, value: float) -> list[float]:
    """Multiplicative steps, plus a sign-preserving fallback for zeros."""
    if value == 0.0:
        return [0.0, 0.25, 0.5, 1.0]
    steps = (0.4, 0.6, 0.8, 0.9, 1.1, 1.25, 1.6, 2.2)
    out = [value * step for step in steps]
    if name.endswith("_miss") or name.endswith("_over") or value < 0:
        out.append(0.0)  # abstaining is a live hypothesis for every penalty
    return out


def coordinate_search(
    compiled: list[list[CompiledTurn]],
    start: np.ndarray,
    passes: int,
    names: tuple[str, ...],
    log,
) -> tuple[np.ndarray, float]:
    current = start.copy()
    best = evaluate(compiled, current)["technical"]
    log(f"  start technical={best:.4f}")
    for pass_index in range(1, passes + 1):
        improved = False
        for name in names:
            index = WEIGHT_INDEX[name]
            base_value = current[index]
            best_value = base_value
            for candidate in _candidates(name, base_value):
                current[index] = candidate
                score = evaluate(compiled, current)["technical"]
                if score > best + 1e-9:
                    best = score
                    best_value = candidate
                    improved = True
            current[index] = best_value
            if best_value != base_value:
                log(f"    {name}: {base_value:.4g} -> {best_value:.4g}  technical={best:.4f}")
        log(f"  pass {pass_index}: technical={best:.4f}")
        if not improved:
            break
    return current, best


# =============================================================================
# ENTRY POINT
# =============================================================================


def _load(spec: str, limit: int | None, select: str, gold_only: bool):
    dataset, simulator = spec.split("/")
    samples = load_jsonl(DATASETS[dataset])
    if gold_only:
        samples = [row for row in samples if row.get("provenance") == "gold"]
    if limit is not None:
        samples = select_samples(samples, limit, select)
    return dataset, simulator, samples


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reranker weight calibration")
    parser.add_argument("--catalog", default="data/catalog.jsonl")
    parser.add_argument("--fit", default="synth800/realistic", help="dataset/simulator to fit on")
    parser.add_argument(
        "--eval",
        default="esci1000/esci",
        help="held-out dataset/simulator; a different generator is the whole point",
    )
    parser.add_argument(
        "--eval-gold-only",
        action="store_true",
        default=True,
        help="restrict the held-out set to provenance==gold (real human labels)",
    )
    parser.add_argument("--no-eval-gold-only", dest="eval_gold_only", action="store_false")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--eval-limit", type=int, default=None)
    parser.add_argument("--select", choices=("stratified", "head"), default="stratified")
    parser.add_argument("--passes", type=int, default=3)
    parser.add_argument("--prune", type=int, default=250)
    parser.add_argument("--out", default="logs/calibration")
    args = parser.parse_args(argv)

    started = time.perf_counter()
    catalog_ids, categories, products = catalog_index(args.catalog)
    agent = Agent(args.catalog)
    retriever: CatalogRetriever = agent.retriever
    config = retriever.config
    print(f"catalog indexed: {len(catalog_ids)} products ({time.perf_counter()-started:.0f}s)\n")

    sets = {}
    for label, spec, limit, gold in (
        ("fit", args.fit, args.limit, False),
        ("eval", args.eval, args.eval_limit, args.eval_gold_only),
    ):
        dataset, simulator, samples = _load(spec, limit, args.select, gold)
        mark = time.perf_counter()
        tapes = capture_tapes(agent, samples, catalog_ids, categories, products, simulator)
        compiled = compile_tapes(retriever, tapes, config, args.prune)
        sets[label] = {"spec": spec, "n": len(samples), "compiled": compiled}
        print(
            f"{label:<5} {spec:<22} n={len(samples):<5} "
            f"gold_only={gold}  compiled in {time.perf_counter()-mark:.0f}s"
        )

    start = np.array(
        [RerankWeights().as_mapping()[name] for name in WEIGHT_NAMES], dtype=np.float64
    )
    before = {label: evaluate(data["compiled"], start) for label, data in sets.items()}

    print("\n## Where the misses come from (baseline weights) — the Phase 6.2 gate")
    print("\n| set | sessions | target reachable | misses | ranking misses | recall misses |")
    print("|---|--:|--:|--:|--:|--:|")
    coverage = {}
    for label, data in sets.items():
        report = coverage_report(data["compiled"], start)
        coverage[label] = report
        print(
            f"| {label} | {report['sessions']} | {report['target_reachable']:.3f} | "
            f"{report['misses']} | {report['ranking_misses']} | {report['recall_misses']} |"
        )

    print("\ncoordinate search (fit set only)")
    tuned, _ = coordinate_search(
        sets["fit"]["compiled"], start, args.passes, SEARCHABLE, print
    )
    after = {label: evaluate(data["compiled"], tuned) for label, data in sets.items()}

    changed = {
        name: (float(start[index]), float(tuned[index]))
        for name, index in WEIGHT_INDEX.items()
        if abs(start[index] - tuned[index]) > 1e-9
    }

    print("\n## Transfer — the decisive column is `eval`")
    print("\n| set | spec | n | technical before | technical after | delta |")
    print("|---|---|--:|--:|--:|--:|")
    for label, data in sets.items():
        b = before[label]["technical"]
        a = after[label]["technical"]
        print(f"| {label} | {data['spec']} | {data['n']} | {b:.4f} | {a:.4f} | {a-b:+.4f} |")

    print("\n| weight | before | after |")
    print("|---|--:|--:|")
    for name, (old, new) in sorted(changed.items()):
        print(f"| {name} | {old:.4g} | {new:.4g} |")

    verdict = (
        "transfers — the calibration carries signal beyond its own generator"
        if after["eval"]["technical"] > before["eval"]["technical"] + 1e-4
        else "does NOT transfer — the gain is generator-specific (circularity confirmed)"
    )
    print(f"\nVerdict: {verdict}")

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "fit": sets["fit"]["spec"],
        "eval": sets["eval"]["spec"],
        "eval_gold_only": args.eval_gold_only,
        "sample_counts": {label: data["n"] for label, data in sets.items()},
        "prune": args.prune,
        "before": before,
        "coverage": coverage,
        "after": after,
        "changed_weights": changed,
        "weights": {name: float(tuned[index]) for name, index in WEIGHT_INDEX.items()},
        "verdict": verdict,
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{stamp}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
