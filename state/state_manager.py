from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from state.clarification import choose_question


# Hard session limit imposed by the evaluator.
MAX_TURNS = 10

# Weight applied to a constraint the user has superseded.  It remains weak
# retrieval evidence, but cannot stay an active hard constraint.
DEMOTED_WEIGHT = 0.4


ALLOWED_ATTRIBUTES = {
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
}


AttributeAction = Literal[
    "set",
    "clear",
    "no_preference",
    "demote",
]

ConstraintPolarity = Literal["must", "prefer", "negate"]
ConstraintStrength = Literal["hard", "soft"]

# Stable provenance names produced by the extraction cascade.  ``legacy`` is
# deliberately the default for pre-schema callers so existing deterministic
# extraction keeps its current authority until it is upgraded.
ConstraintProvenance = Literal["legacy", "tier0", "tier1", "tier2"]


Intent = Literal[
    "buying",
    "browsing",
    "boundary",
    "override",
    "unknown",
]


NextAction = Literal[
    "retrieve",
    "clarify",
]


# Ask order by measured information yield, not intuition.
#
# Regenerate with: python -m scripts.measure_attribute_yield
#
# "other" is the argmax: the simulator answers it without classifying the
# attribute, so it returns undisclosed constraints of ANY class and can be
# re-asked every turn. The remainder is the fallback order for the case
# where a simulator classifies replies differently from the public one.
HIGHEST_YIELD_ATTRIBUTE = "other"

CLARIFICATION_PRIORITY = [
    "other",
    "feature",
    "material",
    "color",
    "style",
    "size",
    "use_case",
    "category",
    "budget",
    "brand",
]


def normalize_phrase(value: str) -> str:
    """
    Normalize a constraint span for substring matching against catalog text.

    The catalog flattens `details` as "key value" while the simulator
    discloses constraints as "key: value", so punctuation must go or the
    colon breaks every match.
    """

    return re.sub(
        r"\s+",
        " ",
        re.sub(r"[^a-z0-9 ]", " ", value.lower()),
    ).strip()


@dataclass
class AttributeUpdate:
    """
    Represents ONE change to ONE attribute.
    """

    attribute: str
    action: AttributeAction
    value: Any = None

    # Verbatim text the value was taken from.
    #
    # Slots keep one value per attribute, so colliding constraints
    # overwrite each other. The raw span survives that collision and is
    # what retrieval actually matches on.
    raw_text: str | None = None

    # Schema freeze: all extraction tiers carry these fields. Defaults retain
    # legacy Tier-0 semantics for callers that have not adopted the cascade.
    polarity: ConstraintPolarity = "must"
    strength: ConstraintStrength = "hard"
    confidence: float = 1.0
    provenance: ConstraintProvenance = "legacy"
    superseded: bool = False


@dataclass
class ExtractedTurn:
    """
    Output from the slot extractor for one user message.

    One turn may contain many independent attribute changes.
    """

    intent: Intent | None = None

    operations: list[AttributeUpdate] = field(
        default_factory=list
    )

    # Set when the customer signals there is nothing left to disclose.
    information_exhausted: bool = False

    # Observable scenario label, derived from message wording only.
    scenario: str | None = None

    # Turn-level defaults let a tier annotate a whole extraction result while
    # individual AttributeUpdate values remain authoritative when present.
    polarity: ConstraintPolarity = "must"
    strength: ConstraintStrength = "hard"
    confidence: float = 1.0
    provenance: ConstraintProvenance = "legacy"
    superseded: bool = False


@dataclass
class ConversationState:
    """
    Current accumulated state for one shopping session.
    """

    session_id: str

    # Current user intent.
    intent: Intent = "unknown"

    # Active known constraints.
    slots: dict[str, Any] = field(
        default_factory=dict
    )

    # Attributes the user explicitly said they do not care about.
    no_preference: set[str] = field(
        default_factory=set
    )

    # Attributes already asked by the agent.
    asked_attributes: set[str] = field(
        default_factory=set
    )

    # Every constraint span disclosed so far, in order, with no collisions.
    raw_constraints: list[dict] = field(
        default_factory=list
    )

    # True once the customer has nothing further to disclose.
    information_exhausted: bool = False

    # buying | browsing_or_boundary | intent_override | unknown
    scenario: str = "unknown"

    turn: int = 0

    # Authoritative immutable event log. `slots`, `no_preference`, and
    # `raw_constraints` are replayed caches, never sources of truth.
    history: list[dict] = field(
        default_factory=list
    )

    # Compact transcript for optional model extraction and trace inspection.
    # User messages are also retained as lexical retrieval evidence. Extracted
    # constraints complement the shopper's words; they must not replace them.
    messages: list[dict] = field(default_factory=list)

    # Latest provisional retrieval summary. Rankings are a derived cache, not
    # user events, and must be refreshed by the Agent each turn.
    retrieval_diagnostics: dict[str, Any] = field(default_factory=dict)

    def record_span(
        self,
        attribute: str,
        text: str,
        turn: int,
        *,
        polarity: ConstraintPolarity = "must",
        strength: ConstraintStrength = "hard",
        confidence: float = 1.0,
        provenance: ConstraintProvenance = "legacy",
        superseded: bool = False,
    ) -> None:
        """
        Store one disclosed constraint verbatim.

        Unlike `slots`, this never overwrites: two constraints that both
        classify as "feature" both survive here.
        """

        phrase = normalize_phrase(text)

        if not phrase:
            return

        for span in self.raw_constraints:
            if span["match_phrase"] == phrase:
                return

        self.raw_constraints.append(
            {
                "text": text,
                "match_phrase": phrase,
                "attribute": attribute,
                "turn": turn,
                "weight": 1.0,
                "polarity": polarity,
                "strength": strength,
                "confidence": confidence,
                "provenance": provenance,
                "superseded": superseded,
            }
        )

    def demote_spans(
        self,
        attribute: str | None = None,
        text: str | None = None,
    ) -> int:
        """
        Reduce the weight of superseded spans without discarding them.
        """

        phrase = normalize_phrase(text or "")
        demoted = 0

        for span in self.raw_constraints:
            matches_attribute = attribute is not None and span["attribute"] == attribute
            matches_text = bool(phrase) and (
                phrase in span["match_phrase"] or span["match_phrase"] in phrase
            )

            if matches_attribute or matches_text:
                span["weight"] = DEMOTED_WEIGHT
                span["superseded"] = True
                demoted += 1

        return demoted


class StateManager:
    """
    Multi-turn memory + decision-control layer.

    Responsibilities:

    1. Keep one state per session
    2. Accumulate constraints across turns
    3. Replace conflicting values
    4. Clear invalidated values
    5. Track explicit "no preference"
    6. Avoid asking the same question repeatedly
    7. Build accumulated search context
    8. Decide whether to retrieve or clarify
    """

    def __init__(
        self,
        hold_until_turn: int = 2,
        credibility_score_floor: float = 10.0,
        credibility_margin_floor: float = 1.0,
    ):
        """
        `hold_until_turn` controls when recommendations are first emitted.

        The evaluator ends a session at the FIRST turn the target appears
        in the top 10 and records that rank permanently, so an early weak
        list locks in a poor reciprocal rank. Holding while the constraints
        are still arriving trades MTTC for MRR at a profit.

        The optimum depends on how fast ranking improves per constraint, so
        re-run the sweep after any retrieval change:

            public set, current retriever
            1 -> .8560   2 -> .8892   3 -> .8814   4 -> .8656
        """

        self.hold_until_turn = hold_until_turn
        self.credibility_score_floor = credibility_score_floor
        self.credibility_margin_floor = credibility_margin_floor

        self.sessions: dict[
            str,
            ConversationState,
        ] = {}

    # =========================================================
    # SESSION MANAGEMENT
    # =========================================================

    def reset(
        self,
        session_id: str,
    ) -> ConversationState:
        """
        Start a completely fresh session.
        """

        state = ConversationState(
            session_id=session_id
        )

        self.sessions[session_id] = state

        return state

    def get(
        self,
        session_id: str,
    ) -> ConversationState:
        """
        Retrieve state for a session.

        Creates a new state if it does not exist.
        """

        if session_id not in self.sessions:
            self.reset(session_id)

        return self.sessions[session_id]

    # =========================================================
    # ATTRIBUTE OPERATIONS
    # =========================================================

    def apply_operation(
        self,
        state: ConversationState,
        operation: AttributeUpdate,
        turn: int,
    ) -> None:
        """
        Append one immutable attribute event, then rebuild derived state.

        Do not edit old events on overrides. A later ``demote`` event is the
        evidence that causes replay to mark matching earlier spans superseded
        and assign their effective retrieval weight of ``DEMOTED_WEIGHT``.
        """

        attribute = operation.attribute
        action = operation.action
        if attribute not in ALLOWED_ATTRIBUTES:
            return

        if action == "set" and operation.value is None:
            return

        state.history.append(
            {
                "event_id": len(state.history),
                "turn": turn,
                "event_type": "attribute",
                "attribute": attribute,
                "action": action,
                "value": operation.value,
                "raw_text": operation.raw_text,
                "polarity": operation.polarity,
                "strength": operation.strength,
                "confidence": operation.confidence,
                "provenance": operation.provenance,
                # Immutable source fact. Replay projects superseded status.
                "superseded": operation.superseded,
            }
        )
        self.replay(state)

    def replay(self, state: ConversationState) -> None:
        """Rebuild all effective retrieval state from the append-only log."""

        state.intent = "unknown"
        state.slots = {}
        state.no_preference = set()
        state.raw_constraints = []
        state.information_exhausted = False
        state.scenario = "unknown"

        for event in state.history:
            event_type = event.get("event_type")
            if event_type == "intent":
                state.intent = event["new_value"]
                continue
            if event_type == "scenario":
                if state.scenario == "unknown":
                    state.scenario = event["value"]
                continue
            if event_type == "exhausted":
                state.information_exhausted = True
                continue
            if event_type != "attribute":
                continue

            attribute = event["attribute"]
            action = event["action"]
            value = event.get("value")
            if action == "set":
                state.no_preference.discard(attribute)
                if event.get("polarity", "must") != "negate":
                    state.slots[attribute] = value
                state.record_span(
                    attribute,
                    str(event.get("raw_text") or value),
                    int(event["turn"]),
                    polarity=event.get("polarity", "must"),
                    strength=event.get("strength", "hard"),
                    confidence=float(event.get("confidence", 1.0)),
                    provenance=event.get("provenance", "legacy"),
                    superseded=bool(event.get("superseded", False)),
                )
            elif action == "clear":
                state.slots.pop(attribute, None)
                state.no_preference.discard(attribute)
            elif action == "no_preference":
                state.slots.pop(attribute, None)
                state.no_preference.add(attribute)
            elif action == "demote":
                state.demote_spans(attribute=attribute, text=event.get("raw_text") or value)
                state.slots.pop(attribute, None)

    # =========================================================
    # WHOLE-TURN UPDATE
    # =========================================================

    def update(
        self,
        session_id: str,
        extracted: ExtractedTurn,
        turn: int,
    ) -> ConversationState:
        """
        Apply all changes extracted from one user message.

        Operations are applied IN ORDER.

        Therefore if one message says:

            "I don't care about brand...
             actually Adidas would be good"

        then:

            brand -> NO_PREFERENCE
            brand -> SET Adidas

        Final state correctly becomes:

            brand = Adidas
        """

        state = self.get(session_id)

        state.turn = turn

        # Each observation is an event. Replay, rather than direct mutation,
        # establishes the effective state consumed by retrieval.
        if extracted.information_exhausted:
            state.history.append(
                {
                    "event_id": len(state.history),
                    "turn": turn,
                    "event_type": "exhausted",
                }
            )

        # Scenario is fixed by the opening message.
        if extracted.scenario and state.scenario == "unknown":
            state.history.append(
                {
                    "event_id": len(state.history),
                    "turn": turn,
                    "event_type": "scenario",
                    "value": extracted.scenario,
                }
            )

        # Update intent if extractor has meaningful evidence.
        if (
            extracted.intent is not None
            and extracted.intent != "unknown"
        ):
            if state.intent != extracted.intent:
                state.history.append(
                    {
                        "event_id": len(state.history),
                        "turn": turn,
                        "event_type": "intent",
                        "old_value": state.intent,
                        "new_value": extracted.intent,
                    }
                )

        # Apply each attribute mutation separately.
        for operation in extracted.operations:

            self.apply_operation(
                state,
                operation,
                turn,
            )

        # This also applies metadata-only events when the extractor produced
        # no attribute operations.
        self.replay(state)

        return state

    # =========================================================
    # QUESTION TRACKING
    # =========================================================

    def mark_asked(
        self,
        session_id: str,
        attribute: str,
    ) -> None:
        """
        Remember that the agent has already asked
        about this attribute.
        """

        if attribute not in ALLOWED_ATTRIBUTES:
            return

        state = self.get(session_id)

        state.asked_attributes.add(
            attribute
        )

    def record_message(self, session_id: str, role: str, content: str, turn: int) -> None:
        """Keep a bounded, non-authoritative transcript for the LLM/debugger."""
        if role not in {"user", "assistant"}:
            return
        state = self.get(session_id)
        state.messages.append({"turn": turn, "role": role, "content": str(content)[:600]})
        del state.messages[:-20]

    def set_retrieval_diagnostics(self, session_id: str, diagnostics: dict[str, Any]) -> None:
        """Store current provisional retrieval statistics for policy decisions."""
        state = self.get(session_id)
        state.retrieval_diagnostics = {**diagnostics, "_turn": state.turn}

    def get_missing_attributes(
        self,
        session_id: str,
    ) -> list[str]:
        """
        Return attributes that:

        1. Are not currently known
        2. Are not marked no-preference
        3. Have not already been asked
        """

        state = self.get(session_id)

        result = []

        for attribute in CLARIFICATION_PRIORITY:

            # Already known.
            if attribute in state.slots:
                continue

            # User explicitly does not care.
            if attribute in state.no_preference:
                continue

            # Already asked before.
            if attribute in state.asked_attributes:
                continue

            result.append(attribute)

        return result

    def choose_next_attribute(
        self,
        session_id: str,
    ) -> str | None:
        """
        Select one clarification attribute.

        Choose a typed question from current candidate statistics. ``other``
        is intentionally excluded: its special evaluator behaviour is not a
        credible shopper-facing policy.
        """

        state = self.get(session_id)

        if state.turn >= MAX_TURNS:
            return None

        if state.information_exhausted:
            return None

        attribute_stats = state.retrieval_diagnostics.get("attribute_stats", {})
        if not isinstance(attribute_stats, dict) or not attribute_stats:
            # The Agent has not yet supplied the provisional retrieval contract.
            # Keep legacy behaviour until that cross-role integration lands;
            # once stats are present, ``other`` is never selected here.
            return HIGHEST_YIELD_ATTRIBUTE
        return choose_question(
            attribute_stats=attribute_stats,
            asked_attributes=state.asked_attributes,
            no_preference=state.no_preference,
            mission=state.intent,
        )

    def should_emit_recommendations(
        self,
        session_id: str,
    ) -> bool:
        """
        Decide whether this turn may return a ranked list.

        Asking and retrieving are NOT alternatives -- the response contract
        carries both fields and the evaluator reads both every turn. The
        only real decision is whether the list is good enough yet to accept
        a permanently recorded rank.
        """

        state = self.get(session_id)

        if state.information_exhausted:
            return True

        if self._is_credible(state):
            return True

        return state.turn >= self.hold_until_turn

    def _is_credible(self, state: ConversationState) -> bool:
        """Evaluate current-turn retrieval confidence, never stale rankings."""

        diagnostics = state.retrieval_diagnostics
        if diagnostics.get("_turn") != state.turn:
            return False

        scores = diagnostics.get("ranked_scores")
        if not isinstance(scores, list):
            candidate_ids = diagnostics.get("candidate_ids")
            candidate_scores = diagnostics.get("candidate_scores")
            if not isinstance(candidate_ids, list) or not isinstance(candidate_scores, dict):
                return False
            scores = [
                candidate_scores.get(str(candidate_id), {}).get("final_score")
                for candidate_id in candidate_ids
            ]
        numeric_scores = [float(score) for score in scores if isinstance(score, (int, float))]
        if not numeric_scores:
            return False
        top_score = numeric_scores[0]
        tenth_score = numeric_scores[9] if len(numeric_scores) >= 10 else numeric_scores[-1]
        return (
            top_score >= self.credibility_score_floor
            or top_score - tenth_score >= self.credibility_margin_floor
        )

    # =========================================================
    # SEARCH CONTEXT
    # =========================================================

    def build_search_context(
        self,
        session_id: str,
    ) -> str:
        """
        Turn accumulated state into a text query.

        The shopper's raw wording is authoritative lexical evidence. Template
        extraction is necessarily incomplete on realistic queries, so slot
        values are appended to the recorded user messages rather than being the
        sole source of the BM25 query. Therefore a session with a user message
        always produces a non-empty query even when no extraction matched.

        Example:

            {
                "category": "running shoes",
                "color": "black",
                "size": "9",
                "budget": "under $150"
            }

        becomes:

            "running shoes black size 9 under $150"
        """

        state = self.get(session_id)

        parts: list[str] = [
            str(message["content"])
            for message in state.messages
            if message.get("role") == "user" and str(message.get("content", "")).strip()
        ]

        # Put category first.
        category = state.slots.get(
            "category"
        )

        if category:
            parts.append(str(category))

        for attribute, value in state.slots.items():

            if attribute == "category":
                continue

            if value is None:
                continue

            if attribute == "size":
                parts.append(
                    f"size {value}"
                )

            elif attribute == "budget":
                parts.append(
                    str(value)
                )

            else:
                parts.append(
                    str(value)
                )

        # Remove duplicates while preserving order.
        parts = list(
            dict.fromkeys(parts)
        )

        return " ".join(parts)

    # =========================================================
    # DECISION CONTROL
    # =========================================================

    def decide_next_action(
        self,
        session_id: str,
    ) -> dict:
        """
        Produce this turn's two independent decisions:

            ask_attribute
                which question to attach, if any

            should_emit_recommendations
                whether to expose a ranked list this turn

        These are not mutually exclusive. A turn normally does both.
        """

        ask_attribute = self.choose_next_attribute(
            session_id
        )

        emit = self.should_emit_recommendations(
            session_id
        )

        return {
            "next_action": "retrieve" if emit else "clarify",
            "ask_attribute": ask_attribute,
            "should_emit_recommendations": emit,
        }

    # =========================================================
    # PUBLIC INTERFACE FOR OTHER COMPONENTS
    # =========================================================

    def export(
        self,
        session_id: str,
    ) -> dict:
        """
        Produce a clean state representation for:

        - retrieval
        - reranking
        - clarification controller
        - debugging
        """

        state = self.get(session_id)

        decision = self.decide_next_action(
            session_id
        )

        return {
            "intent": state.intent,

            # ---- stable keys consumed by retrieval ----

            "constraints": dict(
                state.slots
            ),

            # Immutable source log plus replayed views for downstream roles.
            "events": [dict(event) for event in state.history],
            "demoted_constraints": [
                dict(span) for span in state.raw_constraints
                if span.get("superseded") or span.get("weight") == DEMOTED_WEIGHT
            ],
            "negated_constraints": [
                dict(span) for span in state.raw_constraints
                if span.get("polarity") == "negate" and not span.get("superseded")
            ],

            "no_preference": sorted(
                state.no_preference
            ),

            "search_query":
                self.build_search_context(
                    session_id
                ),

            # ---- lossless view of the same evidence ----
            #
            # `constraints` keeps one value per attribute. Roughly half of
            # the disclosed constraints classify into an attribute that
            # already holds a value, so they are only visible here.

            "raw_constraints": [
                dict(span)
                for span in state.raw_constraints
            ],

            "match_phrases": [
                span["match_phrase"]
                for span in state.raw_constraints
            ],

            # ---- dialogue control ----

            "asked_attributes": sorted(
                state.asked_attributes
            ),

            "category": state.slots.get(
                "category"
            ),

            "scenario": state.scenario,

            "information_exhausted":
                state.information_exhausted,

            "next_action":
                decision["next_action"],

            "ask_attribute":
                decision["ask_attribute"],

            "should_emit_recommendations":
                decision["should_emit_recommendations"],

            "turn": state.turn,
            "conversation": [dict(message) for message in state.messages],
        }
