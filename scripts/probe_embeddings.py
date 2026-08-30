"""Inspect the embedding artifact before anyone wires it into ranking.

Two modes:

    python -m scripts.probe_embeddings --query "black leather ankle boots"
        Qualitative check. Prints the nearest catalog titles for a query, or
        the nearest neighbours of a `parent_asin`.

    python -m scripts.probe_embeddings --ceiling
        Quantitative check. For every public session it builds an ORACLE
        query from the hidden target product -- the same `intent_card` the
        evaluator's simulator discloses from -- and measures how often the
        target appears in the dense top-K.

The ceiling number answers the question worth answering first: can this index
reach the target at all? A dialogue policy can only lose recall from there,
so a poor ceiling means the fix belongs in `retrieval/document.py` or the
encoder, not in prompt or fusion tuning. This is a diagnostic, never part of
an agent turn -- it reads the ground truth labels, which the agent may not.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from retrieval.document import build_query_document
from retrieval.index import VectorIndex
from retrieval.store import ArtifactError
from starter.env import load_project_env


DEFAULT_SESSIONS = Path("data/public_set.jsonl")
DEFAULT_CATALOG = Path("data/catalog.jsonl")
CEILING_KS = (10, 50, 100, 500)


def load_catalog(path: Path) -> dict[str, dict]:
    with open(path, encoding="utf-8") as handle:
        return {
            str(row["parent_asin"]): row
            for row in (json.loads(line) for line in handle if line.strip())
        }


def load_sessions(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def oracle_query(product: dict) -> str:
    """The most informative query a perfect dialogue could ever assemble.

    Built from the evaluator's own intent card so the ceiling reflects what
    the simulated customer is actually willing to disclose, not everything
    printed on the listing.

    The evaluator is imported here rather than at module scope on purpose:
    it pulls in `starter.agent`, and `--query` must stay usable for
    inspecting the index while the agent is mid-refactor or broken.
    """
    from evaluator.local_evaluator import intent_card

    card = intent_card(product)
    constraints = {"category": card["target_category"]}
    spans = [
        {"attribute": "feature", "text": text, "weight": 1.0}
        for text in (*card["hard_constraints"], *card["soft_preferences"])
    ]
    return build_query_document(constraints=constraints, raw_constraints=spans)


def run_query(index: VectorIndex, query: str, catalog: dict[str, dict], top_k: int) -> None:
    hits = (
        index.similar_to(query, top_k=top_k)
        if query in index.store.ids
        else index.search(query, top_k=top_k)
    )
    for hit in hits:
        title = str(catalog.get(hit.parent_asin, {}).get("title", "?"))[:88]
        print(f"{hit.rank:>3}. {hit.score:6.3f}  {hit.parent_asin}  {title}")


def run_ceiling(
    index: VectorIndex,
    sessions: list[dict],
    catalog: dict[str, dict],
    limit: int | None,
) -> dict[str, float]:
    largest = max(CEILING_KS)
    hits_at = {k: 0 for k in CEILING_KS}
    ranks: list[int] = []
    scored = 0
    by_scenario: dict[str, list[int]] = {}

    for session in sessions[: limit or len(sessions)]:
        target = str(session.get("ground_truth", {}).get("parent_asin", ""))
        product = catalog.get(target)
        if product is None:
            continue
        scored += 1
        results = index.search(oracle_query(product), top_k=largest)
        rank = next((hit.rank for hit in results if hit.parent_asin == target), None)
        scenario = str(session.get("scenario_type", "unknown"))
        by_scenario.setdefault(scenario, []).append(rank or 0)
        if rank is None:
            continue
        ranks.append(rank)
        for k in CEILING_KS:
            if rank <= k:
                hits_at[k] += 1

    if not scored:
        raise SystemExit("No sessions could be scored -- is the catalog the frozen one?")

    print(f"\noracle dense ceiling over {scored} public sessions")
    for k in CEILING_KS:
        print(f"  hit rate @{k:<4} {hits_at[k] / scored:6.3f}  ({hits_at[k]}/{scored})")
    if ranks:
        median = sorted(ranks)[len(ranks) // 2]
        print(f"  median rank when found: {median}")
    print("\n  by scenario (hit rate @10):")
    for scenario, found in sorted(by_scenario.items()):
        rate = sum(1 for rank in found if 0 < rank <= 10) / len(found)
        print(f"    {scenario:<18} {rate:6.3f}  ({len(found)} sessions)")
    return {f"hit_rate_at_{k}": hits_at[k] / scored for k in CEILING_KS}


def main(argv: list[str] | None = None) -> int:
    load_project_env()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--query", default=None, help="free text, or a parent_asin for neighbours")
    parser.add_argument("--ceiling", action="store_true", help="oracle recall over the public sessions")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--limit", type=int, default=None, help="sessions to score in --ceiling mode")
    parser.add_argument("--backend", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--sessions", type=Path, default=DEFAULT_SESSIONS)
    args = parser.parse_args(argv)

    if not args.query and not args.ceiling:
        parser.error("pass --query TEXT or --ceiling")

    try:
        index = VectorIndex.load(backend=args.backend, model=args.model)
    except ArtifactError as error:
        raise SystemExit(str(error)) from error

    manifest = index.store.manifest
    print(f"index: {manifest['embedder']}  {manifest['count']} products  {manifest['dimension']}d")

    catalog = load_catalog(args.catalog)
    if args.query:
        run_query(index, args.query, catalog, args.top_k)
    if args.ceiling:
        run_ceiling(index, load_sessions(args.sessions), catalog, args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
