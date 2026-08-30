"""Summarise failures in an opt-in agent JSONL trace.

Usage:
    python scripts/analyze_trace.py debug/deterministic_trace.jsonl
    python scripts/analyze_trace.py debug/deterministic_trace.jsonl results.json public_set.jsonl

The second form joins trace events with evaluator results and public labels by
their stable sample order. It reads ``data/catalog.jsonl`` only to recompute
score explanations for the target, rank 1, and rank 10. It never opens a gz
catalog.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from starter.retriever import CatalogRetriever


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def candidate_explanation(event: dict, parent_asin: object, retriever: CatalogRetriever | None) -> dict | None:
    score = event.get("candidate_scores", {}).get(str(parent_asin))
    if isinstance(score, dict):
        return {
            "bm25_rank": score.get("bm25_rank"),
            "bm25_fusion": score.get("fusion_score"),
            "constraint_total": score.get("constraint_score"),
            "constraint_details": score.get("constraint_details"),
            "quality": score.get("quality_score"),
            "final": score.get("final_score"),
        }
    if retriever is None:
        return None
    ids = event.get("bm25_candidate_ids", [])
    rank = ids.index(parent_asin) + 1 if parent_asin in ids else None
    return retriever.explain_candidate(
        str(parent_asin), event.get("constraints", {}), rank, event.get("raw_constraints", [])
    )


def main(argv: list[str]) -> int:
    if len(argv) not in {2, 4}:
        print(__doc__.strip())
        return 2
    traces = read_jsonl(Path(argv[1]))
    print(f"trace events: {len(traces)}")
    print("by scenario:", dict(Counter(item.get("scenario", "unknown") for item in traces)))
    print("recommendation turns:", sum(bool(item.get("recommendations")) for item in traces))

    if len(argv) == 2:
        return 0

    result_path = Path(argv[2])
    result_payload = json.loads(result_path.read_text(encoding="utf-8"))
    results = result_payload.get("sessions", []) if isinstance(result_payload, dict) else result_payload
    if not isinstance(results, list):
        raise ValueError("results must be evaluator JSON containing a sessions list")
    labels = read_jsonl(Path(argv[3]))
    catalog_path = Path(os.environ.get("TRACE_CATALOG_PATH", "data/catalog.jsonl"))
    if catalog_path.suffix == ".gz":
        raise ValueError("TRACE_CATALOG_PATH must be an uncompressed JSONL catalog")
    retriever = CatalogRetriever(catalog_path) if catalog_path.is_file() else None
    targets = {item.get("sample_id"): item.get("ground_truth", {}).get("parent_asin") for item in labels}
    # The evaluator generates opaque UUID session ids and does not pass a
    # sample id to Agent.  Its runs are sequential, so session first-seen
    # order is the only stable, non-invasive join key.
    session_order = list(dict.fromkeys(item.get("session_id") for item in traces if item.get("session_id")))
    events_by_sample = {
        result.get("sample_id"): [event for event in traces if event.get("session_id") == session_id]
        for result, session_id in zip(results, session_order)
    }
    failures = [item for item in results if not item.get("hit", False)]
    print(f"misses: {len(failures)}/{len(results)}")
    for result in failures[:30]:
        sample_id = result.get("sample_id")
        target = targets.get(sample_id, result.get("target_asin"))
        related = events_by_sample.get(sample_id, [])
        last = related[-1] if related else {}
        bm25_ids = last.get("bm25_candidate_ids", [])
        candidate_ids = last.get("candidate_ids", [])
        bm25_rank = bm25_ids.index(target) + 1 if target in bm25_ids else None
        candidate_rank = candidate_ids.index(target) + 1 if target in candidate_ids else None
        rank_one = candidate_ids[0] if candidate_ids else None
        cutoff = candidate_ids[9] if len(candidate_ids) >= 10 else None
        turn_history = []
        for event in related:
            bm25_turn_ids = event.get("bm25_candidate_ids", [])
            ranked_turn_ids = event.get("candidate_ids", [])
            turn_history.append({
                "turn": event.get("turn"),
                "ask": event.get("ask_attribute"),
                "operations": event.get("extracted_operations", []),
                "target_bm25_rank": bm25_turn_ids.index(target) + 1 if target in bm25_turn_ids else None,
                "target_rerank": ranked_turn_ids.index(target) + 1 if target in ranked_turn_ids else None,
                "emitted": bool(event.get("recommendations")),
            })
        print(json.dumps({
            "sample_id": sample_id,
            "scenario": result.get("scenario") or last.get("scenario"),
            "target_asin": target,
            "bm25_rank": bm25_rank,
            "candidate_rank": candidate_rank,
            "candidate_count": last.get("candidate_count"),
            "query": last.get("search_query"),
            "constraints": last.get("constraints"),
            "recommendations": last.get("recommendations"),
            "target_score": candidate_explanation(last, target, retriever),
            "rank_1": {"asin": rank_one, "score": candidate_explanation(last, rank_one, retriever)},
            "rank_10": {"asin": cutoff, "score": candidate_explanation(last, cutoff, retriever)},
            "turn_history": turn_history,
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
