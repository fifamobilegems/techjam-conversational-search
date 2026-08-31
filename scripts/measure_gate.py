"""Escalation-rate sweep for the Tier 2 gate -- offline, zero model calls.

The gate decides *whether* to spend a model call. That decision is a pure
function of `HeuristicTurnExtractor.last_trace`, so the rate it produces can be
measured exactly without a key, a network, or a cent of spend: run the real
sessions, run the real cascade, and ask the real gate what it would have done.

That separation matters because escalation rate is the cost line in the
submission disclosure. Widening the gate is only defensible if the new rate is
known *before* anyone pays for it, and known for every phrasing rather than
only the official one -- a gate tuned on template phrasing would be tuned on
exactly the turns it must never fire for.

What it reports, per (dataset x simulator) cell and threshold setting:

    escalations      turns the gate would send to the model
    per_session      escalations / sessions -- the disclosure number
    empty            escalations from the Phase 8 opening (cascade silent)
    low_confidence   escalations from the widened opening (thin Tier 1 read)
    blocked_*        why the other turns did not qualify

Usage::

    python3 -m scripts.measure_gate                        # default sweep
    python3 -m scripts.measure_gate --limit 200
    python3 -m scripts.measure_gate --coverage 0.4 0.6 --residual 2 3
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from evaluator.local_evaluator import catalog_index, load_jsonl
from starter.agent import Agent
from starter.extractor import HeuristicTurnExtractor
from state.llm_extractor import (
    GATE_EMPTY,
    GATE_LOW_CONFIDENCE,
    LLMTurnExtractor,
)
from tools.trace_runner import run_session, select_samples


DATASETS: dict[str, str] = {
    "public200": "data/public_set.jsonl",
    "synth800": "data/synth_set_800.jsonl",
    "esci1000": "data/esci_set_1000.jsonl",
}

SIMULATORS: tuple[str, ...] = ("official", "realistic", "esci")

# Datasets that carry the field a simulator needs; mirrors tools.bench so the
# two harnesses agree on which cells exist.
SIMULATOR_REQUIRES: dict[str, str] = {"esci": "esci_query"}


class _CountingExtractor(LLMTurnExtractor):
    """The real gate with the model amputated.

    Subclassing rather than reimplementing is the point: a second copy of the
    gate logic would drift from the one that actually spends money, and the
    measurement would slowly stop describing production. Here `should_escalate`
    is inherited verbatim and only the call it guards is removed.
    """

    def __init__(self, fallback: HeuristicTurnExtractor) -> None:
        """Wrap a deterministic extractor without constructing a model client."""
        super().__init__(fallback)
        self._client = None
        # The budget guard must not truncate a measurement. A run that stops
        # counting at 250 would report the cap, not the rate.
        self.max_calls = 10**9

    def extract(self, user_message: str, state: object | None = None):
        """Run the cascade, ask the gate, count the answer, call nothing."""
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        extracted = self.fallback.extract(user_message, state)
        if self.should_escalate(state):
            self.calls += 1
        return extracted


def _dataset_supports(simulator: str, samples: list[dict]) -> bool:
    """True when a dataset carries the field a simulator needs."""
    field = SIMULATOR_REQUIRES.get(simulator)
    if field is None:
        return True
    return any(field in sample and str(sample.get(field) or "").strip() for sample in samples)


def measure_cell(
    agent: Agent,
    extractor: _CountingExtractor,
    samples: list[dict],
    catalog_ids: set[str],
    categories: dict[str, list[str]],
    products: dict[str, dict],
    simulator: str,
) -> dict:
    """Replay one cell and return its gate accounting."""
    extractor.calls = 0
    extractor.gate_counts = Counter()
    for sample in samples:
        run_session(agent, sample, catalog_ids, categories, products, simulator)

    counts = dict(extractor.gate_counts)
    sessions = len(samples)
    # An escalation increments both its reason key and the "escalated" total,
    # so summing every counter would count those turns twice.
    turns = sum(value for key, value in counts.items() if key != "escalated")
    return {
        "sessions": sessions,
        "turns": turns,
        "escalations": extractor.calls,
        "per_session": round(extractor.calls / sessions, 4) if sessions else 0.0,
        "empty": counts.get("escalated_empty", 0),
        "low_confidence": counts.get("escalated_low_confidence", 0),
        "gate_counts": counts,
    }


def run_sweep(
    datasets: list[str],
    simulators: list[str],
    settings: list[dict],
    limit: int,
    catalog_path: str,
) -> dict:
    """Measure every (setting x dataset x simulator) combination."""
    agent = Agent(catalog_path)
    # Replace whatever the Agent built with the counting wrapper. The
    # deterministic cascade underneath is the same object either way, so the
    # sessions replay identically -- only the gate's answer is recorded rather
    # than acted on.
    base = agent.extractor
    while not isinstance(base, HeuristicTurnExtractor):
        base = base.fallback
    extractor = _CountingExtractor(base)
    agent.extractor = extractor

    catalog_ids, categories, products = catalog_index(catalog_path)
    loaded = {
        name: select_samples(load_jsonl(DATASETS[name]), limit) for name in datasets
    }

    results: list[dict] = []
    for setting in settings:
        extractor.gate = setting["gate"]
        extractor.gate_coverage = setting["coverage"]
        extractor.gate_residual = setting["residual"]
        for dataset in datasets:
            samples = loaded[dataset]
            for simulator in simulators:
                if not _dataset_supports(simulator, samples):
                    continue
                cell = measure_cell(
                    agent, extractor, samples, catalog_ids, categories, products, simulator
                )
                results.append({**setting, "dataset": dataset, "simulator": simulator, **cell})
                label = (
                    setting["gate"]
                    if setting["gate"] == GATE_EMPTY
                    else f"{setting['gate']}(c<={setting['coverage']},r>={setting['residual']})"
                )
                print(
                    f"{label:<34} {dataset:<10} {simulator:<10} "
                    f"escalations={cell['escalations']:>4}  "
                    f"per_session={cell['per_session']:<7} "
                    f"empty={cell['empty']:>4} low_conf={cell['low_confidence']:>4}",
                    flush=True,
                )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "limit": limit,
        "cells": results,
    }


def main() -> None:
    """Sweep gate settings and write the accounting to docs/gate_sweep.json."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--datasets", nargs="+", default=list(DATASETS))
    parser.add_argument("--simulators", nargs="+", default=list(SIMULATORS))
    parser.add_argument("--coverage", nargs="+", type=float, default=[0.34, 0.5, 0.67])
    parser.add_argument("--residual", nargs="+", type=int, default=[2, 3])
    parser.add_argument("--catalog", default="data/catalog.jsonl")
    parser.add_argument("--out", default="docs/gate_sweep.json")
    args = parser.parse_args()

    settings = [{"gate": GATE_EMPTY, "coverage": 0.0, "residual": 0}]
    settings += [
        {"gate": GATE_LOW_CONFIDENCE, "coverage": coverage, "residual": residual}
        for residual in args.residual
        for coverage in args.coverage
    ]

    report = run_sweep(
        args.datasets, args.simulators, settings, args.limit, args.catalog
    )
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
