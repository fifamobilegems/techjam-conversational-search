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
    export OPENAI_API_KEY=...

The client speaks the OpenAI Chat Completions protocol, so it works against
OpenAI directly or against any compatible gateway. For OpenRouter also set:

    export OPENAI_BASE_URL=https://openrouter.ai/api/v1
    export TECHJAM_LLM_MODEL=openai/gpt-4o-mini   # gateway ids are namespaced
"""

from __future__ import annotations

import json
import os
from collections import Counter
from typing import Any

from state.state_manager import ALLOWED_ATTRIBUTES, AttributeUpdate, ExtractedTurn


# Chat-completions model. Override with TECHJAM_LLM_MODEL. Note that gateways
# namespace their ids ("openai/gpt-4o-mini" on OpenRouter) and that the bare
# form 404s there -- which this module swallows silently, so check token usage
# rather than assuming a clean run means the tier fired.
DEFAULT_MODEL = "gpt-4o-mini"

ENV_FLAG = "TECHJAM_LLM_EXTRACTOR"

MAX_TOKENS = 1024

# Hard limit on turns per session, from the evaluator. A message arriving at
# or past it cannot influence a score, so escalating there spends money for
# nothing.
MAX_TURNS = 10

# Ceiling on model calls for one process, as a cost guard independent of the
# gate. Override with TECHJAM_LLM_MAX_CALLS.
DEFAULT_MAX_CALLS = 250

# --- escalation gate modes -------------------------------------------------
#
# `empty` is the Phase 8 gate: escalate only where the deterministic cascade
# is structurally silent. `low_confidence` adds one more opening -- a Tier 1
# gazetteer hit that explained only a fraction of the message.
#
# The distinction that keeps this auditable is that both modes are still
# *structural*. Neither reads a learned confidence score. `low_confidence`
# asks a second observable question ("how many content words did the cascade
# leave unaccounted for?") rather than a modelled one, so the escalation rate
# stays predictable and reproducible -- which is what the cost disclosure
# needs.
GATE_EMPTY = "empty"
GATE_LOW_CONFIDENCE = "low_confidence"
GATE_MODES = (GATE_EMPTY, GATE_LOW_CONFIDENCE)

# Escalate a Tier 1 turn only when the cascade explained at most this fraction
# of the message's content words.
DEFAULT_GATE_COVERAGE = 0.5

# ...and only when at least this many content words are left unexplained. One
# stray adjective is not worth a call; it is usually a word with no catalog
# meaning rather than a missed requirement.
DEFAULT_GATE_RESIDUAL = 2


def gate_mode() -> str:
    """Which escalation gate is active. Unknown names fall back to `empty`."""
    value = os.environ.get("TECHJAM_LLM_GATE", GATE_EMPTY).strip().lower()
    return value if value in GATE_MODES else GATE_EMPTY


def _gate_float(name: str, default: float) -> float:
    """Read a float threshold from the environment, ignoring unusable values."""
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _gate_int(name: str, default: int) -> int:
    """Read an integer threshold from the environment, ignoring unusable values."""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

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
    """Tier 2 is part of the shipped configuration, so this defaults ON.

    Degradation stays total and silent: without `OPENAI_API_KEY`, without the
    `openai` package, or without network, `_build_client()` returns None and
    the deterministic cascade is the entire agent. Verified in a real run --
    two bench cells lost network mid-matrix and scored identically to the
    deterministic baseline. Set `TECHJAM_LLM_EXTRACTOR=0` to disable it.
    """
    return os.environ.get(ENV_FLAG, "1").strip().lower() in {"1", "true", "yes"}


class LLMTurnExtractor:
    """Wraps a deterministic extractor and augments its output."""

    def __init__(self, fallback: Any, model: str | None = None) -> None:
        """Wrap a deterministic extractor and prepare the optional model client."""
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
        self.gate = gate_mode()
        self.gate_coverage = _gate_float("TECHJAM_LLM_GATE_COVERAGE", DEFAULT_GATE_COVERAGE)
        self.gate_residual = _gate_int("TECHJAM_LLM_GATE_RESIDUAL", DEFAULT_GATE_RESIDUAL)
        # Escalation accounting: how often the gate fired and why it did not.
        # This is the cost line for the token-usage disclosure.
        self.calls = 0
        self.gate_counts: Counter = Counter()

    @property
    def active(self) -> bool:
        """True when a client was constructed -- not proof that a call will succeed."""
        return self._client is not None

    def should_escalate(self, state: object | None = None) -> bool:
        """Structural gate -- deliberately not a learned confidence score.

        Two openings, both observable in `HeuristicTurnExtractor.last_trace`
        and so both auditable and reproducible:

        `empty`
            The Phase 8 gate. Tier 0 produced no operations, Tier 1 produced no
            operations, and the message matched no known template -- the
            cascade has structurally nothing to say.

        `low_confidence`
            Adds Tier 1 turns whose gazetteer hit explained only a fraction of
            the message. A single-word colour match on a two-clause sentence
            is *an* extraction, not a complete reading of it, and the original
            gate could not tell those apart: it only asked whether the cascade
            emitted anything at all.

        Tier 0 still blocks unconditionally in both modes. Template phrasing is
        the property worth 0.84 on the official column, and a turn a template
        already read is not a turn with a gap in it.

        A learned confidence model remains the road not taken. On 200 labelled
        sessions it would overfit, and it would make the escalation rate --
        which is the cost line in the submission disclosure -- unpredictable.
        Residual coverage is a count, not a prediction: the same message always
        produces the same decision, and the rate can be measured offline
        without spending a call (`python3 -m scripts.measure_gate`).
        """

        trace = getattr(self.fallback, "last_trace", None)
        if not isinstance(trace, dict):
            # An extractor without the cascade cannot report structure, so the
            # gate cannot be evaluated. Do not spend a call on a guess.
            self.gate_counts["no_trace"] += 1
            return False

        if trace.get("tier0_operations"):
            self.gate_counts["blocked_tier0"] += 1
            return False
        if trace.get("template_matched"):
            # A template matched but yielded nothing: that is Tier 0 correctly
            # recognising "no preference for colour" and friends, not a gap.
            self.gate_counts["blocked_template_matched"] += 1
            return False

        reason = self._opening(trace)
        if reason is None:
            return False

        # Budget checks come last so the counters separate "the gate declined"
        # from "the gate fired and the budget refused it". Ordered the other
        # way the two are indistinguishable after a run.
        if int(getattr(state, "turn", 0) or 0) >= MAX_TURNS:
            self.gate_counts["blocked_turn_budget"] += 1
            return False
        if self.calls >= self.max_calls:
            self.gate_counts["blocked_call_budget"] += 1
            return False

        self.gate_counts[reason] += 1
        self.gate_counts["escalated"] += 1
        return True

    def _opening(self, trace: dict) -> str | None:
        """Name the opening this turn qualifies for, or None, recording why not."""

        if not trace.get("tier1_operations"):
            return "escalated_empty"

        if self.gate != GATE_LOW_CONFIDENCE:
            self.gate_counts["blocked_tiers_produced_output"] += 1
            return None

        # Both conditions are needed. Coverage alone escalates a two-word
        # message where one word was matched (coverage 0.5, one residual
        # token), which is a complete reading of a short request rather than a
        # gap. The residual floor is what distinguishes them.
        if int(trace.get("residual_tokens", 0)) < self.gate_residual:
            self.gate_counts["blocked_residual_floor"] += 1
            return None
        if float(trace.get("coverage", 1.0)) > self.gate_coverage:
            self.gate_counts["blocked_coverage"] += 1
            return None

        return "escalated_low_confidence"

    def extract(self, user_message: str, state: object | None = None) -> ExtractedTurn:
        # Usage is per-turn, not cumulative. The Agent reports this dict on
        # every turn and the evaluator sums across turns, so leaving the last
        # call's numbers in place re-reported one escalation on every
        # subsequent turn -- inflating the required token disclosure by
        # roughly the number of turns that followed each call.
        """Run the deterministic extractor, then optionally augment it.

        Strictly additive: the model may only add constraint spans the rules
        missed. It can never delete a span, clear a slot, or change retrieval
        timing, so the offline score never depends on it.
        """
        self.last_usage = {"prompt_tokens": 0, "completion_tokens": 0}

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
            # Must stay inside the loop: hoisted out, the `text in known`
            # guard never saw earlier insertions, so a response repeating one
            # span appended it twice and double-counted it as evidence.
            known.add(text.lower())

        if any(item.provenance == "tier2" for item in extracted.operations):
            extracted.provenance = "tier2"

        return extracted

    def _build_client(self):
        """Construct the model client, or None if unavailable.

        A missing package or missing credentials returns None rather than
        raising, which keeps the deterministic path dependency-free.
        """
        if not is_enabled():
            return None
        try:
            import openai
        except ImportError:
            # Keeps the scored path dependency-free: no package, no tier, no
            # error. The deterministic extractor still runs.
            return None
        try:
            # The SDK reads OPENAI_API_KEY and OPENAI_BASE_URL from the
            # environment, so a compatible gateway needs no code change.
            #
            # Accept-Encoding excludes zstd deliberately. The SDK's vendored
            # httpx2 calls ZstdDecompressor.decompress(output_buffer_limit=...),
            # which no released backports.zstd accepts, so any zstd-encoded
            # response dies as an opaque "Connection error". Asking for gzip
            # sidesteps the whole incompatibility at negligible bandwidth cost.
            return openai.OpenAI(
                max_retries=1,
                timeout=10.0,
                default_headers={"Accept-Encoding": "gzip, deflate"},
            )
        except Exception:
            return None

    def _call(self, user_message: str, state: object | None = None) -> list[dict]:
        """Ask the model for constraint spans and return them as raw dicts."""
        conversation = getattr(state, "messages", [])
        context = "\n".join(
            f"{item.get('role', 'user')}: {item.get('content', '')}"
            for item in conversation[-8:]
            if isinstance(item, dict)
        )
        content = user_message if not context else f"Conversation context (do not extract from it):\n{context}\n\nCurrent message:\n{user_message}"
        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            # OUTPUT_SCHEMA already satisfies strict mode: additionalProperties
            # is false on every object and every property is required.
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "constraints",
                    "strict": True,
                    "schema": OUTPUT_SCHEMA,
                },
            },
        )

        usage = getattr(response, "usage", None)
        if usage is not None:
            self.last_usage = {
                "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            }

        text = response.choices[0].message.content or ""
        payload = json.loads(text)
        constraints = payload.get("constraints", [])
        return constraints if isinstance(constraints, list) else []
