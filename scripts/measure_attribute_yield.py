"""
Measure how much information each `ask_attribute` value actually returns.

The clarification order in `StateManager` should be a measurement, not an
intuition. This reproduces the evaluator's own customer policy over the
public sessions and counts, for every allowed attribute, how many disclosed
constraints that question would elicit.

Run:

    python -m scripts.measure_attribute_yield
    python -m scripts.measure_attribute_yield --output docs/attribute_yield.json
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
from pathlib import Path

from evaluator.local_evaluator import (
    ALLOWED_ATTRIBUTES,
    classify_constraint,
    materialize_hidden_fields,
)


def load_catalog(path: Path) -> dict[str, dict]:
    """Load the catalog into a parent_asin -> product map."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, mode="rt", encoding="utf-8") as handle:
        return {
            str(product["parent_asin"]): product
            for product in (json.loads(line) for line in handle if line.strip())
        }


def measure(dataset: Path, catalog: Path) -> dict:
    """Measure how often each attribute is answerable from the catalog.

    Feeds the clarification policy: asking about an attribute that few
    products carry wastes a turn.
    """
    products = load_catalog(catalog)
    samples = [json.loads(line) for line in dataset.open(encoding="utf-8") if line.strip()]

    yields: collections.Counter = collections.Counter()
    total = 0

    for sample in samples:
        card, _ = materialize_hidden_fields(sample, products)
        constraints = [
            *card.get("hard_constraints", []),
            *card.get("soft_preferences", []),
        ]
        for constraint in constraints:
            total += 1
            yields[classify_constraint(str(constraint))] += 1

    # "other" is answered without being classified, so it can elicit any
    # constraint regardless of class -- and unlike a per-class question it
    # never exhausts while anything is left undisclosed.
    yields["other"] = total

    ordering = [
        attribute
        for attribute, _ in sorted(
            ((name, yields.get(name, 0)) for name in ALLOWED_ATTRIBUTES),
            key=lambda item: (-item[1], item[0]),
        )
    ]

    return {
        "sample_count": len(samples),
        "constraint_count": total,
        "yield_by_attribute": {name: yields.get(name, 0) for name in ordering},
        "clarification_priority": ordering,
    }


def main() -> None:
    """Command-line entry point for attribute-yield measurement."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="data/public_set.jsonl")
    parser.add_argument("--catalog", default="catalog.jsonl.gz")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    result = measure(Path(args.dataset), Path(args.catalog))
    rendered = json.dumps(result, indent=2)

    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")

    print(rendered)


if __name__ == "__main__":
    main()
