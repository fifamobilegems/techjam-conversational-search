from __future__ import annotations

import re
from typing import Iterable

from state.state_manager import AttributeUpdate, ExtractedTurn


MATERIALS = {
    "cotton",
    "polyester",
    "nylon",
    "leather",
    "wool",
    "spandex",
    "silk",
    "rayon",
    "fabric",
}

COLORS = {
    "black",
    "white",
    "blue",
    "red",
    "pink",
    "green",
    "brown",
    "gray",
    "grey",
    "purple",
    "yellow",
    "orange",
}

ATTRIBUTE_WORDS = (
    "category",
    "material",
    "color",
    "size",
    "style",
    "brand",
    "budget",
    "feature",
    "use_case",
    "other",
)

TOKEN_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)

USE_CASE_WORDS = {
    "hiking",
    "running",
    "gym",
    "winter",
    "outdoor",
    "work",
    "trail",
    "walking",
    "yoga",
    "sports",
}

CATEGORY_FILLER_WORDS = {
    "a",
    "an",
    "some",
    "new",
    "pair",
    "pairs",
    "of",
    "for",
    "me",
}


def _clean(value: str, limit: int = 180) -> str:
    value = re.sub(r"\s+", " ", value).strip(" -;,.\t\n")
    return value[:limit].rstrip()


def _split_constraints(value: str) -> list[str]:
    value = _clean(value)
    if not value:
        return []
    parts = re.split(r"\s*;\s*", value)
    if len(parts) == 1:
        parts = re.split(r"\s+\band\b\s+", value, flags=re.IGNORECASE)
    return [_clean(part) for part in parts if _clean(part)]


def _first_word_match(words: Iterable[str], text: str) -> str | None:
    lowered = text.lower()
    for word in sorted(words):
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            return word
    return None


def _category_from_phrase(value: str) -> str | None:
    category = re.sub(
        r"\b(?:under|below|less than|around|about)\s+\$?\d+(?:\.\d{1,2})?\b",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    category = re.sub(r"\$\s*\d+(?:\.\d{1,2})?", " ", category)
    for word in sorted(COLORS | MATERIALS):
        category = re.sub(rf"\b{re.escape(word)}\b", " ", category, flags=re.IGNORECASE)
    category = _clean(category)
    tokens = [
        token
        for token in category.split()
        if token.lower() not in CATEGORY_FILLER_WORDS
    ]
    category = _clean(" ".join(tokens))
    return category or None


def _state_slots(state: object | None) -> dict:
    slots = getattr(state, "slots", None)
    return slots if isinstance(slots, dict) else {}


def _last_non_category_attribute(state: object | None) -> str | None:
    history = getattr(state, "history", None)
    if not isinstance(history, list):
        return None
    for item in reversed(history):
        if not isinstance(item, dict):
            continue
        attribute = item.get("attribute")
        if isinstance(attribute, str) and attribute in ATTRIBUTE_WORDS and attribute != "category":
            return attribute
    return None


def classify_constraint(value: str) -> str:
    lowered = value.lower()
    if "budget" in lowered or re.search(r"(?:\$|<=|<|under|below|less than|around)\s*\d", lowered):
        return "budget"
    if _first_word_match(MATERIALS, lowered):
        return "material"
    if "color" in lowered or _first_word_match(COLORS, lowered):
        return "color"
    if any(word in lowered for word in ("size", "sizing", "width", "wide", "narrow", "petite", "plus")):
        return "size"
    if any(word in lowered for word in ("brand", "store", "manufacturer", "made by")):
        return "brand"
    if any(word in lowered for word in ("hiking", "running", "gym", "winter", "outdoor", "work", "trail")):
        return "use_case"
    if any(word in lowered for word in ("department", "style", "fit", "sleeve", "neck", "casual", "formal")):
        return "style"
    return "feature"


class HeuristicTurnExtractor:
    """Small deterministic fallback for the competition simulator phrasing."""

    def extract(self, user_message: str, state: object | None = None) -> ExtractedTurn:
        lowered = user_message.lower()
        operations: list[AttributeUpdate] = []
        seen: set[tuple[str, str, str]] = set()
        slots = _state_slots(state)
        override_requested = any(
            phrase in lowered
            for phrase in ("actually", "instead", "ignore earlier", "ignore my earlier")
        )

        def add_operation(attribute: str, action: str, value: str | None = None) -> None:
            cleaned = _clean(value or "") if value is not None else None
            key = (attribute, action, cleaned or "")
            if key in seen:
                return
            seen.add(key)
            operations.append(AttributeUpdate(attribute=attribute, action=action, value=cleaned))

        def add_set(attribute: str, value: str) -> None:
            cleaned = _clean(value)
            if (
                override_requested
                and attribute != "category"
                and attribute in slots
                and str(slots[attribute]) != cleaned
            ):
                add_operation(attribute, "clear")
            add_operation(attribute, "set", cleaned)

        def has_set(attribute: str) -> bool:
            return any(item.attribute == attribute and item.action == "set" for item in operations)

        intent = None
        if override_requested:
            intent = "override"
        elif any(phrase in lowered for phrase in ("still exploring", "exploring", "browsing")):
            intent = "browsing"
        elif any(phrase in lowered for phrase in ("key requirement", "what i need", "i need", "must-have")):
            intent = "buying"
        elif "looking for" in lowered:
            intent = "buying"

        if re.search(r"\bignore\s+(?:my\s+)?(?:earlier|previous)\s+preference\b", lowered):
            attribute = _last_non_category_attribute(state)
            if attribute:
                add_operation(attribute, "clear")

        for pattern in (
            r"\bignore\s+([^;,.]+)",
            r"\binstead\s+of\s+([^;,.]+)",
            r"\bno\s+longer\s+(?:need|want)\s+([^;,.]+)",
        ):
            for match in re.finditer(pattern, user_message, flags=re.IGNORECASE):
                ignored = _clean(match.group(1))
                if re.search(r"\b(?:my\s+)?(?:earlier|previous)\s+preference\b", ignored, flags=re.I):
                    continue
                add_operation(classify_constraint(ignored), "clear")

        clear_match = re.search(
            r"\b(?:clear|remove|drop)\s+(category|material|color|size|style|brand|budget|feature|use_case|other)\b",
            lowered,
        )
        if clear_match:
            add_operation(clear_match.group(1), "clear")

        for pattern in (
            r"\b(?:no preference|no pref)\s+(?:for|on|about)\s+([a-z_ ]+)",
            r"\b(?:do not|don't|dont)\s+have\s+(?:an?\s+)?(?:additional\s+)?preference\s+for\s+([a-z_ ]+)",
            r"\b(?:do not|don't|dont)\s+care\s+(?:about|for)\s+([a-z_ ]+)",
        ):
            no_pref_match = re.search(pattern, lowered)
            if no_pref_match:
                attribute = no_pref_match.group(1).strip().replace(" ", "_")
                if attribute in ATTRIBUTE_WORDS:
                    intent = intent or "boundary"
                    add_operation(attribute, "no_preference")

        category_match = re.search(
            r"\b(?:looking for|shopping for|searching for)\s+(?:(?:a|an|some)\s+)?(.*?)(?:, but|\.|;|$)",
            user_message,
            flags=re.IGNORECASE,
        )
        if category_match and not lowered.startswith("for that"):
            category = _clean(category_match.group(1))
            if category and category.lower() not in {"that", "it", "this"}:
                add_set("category", category)

        constraint_spans: list[str] = []
        for pattern in (
            r"\bkey requirement is:?\s*(.+)$",
            r"\bwhat i need is:?\s*(.+)$",
            r"\bwhat matters is:?\s*(.+)$",
        ):
            match = re.search(pattern, user_message, flags=re.IGNORECASE)
            if match:
                constraint_spans.append(match.group(1))

        # Intent-override opening messages look like:
        # "I'm looking for <category>. <old preference>"
        if "." in user_message and "key requirement" not in lowered and "what matters is" not in lowered:
            tail = _clean(user_message.split(".", 1)[1])
            if tail and not tail.lower().startswith(("ask me", "those options", "what i need")):
                constraint_spans.append(tail)

        for span in constraint_spans:
            for value in _split_constraints(span):
                add_set(classify_constraint(value), value)

        direct_need_spans: list[str] = []
        if not constraint_spans:
            for pattern in (
                r"\b(?:i\s+need|i\s+want|need|want)\s+(?!is\b)([^.;]+)",
                r"\b(?:exploring|browsing)\s+(?:for\s+)?(?:(?:a|an|some)\s+)?([^.;]+)",
            ):
                for match in re.finditer(pattern, user_message, flags=re.IGNORECASE):
                    value = _clean(match.group(1))
                    if value and not value.lower().startswith(("to ", "your ")):
                        direct_need_spans.append(value)

        for value in direct_need_spans:
            color = _first_word_match(COLORS, value)
            if color:
                add_set("color", color)

            material = _first_word_match(MATERIALS, value)
            if material:
                add_set("material", material)

            use_case = _first_word_match(USE_CASE_WORDS, value)
            if use_case:
                add_set("use_case", use_case)

            category = _category_from_phrase(value)
            if category:
                add_set("category", category)

        if not constraint_spans:
            budget_match = re.search(
                r"\b(?:under|below|less than|around|about|budget(?: is)?|<=|<)?\s*\$?\s*(\d+(?:\.\d{1,2})?)",
                user_message,
                flags=re.IGNORECASE,
            )
            if budget_match and any(
                marker in lowered
                for marker in ("$", "budget", "under", "below", "less than", "around")
            ):
                add_set("budget", _clean(budget_match.group(0)))

            color = _first_word_match(COLORS, user_message)
            if color and not has_set("color"):
                add_set("color", color)

            material = _first_word_match(MATERIALS, user_message)
            if material and not has_set("material"):
                add_set("material", material)

        return ExtractedTurn(intent=intent, operations=operations)
