"""
Optional LLM extraction layer.

This is strictly additive. The deterministic extractor runs first and its
result is authoritative; the model may only contribute constraint spans the
rules missed. It can never delete a span, clear a slot, or change the
retrieval-timing decision.

That asymmetry is deliberate. `docs/submission_rules.md` warns that organizer
policy may disable network access for official scoring, so the offline path
has to stand on its own. Anything the model adds is upside, never a
dependency.

Enable with:

    export TECHJAM_LLM_EXTRACTOR=1
    export ANTHROPIC_API_KEY=...
"""

from __future__ import annotations

import json
import os
from collections import Counter
from typing import Any

from state.state_manager import ALLOWED_ATTRIBUTES, AttributeUpdate, ExtractedTurn


DEFAULT_MODEL = "claude-opus-5"

ENV_FLAG = "TECHJAM_LLM_EXTRACTOR"

MAX_TOKENS = 1024

# Hard limit on turns per session, from the evaluator. A message arriving at
# or past it cannot influence a score, so escalating there spends money for
# nothing.
MAX_TURNS = 10

# Ceiling on model calls for one process, as a cost guard independent of the
# gate. Override with TECHJAM_LLM_MAX_CALLS.
DEFAULT_MAX_CALLS = 250

SYSTEM_PROMPT = """You extract shopping constraints from one customer message.

Return every distinct requirement the customer states, copied VERBATIM from
the message. Do not paraphrase, normalize, expand abbreviations, or merge two
requirements into one string: the text is matched literally against a product
catalog, so an altered string is worthless.

Assign each requirement the attribute it best fits. Prefer "feature" when
unsure. Ignore pleasantries and anything that is not a product requirement."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "constraints": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "attribute": {
                        "type": "string",
                        "enum": sorted(ALLOWED_ATTRIBUTES),
                    },
                },
                "required": ["text", "attribute"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["constraints"],
    "additionalProperties": False,
}


def is_enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip().lower() in {"1", "true", "yes"}


class LLMTurnExtractor:
    """Wraps a deterministic extractor and augments its output."""

    def __init__(self, fallback: Any, model: str | None = None) -> None:
        self.fallback = fallback
        # Read at construction time: Agent loads .env immediately before it
        # constructs this wrapper.
        self.model = model or os.environ.get("TECHJAM_LLM_MODEL", DEFAULT_MODEL)
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self._client = self._build_client()
        try:
            self.max_calls = int(os.environ.get("TECHJAM_LLM_MAX_CALLS", DEFAULT_MAX_CALLS))
        except ValueError:
            self.max_calls = DEFAULT_MAX_CALLS
        # Escalation accounting, reported in the handover and available to
        # anyone tuning the gate on provenance data.
        self.calls = 0
        self.gate_counts: Counter = Counter()

    @property
    def active(self) -> bool:
        return self._client is not None

    def should_escalate(self, state: object | None = None) -> bool:
        """Structural gate -- deliberately not a confidence score.

        Escalate only when the deterministic cascade has structurally nothing
        to say: Tier 0 produced no operations, Tier 1 produced no operations,
        and the message matched no known template. Those three facts are
        observable in `HeuristicTurnExtractor.last_trace`, so the decision is
        auditable and reproducible.

        A learned confidence model was the alternative. On 200 labelled
        sessions it would overfit, and it would make the escalation rate --
        which is the cost line in the submission disclosure -- unpredictable.
        """

        trace = getattr(self.fallback, "last_trace", None)
        if not isinstance(trace, dict):
            # An extractor without the cascade cannot report structure, so the
            # gate cannot be evaluated. Do not spend a call on a guess.
            self.gate_counts["no_trace"] += 1
            return False

        if trace.get("tier0_operations") or trace.get("tier1_operations"):
            self.gate_counts["blocked_tiers_produced_output"] += 1
            return False
        if trace.get("template_matched"):
            # A template matched but yielded nothing: that is Tier 0 correctly
            # recognising "no preference for colour" and friends, not a gap.
            self.gate_counts["blocked_template_matched"] += 1
            return False
        if int(getattr(state, "turn", 0) or 0) >= MAX_TURNS:
            self.gate_counts["blocked_turn_budget"] += 1
            return False
        if self.calls >= self.max_calls:
            self.gate_counts["blocked_call_budget"] += 1
            return False

        self.gate_counts["escalated"] += 1
        return True

    def extract(self, user_message: str, state: object | None = None) -> ExtractedTurn:
        extracted = self.fallback.extract(user_message, state)

        if self._client is None:
            return extracted

        if not self.should_escalate(state):
            return extracted

        self.calls += 1
        try:
            constraints = self._call(user_message, state)
        except Exception:
            # Any failure -- no network, no credentials, bad JSON, rate limit
            # -- leaves the deterministic result untouched.
            return extracted

        known = {
            str(item.value).strip().lower()
            for item in extracted.operations
            if item.value
        }

        for constraint in constraints:
            text = str(constraint.get("text", "")).strip()
            attribute = str(constraint.get("attribute", "feature"))

            if not text or text.lower() in known:
                continue
            if attribute not in ALLOWED_ATTRIBUTES:
                attribute = "feature"
            if text not in user_message:
                # Guard against paraphrase: only verbatim spans are useful.
                continue

            extracted.operations.append(
                AttributeUpdate(
                    attribute=attribute,
                    action="set",
                    value=text,
                    raw_text=text,
                    provenance="tier2",
                    # The model is additive and non-authoritative, so its
                    # spans are soft evidence next to a matched template.
                    strength="soft",
                    confidence=0.5,
                )
            )
        if any(item.provenance == "tier2" for item in extracted.operations):
            extracted.provenance = "tier2"
            known.add(text.lower())

        return extracted

    def _build_client(self):
        if not is_enabled():
            return None
        try:
            import anthropic
        except ImportError:
            return None
        try:
            return anthropic.Anthropic(max_retries=1, timeout=10.0)
        except Exception:
            return None

    def _call(self, user_message: str, state: object | None = None) -> list[dict]:
        conversation = getattr(state, "messages", [])
        context = "\n".join(
            f"{item.get('role', 'user')}: {item.get('content', '')}"
            for item in conversation[-8:]
            if isinstance(item, dict)
        )
        content = user_message if not context else f"Conversation context (do not extract from it):\n{context}\n\nCurrent message:\n{user_message}"
        response = self._client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
            output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        )

        usage = getattr(response, "usage", None)
        if usage is not None:
            self.last_usage = {
                "prompt_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            }

        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        payload = json.loads(text)
        constraints = payload.get("constraints", [])
        return constraints if isinstance(constraints, list) else []
