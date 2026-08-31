"""BM25 recall gate: the raw message vs what the pipeline actually searches.

This is the measurement that found the main robustness bug. On real ESCI
phrasing the extractor matches no template, ``build_search_context()`` returns an
empty string, ``_bm25_search`` gets nothing, and the target can never be
retrieved — even though the *raw* query would have found it. The two columns
below make that divergence impossible to miss, and keeping this script in the
tree makes it a permanent regression check rather than a one-off finding.

For each dataset x simulator it reports, over the turn-1 opening message:

    raw       recall@{100,500} of ``retriever._bm25_search(raw_message)``
    pipeline  recall@{100,500} of ``_bm25_search(build_search_context(...))``
    empty%    share of samples whose pipeline query is the empty string

The pipeline query is built with the real extractor + StateManager (the same
objects ``starter.agent.Agent`` uses), so the numbers match production. No agent
retrieval loop is needed for the default turn-1 view.

``--end-of-session`` additionally runs the full multi-turn session (reusing
``tools.trace_runner.run_session``) and measures recall of the accumulated user
text vs the final-turn ``search_query`` — confirming whether the empty-query
pathology persists once constraints have accumulated.

Usage::

    python3 -m scripts.measure_recall                 # turn 1, all cells
    python3 -m scripts.measure_recall --limit 400     # 400 samples per cell
    python3 -m scripts.measure_recall --end-of-session --limit 200
    python3 -m scripts.measure_recall --datasets esci1000 --simulators esci
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from evaluator.local_evaluator import catalog_index, coarse_category, load_jsonl
from starter.extractor import HeuristicTurnExtractor
from starter.retriever import CatalogRetriever
from state.state_manager import StateManager
from tools.trace_runner import build_customer, run_session, select_samples


DATASETS: dict[str, str] = {
    "public200": "data/public_set.jsonl",
    "synth800": "data/synth_set_800.jsonl",
    "esci1000": "data/esci_set_1000.jsonl",
}

SIMULATORS: tuple[str, ...] = ("official", "realistic", "esci")
SIMULATOR_REQUIRES: dict[str, str] = {"esci": "esci_query"}
KS = (100, 500)


def _dataset_supports(simulator: str, samples: list[dict]) -> bool:
    field = SIMULATOR_REQUIRES.get(simulator)
    if field is None:
        return True
    return any(field in s and str(s.get(field) or "").strip() for s in samples)


def _hit_at(retriever: CatalogRetriever, query: str, target: str) -> dict:
    """Rank of ``target`` in the raw BM25 candidate list, as recall@k flags."""
    query = (query or "").strip()
    if not query:
        return {"empty": True, **{k: False for k in KS}}
    ranked = retriever._bm25_search(query)  # bm25-ordered, up to candidate_limit
    rank = ranked.index(target) + 1 if target in ranked else None
    return {"empty": False, **{k: (rank is not None and rank <= k) for k in KS}}


def _pipeline_query_turn1(extractor: HeuristicTurnExtractor, message: str) -> str:
    """Reproduce the turn-1 ``state['search_query']`` the Agent would build.

    The message is recorded *before* export, mirroring the live ``Agent.respond``
    ordering (fixed in agent.py so the current turn's words reach
    ``build_search_context``). Before that fix this probe reported ~68% empty
    turn-1 queries on real phrasing; recording the message here keeps the tool
    honest about what the agent actually searches.
    """
    manager = StateManager()
    session_id = "recall_probe"
    manager.reset(session_id)
    manager.record_message(session_id, "user", message, 1)
    extracted = extractor.extract(message, manager.get(session_id))
    manager.update(session_id, extracted, 1)
    return manager.export(session_id).get("search_query", "") or ""


def _blank_acc() -> dict:
    return {
        "n": 0,
        "gold_n": 0,
        "raw": {k: 0 for k in KS},
        "pipeline": {k: 0 for k in KS},
        "raw_gold": {k: 0 for k in KS},
        "pipeline_empty": 0,
    }


def _fold(acc: dict, raw: dict, pipe: dict, is_gold: bool) -> None:
    acc["n"] += 1
    if is_gold:
        acc["gold_n"] += 1
    for k in KS:
        acc["raw"][k] += int(raw[k])
        acc["pipeline"][k] += int(pipe[k])
        if is_gold:
            acc["raw_gold"][k] += int(raw[k])
    if pipe["empty"]:
        acc["pipeline_empty"] += 1


def measure_turn1(
    retriever: CatalogRetriever,
    categories: dict[str, list[str]],
    products: dict[str, dict],
    samples: list[dict],
    simulator: str,
) -> dict:
    extractor = HeuristicTurnExtractor()
    acc = _blank_acc()
    for sample in samples:
        target = str(sample["ground_truth"]["parent_asin"])
        if target not in retriever.valid_ids:
            continue
        coarse = coarse_category(categories.get(target, []))
        customer = build_customer(simulator, sample, products, target, coarse)
        message = customer.opening()
        raw = _hit_at(retriever, message, target)
        pipe_query = _pipeline_query_turn1(extractor, message)
        pipe = _hit_at(retriever, pipe_query, target)
        _fold(acc, raw, pipe, str(sample.get("provenance")) == "gold")
    return acc


def measure_end_of_session(
    retriever: CatalogRetriever,
    catalog_ids: set[str],
    categories: dict[str, list[str]],
    products: dict[str, dict],
    samples: list[dict],
    simulator: str,
) -> dict:
    """Recall of accumulated user text vs the final-turn pipeline query."""
    from starter.agent import Agent

    agent = Agent(retriever.catalog_path)
    acc = _blank_acc()
    for sample in samples:
        target = str(sample["ground_truth"]["parent_asin"])
        if target not in retriever.valid_ids:
            continue
        trace = run_session(agent, sample, catalog_ids, categories, products, simulator)
        turns = trace.get("turns") or []
        if not turns:
            continue
        raw_text = " ".join(str(t.get("user_message", "")) for t in turns)
        final_state = trace.get("final_state") or (turns[-1].get("state") or {})
        pipe_query = str(final_state.get("search_query", "") or "")
        raw = _hit_at(retriever, raw_text, target)
        pipe = _hit_at(retriever, pipe_query, target)
        _fold(acc, raw, pipe, str(sample.get("provenance")) == "gold")
    return acc


def _rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def summarise_cell(dataset: str, simulator: str, phase: str, acc: dict) -> dict:
    n = acc["n"]
    cell = {
        "dataset": dataset,
        "simulator": simulator,
        "phase": phase,
        "n": n,
        "pipeline_empty_rate": _rate(acc["pipeline_empty"], n),
    }
    for k in KS:
        cell[f"raw_recall@{k}"] = _rate(acc["raw"][k], n)
        cell[f"pipeline_recall@{k}"] = _rate(acc["pipeline"][k], n)
    if acc["gold_n"]:
        for k in KS:
            cell[f"raw_recall@{k}_gold"] = _rate(acc["raw_gold"][k], acc["gold_n"])
    return cell


def render_markdown(result: dict) -> str:
    lines: list[str] = []
    lines.append(f"# BM25 recall gate — {result['generated_at']}")
    lines.append("")
    lines.append(
        f"`limit={result['limit'] if result['limit'] is not None else 'all'}` · "
        f"`select={result['select']}` · catalog `{result['catalog']}`"
    )
    lines.append("")
    lines.append(
        "The **raw** columns feed the customer's own words to BM25; the "
        "**pipeline** columns feed `build_search_context()`. A large gap — and a "
        "high **empty%** — is the extractor discarding the query."
    )
    lines.append("")
    lines.append(
        "| dataset | simulator | phase | n | raw@100 | pipe@100 | raw@500 | pipe@500 | empty% |"
    )
    lines.append("|---|---|---|--:|--:|--:|--:|--:|--:|")
    for c in result["cells"]:
        lines.append(
            f"| {c['dataset']} | {c['simulator']} | {c['phase']} | {c['n']} | "
            f"{c['raw_recall@100']:.3f} | {c['pipeline_recall@100']:.3f} | "
            f"{c['raw_recall@500']:.3f} | {c['pipeline_recall@500']:.3f} | "
            f"{c['pipeline_empty_rate']*100:.1f}% |"
        )
    lines.append("")
    gold = [c for c in result["cells"] if "raw_recall@500_gold" in c]
    if gold:
        lines.append("## Gold-only raw recall (ESCI human-labelled rows)")
        lines.append("")
        lines.append("| dataset | simulator | phase | raw@100 gold | raw@500 gold |")
        lines.append("|---|---|---|--:|--:|")
        for c in gold:
            lines.append(
                f"| {c['dataset']} | {c['simulator']} | {c['phase']} | "
                f"{c['raw_recall@100_gold']:.3f} | {c['raw_recall@500_gold']:.3f} |"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BM25 recall: raw message vs build_search_context()")
    parser.add_argument("--catalog", default="data/catalog.jsonl")
    parser.add_argument("--datasets", nargs="+", choices=tuple(DATASETS), default=list(DATASETS))
    parser.add_argument("--simulators", nargs="+", choices=SIMULATORS, default=list(SIMULATORS))
    parser.add_argument("--limit", type=int, default=None, help="samples per cell (default: all)")
    parser.add_argument("--select", choices=("stratified", "head"), default="stratified")
    parser.add_argument("--end-of-session", action="store_true", help="also measure the multi-turn accumulated query")
    parser.add_argument("--out", default="docs/recall.md", help="markdown report path (kept committed)")
    parser.add_argument("--json-out", default=None, help="optional JSON path (default: alongside --out)")
    args = parser.parse_args(argv)

    catalog_ids, categories, products = catalog_index(args.catalog)
    retriever = CatalogRetriever(args.catalog)
    print(f"catalog indexed: {len(retriever.valid_ids)} products\n")

    cells: list[dict] = []
    for dataset in args.datasets:
        all_samples = load_jsonl(DATASETS[dataset])
        chosen = all_samples if args.limit is None else select_samples(all_samples, args.limit, args.select)
        for simulator in args.simulators:
            if not _dataset_supports(simulator, chosen):
                print(f"  {dataset:<10} x {simulator:<10}  — skipped (no {SIMULATOR_REQUIRES[simulator]})")
                continue
            acc = measure_turn1(retriever, categories, products, chosen, simulator)
            cell = summarise_cell(dataset, simulator, "turn1", acc)
            cells.append(cell)
            print(
                f"  {dataset:<10} x {simulator:<10} turn1  n={cell['n']:<5} "
                f"raw@500={cell['raw_recall@500']:.3f} pipe@500={cell['pipeline_recall@500']:.3f} "
                f"empty={cell['pipeline_empty_rate']*100:.0f}%"
            )
            if args.end_of_session:
                acc_eos = measure_end_of_session(
                    retriever, catalog_ids, categories, products, chosen, simulator
                )
                cell_eos = summarise_cell(dataset, simulator, "end_of_session", acc_eos)
                cells.append(cell_eos)
                print(
                    f"  {dataset:<10} x {simulator:<10} eos    n={cell_eos['n']:<5} "
                    f"raw@500={cell_eos['raw_recall@500']:.3f} pipe@500={cell_eos['pipeline_recall@500']:.3f} "
                    f"empty={cell_eos['pipeline_empty_rate']*100:.0f}%"
                )

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "catalog": str(args.catalog),
        "limit": args.limit,
        "select": args.select,
        "cells": cells,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_markdown(result) + "\n", encoding="utf-8")
    json_path = Path(args.json_out) if args.json_out else out_path.with_suffix(".json")
    json_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    print()
    print(render_markdown(result))
    print(f"\nwrote {out_path}\n      {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
