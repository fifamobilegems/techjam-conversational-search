"""Join the public Amazon ESCI relevance judgments onto the frozen catalog.

`data/esci_set_1000.jsonl` carries 234 rows marked ``provenance == "gold"``.
Those are the only *human* relevance labels anywhere in this project: every
other training signal we have was written by a simulator we also wrote, and
fitting a ranker to your own generator can only teach it to imitate the
generator. This script goes back to the source and takes everything the frozen
catalog can support.

Two join paths, both public-data-only:

``asin``
    ESCI ``product_id`` equals a catalog ``parent_asin``. Exact, zero risk,
    and what the existing 234 rows used. It finds 357 judgments.

``title``
    ESCI ``product_id`` is frequently a *child* ASIN -- one size or colour of a
    listing whose parent is in the catalog -- and a child ASIN never equals its
    parent, so the exact join drops it. Amazon variants share the parent's
    title verbatim, so a normalized title match recovers them. Titles that are
    not unique within the catalog are excluded outright: an ambiguous title
    would attach a human judgment to an arbitrary one of several products,
    which is worse than not having the row.

Together: **786 judgments over 748 real queries and 442 catalog products**, up
from 234. That is the ceiling, not a sample. The full 2.62M-judgment ESCI
release was scanned to find them; the frozen 50,000-product catalog is what
limits the yield, so no amount of additional fetching moves this number.

What the labels mean (Reddy et al. 2022, arXiv:2206.06588):

    E  Exact       -- satisfies the query
    S  Substitute  -- does not fully satisfy it but is a reasonable swap
    C  Complement  -- bought alongside, not instead
    I  Irrelevant  -- does not satisfy the query

Only ``query`` text and ``esci_label`` are read. No ESCI product metadata
enters the catalog, and nothing here touches the unreleased evaluation labels.

Source files (~1.2 GB, cached under ``data/esci_raw/``, gitignored) come from
the official release at https://github.com/amazon-science/esci-data.

Usage::

    python3 -m scripts.build_esci_gold              # download if needed, then build
    python3 -m scripts.build_esci_gold --source-dir /path/to/parquet
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow.parquet as pq


BASE_URL = (
    "https://media.githubusercontent.com/media/amazon-science/esci-data/main/"
    "shopping_queries_dataset"
)

FILES = {
    "examples": "shopping_queries_dataset_examples.parquet",
    "products": "shopping_queries_dataset_products.parquet",
}

# The competition catalog is `Clothing_Shoes_and_Jewelry`, US locale.
LOCALE = "us"

_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


def normalize_title(value: object) -> str:
    """Collapse a title to comparable form: lowercase alphanumerics, single spaces.

    Deliberately aggressive. Variant listings differ in punctuation and
    whitespace far more often than in words, so a strict comparison would drop
    matches that are the same product by any reading.
    """
    return _NORMALIZE_RE.sub(" ", str(value).lower()).strip()


def download(source_dir: Path) -> dict[str, Path]:
    """Fetch the ESCI parquet files into ``source_dir`` unless already present."""
    source_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for key, name in FILES.items():
        target = source_dir / name
        if not target.exists():
            print(f"downloading {name} ...", flush=True)
            urllib.request.urlretrieve(f"{BASE_URL}/{name}", target)
        paths[key] = target
        print(f"{key}: {target} ({target.stat().st_size / 1e6:.0f} MB)")
    return paths


def catalog_keys(catalog_path: Path) -> tuple[set[str], dict[str, str]]:
    """Return the catalog's parent ASINs and its unambiguous title -> ASIN map.

    A title shared by two catalog products is dropped rather than resolved.
    There is no principled tie-break, and a wrong one silently mislabels a
    training row.
    """
    by_title: dict[str, list[str]] = defaultdict(list)
    asins: set[str] = set()
    with catalog_path.open() as handle:
        for line in handle:
            product = json.loads(line)
            parent = str(product["parent_asin"])
            asins.add(parent)
            by_title[normalize_title(product.get("title"))].append(parent)

    unique = {
        title: parents[0]
        for title, parents in by_title.items()
        if len(parents) == 1 and title
    }
    ambiguous = sum(1 for parents in by_title.values() if len(parents) > 1)
    print(
        f"catalog: {len(asins)} products, {len(unique)} unambiguous titles "
        f"({ambiguous} titles shared by 2+ products, excluded)"
    )
    return asins, unique


def title_map(products_path: Path, unique_titles: dict[str, str]) -> dict[str, str]:
    """Map ESCI ``product_id`` -> catalog ``parent_asin`` by normalized title."""
    table = pq.read_table(
        products_path, columns=["product_id", "product_title", "product_locale"]
    ).to_pandas()
    table = table[table.product_locale == LOCALE]
    mapping = {
        row.product_id: unique_titles[normalize_title(row.product_title)]
        for row in table.itertuples()
        if normalize_title(row.product_title) in unique_titles
    }
    print(
        f"title join: {len(table)} ESCI {LOCALE} products -> {len(mapping)} matched "
        f"({len(set(mapping.values()))} distinct catalog products)"
    )
    return mapping


def build_rows(
    examples_path: Path, catalog_asins: set[str], titles: dict[str, str]
) -> list[dict]:
    """Produce one row per (ESCI query, catalog product) human judgment."""
    table = pq.read_table(
        examples_path,
        columns=[
            "query_id", "query", "product_id", "product_locale", "esci_label", "split"
        ],
    ).to_pandas()
    table = table[table.product_locale == LOCALE]
    print(f"examples: {len(table)} {LOCALE} judgments scanned")

    rows: list[dict] = []
    for item in table.itertuples():
        if item.product_id in catalog_asins:
            parent, join = item.product_id, "asin"
        elif item.product_id in titles:
            parent, join = titles[item.product_id], "title"
        else:
            continue
        rows.append({
            "query_id": int(item.query_id),
            "query": str(item.query),
            "parent_asin": parent,
            "esci_label": str(item.esci_label),
            "esci_product_id": str(item.product_id),
            "join": join,
            # ESCI's own train/test split. Carried through so a model fitted
            # here can be held out the way the dataset authors intended,
            # rather than on a split we invented to flatter ourselves.
            "esci_split": str(item.split),
        })

    # A query/product pair can appear once per ESCI version flag; keep one.
    seen: set[tuple[int, str]] = set()
    unique_rows = []
    for row in rows:
        key = (row["query_id"], row["parent_asin"])
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(row)
    return unique_rows


def main() -> None:
    """Build `data/esci_gold_relevance.jsonl` and print its composition."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", default="data/esci_raw")
    parser.add_argument("--catalog", default="data/catalog.jsonl")
    parser.add_argument("--out", default="data/esci_gold_relevance.jsonl")
    args = parser.parse_args()

    paths = download(Path(args.source_dir))
    asins, unique_titles = catalog_keys(Path(args.catalog))
    titles = title_map(paths["products"], unique_titles)
    rows = build_rows(paths["examples"], asins, titles)
    rows.sort(key=lambda row: (row["query_id"], row["parent_asin"]))

    out = Path(args.out)
    with out.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")

    labels = Counter(row["esci_label"] for row in rows)
    joins = Counter(row["join"] for row in rows)
    queries = {row["query_id"] for row in rows}
    products = {row["parent_asin"] for row in rows}
    per_query = Counter(row["query_id"] for row in rows)

    print(
        f"\nwrote {out}: {len(rows)} judgments\n"
        f"  labels      {dict(sorted(labels.items()))}\n"
        f"  join path   {dict(sorted(joins.items()))}\n"
        f"  queries     {len(queries)}\n"
        f"  products    {len(products)}\n"
        f"  queries with 2+ labelled catalog products: "
        f"{sum(1 for count in per_query.values() if count > 1)}"
    )


if __name__ == "__main__":
    main()
