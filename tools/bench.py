"""Bench matrix: every dataset crossed with every customer simulator.

This is the accept/reject gate for every later phase. A change that lifts
``official`` while dropping ``realistic`` or ``esci`` is overfitting, and the
only way to see that is to score all three phrasings side by side, every time.

It reuses the exact session loop (:func:`tools.trace_runner.run_session`) and
the official scorer (:func:`evaluator.local_evaluator.metric_summary`, via
:func:`tools.trace_runner.summarize`) so the numbers match what the evaluator
would report — this is a *reporting* wrapper, not a second scorer.

Matrix::

    datasets    public200  synth800  esci1000
    simulators  official   realistic esci

The ``esci`` simulator needs an ``esci_query`` field; datasets without one
(public200, synth800) are skipped for that column and marked ``—`` rather than
silently run as a duplicate ``realistic`` cell. Pass ``--full-matrix`` to force
every cell anyway (the incompatible ones fall back to a realistic opening).

Outputs (default ``logs/bench/``)::

    <timestamp>.json   structured results for every cell
    <timestamp>.md     comparison table + per-scenario breakdown

Usage::

    python3 -m tools.bench                       # full matrix, all samples
    python3 -m tools.bench --limit 100           # 100 per cell, fast iteration
    python3 -m tools.bench --datasets esci1000 --simulators official esci
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evaluator.local_evaluator import catalog_index, load_jsonl
from starter.agent import Agent
from tools.trace_runner import run_session, select_samples, summarize


# name -> dataset path
DATASETS: dict[str, str] = {
    "public200": "data/public_set.jsonl",
    "synth800": "data/synth_set_800.jsonl",
    "esci1000": "data/esci_set_1000.jsonl",
}

SIMULATORS: tuple[str, ...] = ("official", "realistic", "esci")

# Simulators that require a field to be present on every sample.
SIMULATOR_REQUIRES: dict[str, str] = {"esci": "esci_query"}

METRIC_COLUMNS = ("hit_rate_at_10", "mrr", "mttc", "efficiency", "recommended_technical_score")
SCENARIO_ORDER = ("buying", "browsing", "intent_override", "boundary")


def _dataset_supports(simulator: str, samples: list[dict]) -> bool:
    field = SIMULATOR_REQUIRES.get(simulator)
    if field is None:
        return True
    return any(field in sample and str(sample.get(field) or "").strip() for sample in samples)


def run_cell(
    agent: Agent,
    samples: list[dict],
    catalog_ids: set[str],
    categories: dict[str, list[str]],
    products: dict[str, dict],
    simulator: str,
) -> dict:
    """Score one (dataset, simulator) cell and return its summary."""
    traces = [
        run_session(agent, sample, catalog_ids, categories, products, simulator)
        for sample in samples
    ]
    summary = summarize(traces)
    summary.pop("sessions", None)  # keep the matrix file compact
    return summary


def run_matrix(
    catalog_path: str | Path,
    datasets: list[str],
    simulators: list[str],
    limit: int | None,
    select: str,
    full_matrix: bool,
    hold_until_turn: int | None = None,
) -> dict:
    catalog_ids, categories, products = catalog_index(catalog_path)
    build_started = time.perf_counter()
    # hold_until_turn is the emit policy's only live lever (the credibility test
    # is inert until retrieval diagnostics are published in-turn), so it is worth
    # sweeping. None keeps the Agent's own default.
    agent = (
        Agent(catalog_path)
        if hold_until_turn is None
        else Agent(catalog_path, hold_until_turn=hold_until_turn)
    )  # built once, reused across every cell
    build_ms = round((time.perf_counter() - build_started) * 1000, 1)
    print(f"catalog indexed: {len(catalog_ids)} products | agent built in {build_ms:.0f}ms\n")

    cells: list[dict] = []
    for dataset in datasets:
        all_samples = load_jsonl(DATASETS[dataset])
        chosen = all_samples if limit is None else select_samples(all_samples, limit, select)
        for simulator in simulators:
            supported = _dataset_supports(simulator, chosen)
            if not supported and not full_matrix:
                print(f"  {dataset:<10} x {simulator:<10}  — skipped (no {SIMULATOR_REQUIRES[simulator]})")
                cells.append({
                    "dataset": dataset,
                    "simulator": simulator,
                    "skipped": True,
                    "reason": f"dataset has no {SIMULATOR_REQUIRES[simulator]}",
                })
                continue
            started = time.perf_counter()
            summary = run_cell(agent, chosen, catalog_ids, categories, products, simulator)
            elapsed = time.perf_counter() - started
            cells.append({
                "dataset": dataset,
                "simulator": simulator,
                "skipped": False,
                "degenerate": not supported,  # forced via --full-matrix
                "duration_s": round(elapsed, 2),
                **summary,
            })
            tech = summary.get("recommended_technical_score")
            hr = summary.get("hit_rate_at_10")
            print(
                f"  {dataset:<10} x {simulator:<10}  n={summary.get('sample_count'):<5} "
                f"HR@10={hr:.4f} tech={tech:.4f}  ({elapsed:.1f}s)"
            )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "catalog": str(catalog_path),
        "limit": limit,
        "select": select,
        "hold_until_turn": hold_until_turn,
        "datasets": datasets,
        "simulators": simulators,
        "cells": cells,
    }


# =============================================================================
# RENDERING
# =============================================================================


def _fmt(value: Any, spec: str = ".4f") -> str:
    if value is None:
        return "—"
    try:
        return format(float(value), spec)
    except (TypeError, ValueError):
        return str(value)


def render_markdown(result: dict) -> str:
    lines: list[str] = []
    lines.append(f"# Bench matrix — {result['generated_at']}")
    lines.append("")
    limit = result["limit"]
    lines.append(
        f"`limit={limit if limit is not None else 'all'}` · `select={result['select']}` · "
        f"catalog `{result['catalog']}`"
    )
    lines.append("")
    lines.append("## Overall")
    lines.append("")
    lines.append("| dataset | simulator | n | HR@10 | MRR | MTTC | Efficiency | Technical | tokens |")
    lines.append("|---|---|--:|--:|--:|--:|--:|--:|--:|")
    for cell in result["cells"]:
        label = f"{cell['dataset']}"
        sim = cell["simulator"]
        if cell.get("skipped"):
            lines.append(f"| {label} | {sim} | — | — | — | — | — | — | _{cell['reason']}_ |")
            continue
        sim_label = sim + (" *(degenerate)*" if cell.get("degenerate") else "")
        tokens = cell.get("reported_token_usage", {}).get("total_tokens", 0)
        lines.append(
            f"| {label} | {sim_label} | {cell.get('sample_count')} | "
            f"{_fmt(cell.get('hit_rate_at_10'))} | {_fmt(cell.get('mrr'))} | "
            f"{_fmt(cell.get('mttc'), '.3f')} | {_fmt(cell.get('efficiency'))} | "
            f"**{_fmt(cell.get('recommended_technical_score'))}** | {tokens} |"
        )
    lines.append("")

    # Per-scenario HR@10 breakdown.
    lines.append("## HR@10 by scenario")
    lines.append("")
    lines.append("| dataset | simulator | " + " | ".join(SCENARIO_ORDER) + " |")
    lines.append("|---|---|" + "|".join(["--:"] * len(SCENARIO_ORDER)) + "|")
    for cell in result["cells"]:
        if cell.get("skipped"):
            continue
        scen = cell.get("scenario_metrics", {})
        row = [
            _fmt(scen.get(name, {}).get("hit_rate_at_10")) if name in scen else "—"
            for name in SCENARIO_ORDER
        ]
        lines.append(f"| {cell['dataset']} | {cell['simulator']} | " + " | ".join(row) + " |")
    lines.append("")
    return "\n".join(lines)


# =============================================================================
# ENTRY POINT
# =============================================================================


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dataset x simulator bench matrix")
    parser.add_argument("--catalog", default="data/catalog.jsonl")
    parser.add_argument(
        "--datasets",
        nargs="+",
        choices=tuple(DATASETS),
        default=list(DATASETS),
        help="which datasets to include (default: all)",
    )
    parser.add_argument(
        "--simulators",
        nargs="+",
        choices=SIMULATORS,
        default=list(SIMULATORS),
        help="which simulators to include (default: all)",
    )
    parser.add_argument("--limit", type=int, default=None, help="samples per cell (default: all)")
    parser.add_argument("--select", choices=("stratified", "head"), default="stratified")
    parser.add_argument("--out-dir", default="logs/bench")
    parser.add_argument(
        "--full-matrix",
        action="store_true",
        help="force incompatible cells (e.g. esci sim on a dataset without esci_query)",
    )
    parser.add_argument(
        "--hold-until-turn",
        type=int,
        default=None,
        help="override the emit-policy hold turn (default: the Agent's own value)",
    )
    args = parser.parse_args(argv)

    result = run_matrix(
        args.catalog,
        args.datasets,
        args.simulators,
        args.limit,
        args.select,
        args.full_matrix,
        args.hold_until_turn,
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = out_dir / f"{stamp}.json"
    md_path = out_dir / f"{stamp}.md"
    json_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(result) + "\n", encoding="utf-8")

    print()
    print(render_markdown(result))
    print(f"\nwrote {json_path}\n      {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
