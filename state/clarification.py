"""Catalog-driven clarification policy, independent of simulator exploits."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping


QUESTION_ATTRIBUTES = (
    "feature", "material", "color", "style", "size", "use_case", "category", "budget", "brand",
)


def _normalised_entropy(value_counts: Mapping[str, int | float]) -> float:
    total = sum(float(count) for count in value_counts.values() if float(count) > 0)
    if total <= 0 or len(value_counts) <= 1:
        return 0.0
    entropy = -sum(
        (float(count) / total) * math.log(float(count) / total)
        for count in value_counts.values() if float(count) > 0
    )
    return entropy / math.log(len(value_counts))


def _answerability_from_measurement(
    path: Path | None = None,
    mission: str | None = None,
) -> dict[str, float]:
    """Read measured answerability for the current mission, never guess it."""
    artifact = path or Path("docs/attribute_yield.json")
    try:
        measured = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    by_mission = measured.get("answerability_by_mission")
    if isinstance(by_mission, Mapping):
        mission_values = by_mission.get(mission or "", by_mission.get("default"))
        if isinstance(mission_values, Mapping):
            return {str(key): float(value) for key, value in mission_values.items()}

    explicit = measured.get("answerability_by_attribute")
    if isinstance(explicit, Mapping):
        return {str(key): float(value) for key, value in explicit.items()}
    yields = measured.get("yield_by_attribute")
    if not isinstance(yields, Mapping):
        return {}
    maximum = max((float(value) for key, value in yields.items() if key != "other"), default=0.0)
    return {str(key): float(value) / maximum for key, value in yields.items()
            if key != "other" and maximum > 0}


def question_value(attribute: str, *, attribute_stats: Mapping[str, Mapping[str, Any]],
                   asked_attributes: Iterable[str], no_preference: Iterable[str],
                   answerability: Mapping[str, float]) -> float:
    """0.45 reduction + .30 coverage + .15 measured answerability + .10 instability."""
    stats = attribute_stats.get(attribute, {})
    reduction = _normalised_entropy(stats.get("value_counts", {}))
    coverage = min(1.0, max(0.0, float(stats.get("coverage", 0.0))))
    answerable = min(1.0, max(0.0, float(answerability.get(attribute, 0.0))))
    # D publishes Shannon entropy in bits. Normalise it against the observed
    # value support; clipping raw bits at 1.0 would make most live attributes
    # indistinguishable and discard the diagnostic's useful signal.
    value_counts = stats.get("value_counts", {})
    support = len(value_counts) if isinstance(value_counts, Mapping) else 0
    instability_bits = max(0.0, float(stats.get("instability", 0.0)))
    instability = (
        min(1.0, instability_bits / math.log2(support))
        if support > 1
        else 0.0
    )
    penalty = float(attribute in set(asked_attributes)) + float(attribute in set(no_preference))
    return 0.45 * reduction + 0.30 * coverage + 0.15 * answerable + 0.10 * instability - penalty


def choose_question(*, attribute_stats: Mapping[str, Mapping[str, Any]],
                    asked_attributes: Iterable[str], no_preference: Iterable[str],
                    mission: str | None = None, measurement_path: Path | None = None,
                    minimum_value: float = 0.01) -> str | None:
    """Return the best non-repeated typed question, or no question."""
    answerability = _answerability_from_measurement(measurement_path, mission)
    declined = set(no_preference)
    candidates = [attribute for attribute in QUESTION_ATTRIBUTES if attribute not in declined]
    if not candidates:
        return None
    scored = {attribute: question_value(attribute, attribute_stats=attribute_stats,
                                        asked_attributes=asked_attributes, no_preference=declined,
                                        answerability=answerability)
              for attribute in candidates}
    best_attribute, best_value = max(scored.items(), key=lambda item: (item[1], item[0]))
    return best_attribute if best_value >= minimum_value else None
