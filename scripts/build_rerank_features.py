"""Compile ESCI human relevance judgments into reranker training tensors.

`scripts/calibrate_rerank.py` fits weights on sessions our own simulators
wrote. Its docstring is candid about the limit: the simulators build queries
from the target product's metadata, so the training pairs are *(query derived
from P, product P)*, and the generator's function is the ceiling on what can be
learned from them.

This script builds the other kind of training set. Each row is a **real Amazon
shopper query** with a **human E/S/C/I judgment** against a product that is in
the frozen catalog (`scripts/build_esci_gold.py`). Nothing here was generated
by anything we wrote.

One example = one query:

    dense       (candidates x 37) coefficients for the linear stages
    soft        (candidates x 37) coefficients for the soft stage, which
                `soft_scale` multiplies -- kept separate because that makes the
                score bilinear in the weights, not linear
    violations  (candidates,) determinate hard-constraint violations, which key
                the sort ahead of any score
    gain        (candidates,) 1.0 for Exact, 0.5 for Substitute, 0.0 otherwise

Coefficients come from the production `stage_contributions`, so a weight vector
fitted here means exactly what it means in `starter/retriever.py` -- there is no
second scorer to drift.

Two properties of the construction are worth stating because they are what
stops this being circular:

*   The query text is the shopper's, not ours. The extraction cascade runs on
    it exactly as it would in a live turn, so the plan carries whatever real
    phrasing actually yields -- including nothing.
*   Irrelevant judgments are kept. 62 of them are products a human looked at
    and rejected for that query, and they stay in the softmax denominator as
    hard negatives. Unlabelled pool members are only *presumed* negative;
    these are known ones. 30 survive retrieval into a pool, which is the
    number that trains anything.

Usage::

    python3 -m scripts.build_rerank_features
    python3 -m scripts.build_rerank_features --prune 400 --out data/rerank_features.npz
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from starter.agent import Agent
from starter.retriever import WEIGHT_NAMES, RerankWeights


WEIGHT_INDEX = {name: index for index, name in enumerate(WEIGHT_NAMES)}
SOFT_SCALE = WEIGHT_INDEX["soft_scale"]

# Graded relevance. Substitute is genuinely relevant -- it is a product the
# shopper could reasonably buy instead -- but ranking it level with Exact would
# teach the model that near-misses are the goal. Complement and Irrelevant are
# both zero: neither answers the query.
GAINS = {"E": 1.0, "S": 0.5, "C": 0.0, "I": 0.0}


class _RecordingRetriever:
    """Stands in for the retriever to capture the exact request one turn makes.

    Returning nothing is deliberate: the agent's own dialogue policy branches
    on state rather than on results, so an empty slate does not change the
    request being recorded.
    """

    def __init__(self) -> None:
        """Prepare an empty request log."""
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
        prior_ranks=None,
    ) -> list[str]:
        """Record one retrieval request and return no candidates."""
        self.calls.append({
            "search_query": search_query,
            "constraints": dict(constraints),
            "no_preference": [str(item) for item in no_preference],
            "raw_constraints": [dict(span) for span in raw_constraints],
            "user_profile": user_profile,
        })
        return []


def load_gold(path: Path) -> dict[int, dict]:
    """Group the judgment file by query: text plus {parent_asin: label}."""
    queries: dict[int, dict] = {}
    with path.open() as handle:
        for line in handle:
            row = json.loads(line)
            entry = queries.setdefault(
                row["query_id"],
                {"query": row["query"], "split": row["esci_split"], "labels": {}},
            )
            entry["labels"][row["parent_asin"]] = row["esci_label"]
    return queries


def capture_requests(agent: Agent, queries: dict[int, dict]) -> dict[int, dict]:
    """Run each query as a turn-1 message and record the retrieval request.

    Going through `Agent.respond` rather than calling `build_plan` directly is
    what keeps the features honest. The plan depends on the extraction cascade,
    the polarity layer, and the state manager's search-context assembly; a
    shortcut past any of them would compile features for a retriever that does
    not exist.
    """
    real = agent.retriever
    recorder = _RecordingRetriever()
    agent.retriever = recorder  # type: ignore[assignment]
    captured: dict[int, dict] = {}
    try:
        for query_id, entry in queries.items():
            session = f"esci_gold_{query_id}"
            recorder.calls = []
            agent.reset(session, {})
            agent.respond(session, entry["query"], turn=1, top_k=10)
            if recorder.calls:
                captured[query_id] = recorder.calls[-1]
    finally:
        agent.retriever = real
    return captured


def compile_query(
    retriever,
    request: dict,
    labels: dict[str, str],
    prune: int,
    baseline: np.ndarray,
) -> dict | None:
    """Score one query's candidate pool into coefficient matrices."""
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
        return None

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
        violations[row] = retriever.violations(record, plan)

    gain = np.array(
        [GAINS.get(labels.get(parent_asin, ""), 0.0) for parent_asin in ids],
        dtype=np.float32,
    )
    labelled = np.array(
        [1 if parent_asin in labels else 0 for parent_asin in ids], dtype=np.int8
    )

    # Prune to a workable width by *baseline* score, never by label. Every
    # judged product is kept regardless of where the baseline puts it --
    # dropping a positive the current weights rank badly would delete exactly
    # the examples there is something to learn from.
    if 0 < prune < len(ids):
        scores = dense @ baseline + baseline[SOFT_SCALE] * (soft @ baseline)
        order = np.lexsort((-scores, violations))[:prune]
        forced = np.flatnonzero(labelled)
        keep = np.sort(np.unique(np.concatenate([order, forced])))
        dense, soft, violations = dense[keep], soft[keep], violations[keep]
        gain, labelled = gain[keep], labelled[keep]
        ids = [ids[index] for index in keep]

    return {
        "dense": dense,
        "soft": soft,
        "violations": violations,
        "gain": gain,
        "labelled": labelled,
        "ids": ids,
    }


def main() -> None:
    """Compile every gold query into an .npz bundle of coefficient matrices."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", default="data/esci_gold_relevance.jsonl")
    parser.add_argument("--catalog", default="data/catalog.jsonl")
    parser.add_argument("--out", default="data/rerank_features.npz")
    parser.add_argument("--prune", type=int, default=400)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    queries = load_gold(Path(args.gold))
    if args.limit:
        queries = dict(list(queries.items())[: args.limit])
    print(f"{len(queries)} gold queries")

    agent = Agent(args.catalog)
    requests = capture_requests(agent, queries)
    print(f"{len(requests)} queries produced a retrieval request")

    baseline = np.array(
        [RerankWeights().as_mapping()[name] for name in WEIGHT_NAMES], dtype=np.float64
    )

    bundle: dict[str, np.ndarray] = {}
    meta: list[dict] = []
    reach = Counter()
    label_totals: Counter = Counter()
    for index, (query_id, entry) in enumerate(sorted(queries.items())):
        request = requests.get(query_id)
        if request is None:
            reach["no_request"] += 1
            continue
        compiled = compile_query(
            agent.retriever, request, entry["labels"], args.prune, baseline
        )
        if compiled is None:
            reach["empty_pool"] += 1
            continue

        found = int(compiled["labelled"].sum())
        positives = int((compiled["gain"] > 0).sum())
        reach["compiled"] += 1
        reach["judged_in_pool"] += found
        reach["judged_total"] += len(entry["labels"])
        if positives == 0:
            reach["no_positive_in_pool"] += 1

        key = f"q{query_id}"
        bundle[f"{key}_dense"] = compiled["dense"]
        bundle[f"{key}_soft"] = compiled["soft"]
        bundle[f"{key}_violations"] = compiled["violations"]
        bundle[f"{key}_gain"] = compiled["gain"]
        bundle[f"{key}_labelled"] = compiled["labelled"]
        for parent_asin in compiled["ids"]:
            if parent_asin in entry["labels"]:
                label_totals[entry["labels"][parent_asin]] += 1
        meta.append({
            "key": key,
            "query_id": query_id,
            "query": entry["query"],
            "split": entry["split"],
            "candidates": int(compiled["dense"].shape[0]),
            "positives": positives,
            "judged": found,
        })
        if (index + 1) % 100 == 0:
            print(f"  compiled {index + 1}/{len(queries)}", flush=True)

    bundle["__meta__"] = np.frombuffer(
        json.dumps(meta).encode("utf-8"), dtype=np.uint8
    )
    bundle["__weight_names__"] = np.array(WEIGHT_NAMES)
    np.savez_compressed(args.out, **bundle)

    usable = sum(1 for row in meta if row["positives"] > 0)
    print(
        f"\nwrote {args.out}\n"
        f"  queries compiled            {reach['compiled']}\n"
        f"  judged products in pool     {reach['judged_in_pool']}/{reach['judged_total']} "
        f"({reach['judged_in_pool'] / max(1, reach['judged_total']):.1%} BM25 reachability)\n"
        f"  labels reached              {dict(sorted(label_totals.items()))}\n"
        f"  queries with a positive     {usable}\n"
        f"  queries with none           {reach['no_positive_in_pool']}\n"
        f"  empty pool / no request     {reach['empty_pool']} / {reach['no_request']}"
    )
    splits = Counter(row["split"] for row in meta if row["positives"] > 0)
    print(f"  usable by ESCI split        {dict(sorted(splits.items()))}")


if __name__ == "__main__":
    main()
