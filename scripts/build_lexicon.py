"""Mine an attribute lexicon from the frozen catalog.

    python3 -m scripts.build_lexicon            # -> data/lexicon.json

`starter/extractor.py` hardcodes 12 colors and 10 materials. The catalog holds
1,165 distinct ``Color`` values and 463 distinct ``Material`` values, so a real
shopper saying "burgundy" or "faux leather" falls straight through Tier 0. That
gap is what this script closes.

Structured ``details`` coverage is sparse -- Color sits on 4.9% of products,
Material on 4.1% -- so ``details`` is used only to learn *which strings are
values of which attribute*. Tier 1 then matches those strings across the whole
message, and the reranker matches them across the whole product text.

Two attributes are mined from full-coverage fields instead of ``details``,
because they carry most real queries and ``details`` barely mentions them:

``category``
    from the ``categories`` tree (100% coverage). Real queries are dominated by
    product type -- "hoodies for men", "womens trail running shoes" -- and Tier
    0 finds a category only when a "looking for X" template matches.
``brand``
    from ``store`` (100% coverage), held to a far higher frequency floor than
    anything else: a false brand tag costs -50 in ``retriever._brand_score``,
    the harshest penalty in the reranker.

Output is deterministic -- sorted throughout, floors recorded in the payload --
and committed, so extraction never depends on a build step at scoring time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from starter.retriever import BOILERPLATE_PHRASES


DEFAULT_CATALOG = Path("data/catalog.jsonl")
DEFAULT_OUTPUT = Path("data/lexicon.json")

LEXICON_VERSION = 1

# Minimum number of catalog products evidencing a value before it earns an
# entry. Tuned per attribute by what a false positive costs in
# `retriever._constraint_score_details`: brand mismatch -50, category -20,
# a bad style guess -12.
#
# Colour and material sit at 2 deliberately. At a floor of 3 the vocabulary
# drops from 107 entries to 61 and loses precisely the words Tier 0 already
# fails on -- "bordeaux", "army green", "vegan leather", "patent leather".
# The junk that comes with a floor of 2 is removed by the filters below,
# which are targeted, rather than by the floor, which is not.
DEFAULT_FLOORS = {
    "brand": 40,
    "category": 8,
    "color": 2,
    "material": 2,
    "size": 4,
    "style": 6,
}

# Which catalog `details` keys feed which agent attribute. Several keys carry
# the same information under different vendor spellings.
DETAIL_SOURCES = {
    "color": ("Color",),
    "material": ("Material", "Fabric Type", "Outer Material"),
    "size": ("Size",),
    "style": ("Style",),
}

# Frequent but useless as query terms: they match everything or nothing.
VALUE_STOPLIST = {
    "generic", "unknown", "n a", "na", "none", "no", "other", "various",
    "assorted", "multi", "as shown", "as picture", "see description",
    "standard", "regular", "default", "one size", "one size fits all",
    "solid", "plain", "custom", "imported", "new", "brand new",
    "human", "shell", "reshape", "design", "no metal type", "colorful",
    "multicolor", "multi color", "mixed", "assorted colors",
}

# Brand names that are also ordinary English or ordinary product words. A store
# genuinely called "Casual" or "Fashion" would otherwise tag every third query
# with a -50 penalty attached.
BRAND_WORD_STOPLIST = {
    "casual", "classic", "comfort", "dress", "fashion", "generic", "jewelry",
    "shoes", "sport", "sports", "style", "collection", "amazon collection",
    "boots", "socks", "watch", "watches", "kids", "women", "men", "girls",
    "boys", "unisex", "vintage", "modern", "elegant", "premium", "quality",
    "the", "and", "for", "one", "two", "star", "gold", "silver", "black",
}

# Audience words. Every category path contains one ("Women > Shoes > Boots"),
# so they carry no discriminative signal, and as category values they score
# +45 against almost the whole catalog while displacing the real product type:
# "hoodies for men" would tag `category=men` and lose the hoodie.
AUDIENCE_STOPLIST = {
    "men", "mens", "women", "womens", "man", "woman", "girl", "girls",
    "boy", "boys", "kid", "kids", "child", "children", "baby", "babies",
    "adult", "adults", "unisex", "junior", "juniors", "teen", "teens",
    "toddler", "toddlers", "infant", "infants", "male", "female", "ladies",
    "gents", "youth", "big", "tall", "petite", "plus",
}

# Vendor prose that leaked into a metadata field: a sentence fragment, not a
# value. "layer" catches "inner layer fleece warmth" and friends.
JUNK_SUBSTRINGS = ("please", "contact", "click", "http", "www", " etc", "layer")

PERCENT_RE = re.compile(r"\b\d{1,3}\s*%\s*")
SPLIT_RE = re.compile(r"\s*(?:[/,;+]|\band\b|&)\s*")

# Shorter than this is too collision-prone to match on. Sizes are exempt --
# "8" and "xl" are legitimate and unambiguous in context.
MIN_VALUE_CHARS = 3
MAX_VALUE_WORDS = 4


def normalize(value: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Mirrors `state.state_manager.normalize_phrase`, so a lexicon surface form
    and a stored constraint span compare equal.
    """
    lowered = PERCENT_RE.sub(" ", str(value).lower())
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", lowered)).strip()


def variants_of(canonical: str) -> set[str]:
    """Surface forms that should map to one canonical value.

    Limited to spelling and morphology the catalog itself demonstrates --
    plurals, the grey/gray split, hyphen loss. Edit distance beyond this is not
    justified by any measured failure and would start matching "boot" to "boat".
    """
    forms = {canonical}
    for form in tuple(forms):
        if form.endswith("ies"):
            forms.add(form[:-3] + "y")
        elif form.endswith("es") and len(form) > 4:
            forms.add(form[:-2])
        if form.endswith("s") and len(form) > 3:
            forms.add(form[:-1])
        else:
            forms.add(form + "s")
        if " " in form:
            forms.add(form.replace(" ", ""))
    if "grey" in canonical:
        forms.add(canonical.replace("grey", "gray"))
    if "gray" in canonical:
        forms.add(canonical.replace("gray", "grey"))
    return {form for form in forms if form}


def _iter_catalog(path: Path) -> Iterator[dict]:
    """Stream catalog rows."""
    if not path.exists():
        raise SystemExit(f"Catalog not found: {path}")
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _split_values(raw: object) -> list[str]:
    """Split compound metadata: "Polyester, Cotton" is two materials."""
    parts = [part for part in SPLIT_RE.split(str(raw)) if part and part.strip()]
    return parts or [str(raw)]


def _acceptable(attribute: str, value: str) -> bool:
    """True when a mined value is worth a lexicon entry.

    This is where the mined vocabulary is cleaned: vendor prose, SKU
    fragments, audience words and generic brand names are all rejected here
    rather than by raising the frequency floor, which would also discard the
    rare-but-real words shoppers actually use.
    """
    if not value or value in VALUE_STOPLIST or value in BOILERPLATE_PHRASES:
        return False
    if any(marker in value for marker in JUNK_SUBSTRINGS):
        return False
    words = value.split()
    if len(words) > MAX_VALUE_WORDS:
        return False
    if attribute != "size" and len(value) < MIN_VALUE_CHARS:
        return False
    # Digits inside a colour, material or brand mean a vendor SKU fragment
    # leaked in ("1 black", "0212g", "design 1"). Sizes are the exception.
    if attribute != "size" and any(character.isdigit() for character in value):
        return False
    if attribute == "category" and value in AUDIENCE_STOPLIST:
        return False
    if attribute == "brand":
        # The stoplist is checked both as a whole phrase ("amazon collection")
        # and word-by-word, so a house brand made entirely of generic words is
        # rejected either way.
        if value in BRAND_WORD_STOPLIST or len(words) > 3:
            return False
        if all(word in BRAND_WORD_STOPLIST for word in words):
            return False
    return True


def mine(catalog_path: Path) -> dict[str, Counter]:
    """Count how many catalog products evidence each (attribute, value) pair."""
    counts: dict[str, Counter] = defaultdict(Counter)

    for product in _iter_catalog(catalog_path):
        details = product.get("details")
        if isinstance(details, dict):
            for attribute, keys in DETAIL_SOURCES.items():
                for key in keys:
                    raw = details.get(key)
                    if raw in (None, "", []):
                        continue
                    for part in _split_values(raw):
                        value = normalize(part)
                        if _acceptable(attribute, value):
                            counts[attribute][value] += 1

        # Categories: leaf and its parent are the product type. The root is
        # always "Clothing, Shoes & Jewelry" and carries no signal.
        categories = product.get("categories") or []
        if isinstance(categories, list) and len(categories) > 1:
            for node in categories[1:][-2:]:
                for part in _split_values(node):
                    value = normalize(part)
                    if _acceptable("category", value):
                        counts["category"][value] += 1

        store = product.get("store")
        if store not in (None, "", []):
            value = normalize(store)
            if _acceptable("brand", value):
                counts["brand"][value] += 1

    return counts


def build(counts: dict[str, Counter], floors: dict[str, int]) -> dict:
    """Apply floors, expand variants, resolve cross-attribute clashes."""
    kept: dict[str, dict[str, int]] = {}
    for attribute, counter in counts.items():
        floor = floors.get(attribute, 5)
        kept[attribute] = {
            value: count for value, count in counter.items() if count >= floor
        }

    # One surface form cannot mean two attributes. When a string is evidenced
    # for several, the attribute with the most documents behind it wins --
    # otherwise "leather" arrives as both material and category and the
    # tagger's answer depends on dict iteration order.
    owner: dict[str, tuple[str, int]] = {}
    for attribute in sorted(kept):
        for value, count in kept[attribute].items():
            current = owner.get(value)
            if current is None or count > current[1]:
                owner[value] = (attribute, count)

    attributes: dict[str, dict] = {}
    for attribute in sorted(kept):
        entries = []
        for value, count in sorted(kept[attribute].items(), key=lambda item: (-item[1], item[0])):
            if owner[value][0] != attribute:
                continue
            surfaces = sorted(
                form
                for form in variants_of(value)
                # A generated variant that another attribute owns outright is
                # dropped: inventing "shoe" must not shadow a real entry.
                if owner.get(form, (attribute, 0))[0] == attribute
            )
            if not surfaces:
                continue
            entries.append({"canonical": value, "df": count, "surfaces": surfaces})
        if entries:
            attributes[attribute] = {"count": len(entries), "entries": entries}
    return attributes


def checksum(path: Path, chunk: int = 1 << 20) -> str:
    """SHA256 of a file, streamed."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    """Mine the catalog and write `data/lexicon.json`."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    for attribute, floor in sorted(DEFAULT_FLOORS.items()):
        parser.add_argument(f"--floor-{attribute}", type=int, default=floor)
    args = parser.parse_args(argv)

    floors = {
        attribute: getattr(args, f"floor_{attribute}") for attribute in DEFAULT_FLOORS
    }
    attributes = build(mine(args.catalog), floors)

    payload = {
        "lexicon_version": LEXICON_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "catalog": str(args.catalog),
        "catalog_sha256": checksum(args.catalog),
        "floors": floors,
        "attributes": attributes,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    total = sum(block["count"] for block in attributes.values())
    print(f"wrote {args.out} — {total} entries")
    for attribute in sorted(attributes):
        block = attributes[attribute]
        top = ", ".join(entry["canonical"] for entry in block["entries"][:6])
        print(f"  {attribute:<10}{block['count']:>6}  (floor {floors.get(attribute, 5)})  {top}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
