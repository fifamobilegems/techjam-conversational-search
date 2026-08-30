"""Summarise failures in an opt-in agent JSONL trace.

Usage:
    python scripts/analyze_trace.py debug/deterministic_trace.jsonl
    python scripts/analyze_trace.py debug/deterministic_trace.jsonl results.json public_set.jsonl

The second form joins trace events with evaluator results and public labels by
their stable sample order.  It never opens a catalog file.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


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
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
