"""Canonical text views of catalog products and of the live session state.

Both sides of a dense retrieval system have to agree on how text is built.
If the build script and the query path disagree -- different field order,
different truncation, one side keeping "Department: Womens" and the other
dropping it -- the vectors land in subtly different regions of the space and
recall degrades in a way that is very hard to see from the scores alone.

Everything that turns a record into a string therefore lives here, and the
version string below is written into the embedding manifest. A stale artifact
built from an older document format is then a loud mismatch instead of a
silent quality regression.
"""

from __future__ import annotations

import re
from typing import Any, Iterable


# Bump on ANY change to the text produced by this module. `EmbeddingStore`
# refuses to load an artifact whose manifest records a different version.
DOCUMENT_VERSION = "v1"

# Sentence-transformer encoders truncate at a fixed token budget (256 word
# pieces for the default model). Feeding them more costs build time and buys
# nothing, so each field is capped and the document is capped again.
MAX_DOCUMENT_CHARS = 1200

FEATURE_LIMIT = 6
FEATURE_CHARS = 160
DESCRIPTION_CHARS = 400

# Listing fields that appear on tens of thousands of products. They carry no
# discriminative signal and would dominate the short document budget.
BOILERPLATE_DETAIL_KEYS = {
    "date first available",
    "is discontinued by manufacturer",
    "asin",
    "best sellers rank",
    "item model number",
    "customer reviews",
}

DETAIL_LIMIT = 6
DETAIL_CHARS = 80

WHITESPACE_RE = re.compile(r"\s+")


def _clean(value: object, limit: int | None = None) -> str:
    """Collapse whitespace and optionally truncate; empty for None."""
    if value in (None, ""):
        return ""
    text = WHITESPACE_RE.sub(" ", str(value)).strip()
    if limit is not None and len(text) > limit:
        text = text[:limit].rstrip()
    return text


def _as_list(value: object) -> list[str]:
    """Flatten any catalog value into a list of strings."""
    if value in (None, "", []):
        return []
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value if item not in (None, "")]
    if isinstance(value, dict):
        return [f"{key}: {_clean(item)}" for key, item in value.items() if item not in (None, "", [])]
    return [_clean(value)]


def _details(details: object) -> list[str]:
    """Keep the discriminative `details` entries, dropping ubiquitous keys."""
    if not isinstance(details, dict):
        return _as_list(details)[:DETAIL_LIMIT]
    kept: list[str] = []
    for key, value in details.items():
        if str(key).strip().lower() in BOILERPLATE_DETAIL_KEYS:
            continue
        if value in (None, "", []):
            continue
        kept.append(f"{_clean(key)}: {_clean(value, DETAIL_CHARS)}")
        if len(kept) >= DETAIL_LIMIT:
            break
    return kept


def build_product_document(product: dict[str, Any]) -> str:
    """Render one catalog row as the text that gets embedded.

    The layout is deliberately label-prefixed ("brand: Columbia") rather than
    a bag of raw values, because the query side is assembled from the same
    labelled slots. Matching structure on both sides is what lets a short
    constraint list sit near a long product listing in the vector space.
    """

    title = _clean(product.get("title"), 200)
    brand = _clean(product.get("store"), 80)

    categories = _as_list(product.get("categories"))
    # The leaf categories carry the product type; the root is always
    # "Clothing, Shoes & Jewelry" and is worthless inside this catalog.
    category_text = " > ".join(categories[1:] or categories)[:160]

    features = [_clean(item, FEATURE_CHARS) for item in _as_list(product.get("features"))]
    description = " ".join(_as_list(product.get("description")))

    price = product.get("price")
    price_text = "" if price in (None, "") else f"price: ${price}"

    lines = [
        title,
        f"brand: {brand}" if brand else "",
        f"category: {category_text}" if category_text else "",
        "; ".join(item for item in features[:FEATURE_LIMIT] if item),
        "; ".join(_details(product.get("details"))),
        _clean(description, DESCRIPTION_CHARS),
        price_text,
    ]
    document = ". ".join(line for line in lines if line)
    return _clean(document, MAX_DOCUMENT_CHARS)


def build_query_document(
    search_query: str = "",
    constraints: dict[str, Any] | None = None,
    raw_constraints: Iterable[dict] | None = None,
    no_preference: Iterable[str] = (),
) -> str:
    """Render the current session state as the text that gets embedded.

    Mirrors `build_product_document`: the same labels, the same separator.
    Callers pass `StateManager.export()` fields straight through --
    `search_query`, `constraints`, `raw_constraints`, `no_preference`.

    Attributes the customer has explicitly disclaimed are dropped; spans the
    state manager has demoted (weight below 1.0) are kept, because in this
    dataset an overridden preference still describes the same target product,
    but they are appended last where they influence the vector least.
    """

    skip = {str(item) for item in no_preference}
    constraints = constraints or {}

    parts: list[str] = []
    query_text = _clean(search_query, 300)
    if query_text:
        parts.append(query_text)

    for attribute, value in constraints.items():
        if attribute in skip or value in (None, "", []):
            continue
        parts.append(f"{attribute}: {_clean(value, 120)}")

    demoted: list[str] = []
    for span in raw_constraints or ():
        if not isinstance(span, dict):
            continue
        attribute = str(span.get("attribute", ""))
        if attribute in skip:
            continue
        text = _clean(span.get("text") or span.get("match_phrase") or "", 120)
        if not text:
            continue
        try:
            weight = float(span.get("weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        target = parts if weight >= 1.0 else demoted
        if text not in target:
            target.append(text)

    seen: set[str] = set()
    ordered = [part for part in (*parts, *demoted) if not (part in seen or seen.add(part))]
    return _clean(". ".join(ordered), MAX_DOCUMENT_CHARS)
