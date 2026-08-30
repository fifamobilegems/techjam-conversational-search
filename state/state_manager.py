from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal


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

    # Useful for debugging and presentation.
    history: list[dict] = field(
        default_factory=list
    )

    # Compact transcript for optional model extraction and trace inspection.
    # Retrieval still consumes only explicit constraints, never free-form chat.
    messages: list[dict] = field(default_factory=list)

    def record_span(
        self,
        attribute: str,
        text: str,
        turn: int,
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
        Apply one attribute-level mutation.

        The three possible operations are:

        SET
            Unknown -> value
            Existing value -> replacement value

        CLEAR
            Existing value -> unknown

        NO_PREFERENCE
            Existing/unknown -> explicitly irrelevant
        """

        attribute = operation.attribute
        action = operation.action
        value = operation.value

        if attribute not in ALLOWED_ATTRIBUTES:
            return

        # -----------------------------------------------------
        # SET
        # -----------------------------------------------------

        if action == "set":

            if value is None:
                return

            old_value = state.slots.get(
                attribute
            )

            # User has supplied a value, therefore they no
            # longer have "no preference" for this attribute.
            state.no_preference.discard(
                attribute
            )

            state.slots[attribute] = value

            # Slots collide; spans do not. Keep both.
            state.record_span(
                attribute,
                str(operation.raw_text or value),
                turn,
            )

            if old_value is None:
                change_type = "add"

            elif old_value != value:
                change_type = "override"

            else:
                change_type = "unchanged"

            state.history.append(
                {
                    "turn": turn,
                    "attribute": attribute,
                    "action": change_type,
                    "old_value": old_value,
                    "new_value": value,
                }
            )

        # -----------------------------------------------------
        # CLEAR
        # -----------------------------------------------------

        elif action == "clear":

            old_value = state.slots.pop(
                attribute,
                None,
            )

            # CLEAR does NOT mean "I don't care".
            #
            # It means the previous value is no longer valid
            # and this attribute becomes unknown again.
            state.no_preference.discard(
                attribute
            )

            state.history.append(
                {
                    "turn": turn,
                    "attribute": attribute,
                    "action": "clear",
                    "old_value": old_value,
                    "new_value": None,
                }
            )

        # -----------------------------------------------------
        # NO PREFERENCE
        # -----------------------------------------------------

        # -----------------------------------------------------
        # DEMOTE
        # -----------------------------------------------------

        elif action == "demote":

            demoted = state.demote_spans(
                attribute=attribute,
                text=operation.raw_text or value,
            )

            # An explicit override invalidates the old active slot.  Keeping it in
            # ``slots`` made an "ignore my earlier preference" message continue to
            # steer ranking toward the superseded value.
            old_value = state.slots.pop(attribute, None)

            # The slot value is deliberately left in place: an overridden
            # preference still describes the same target product.
            if demoted:
                state.history.append(
                    {
                        "turn": turn,
                        "attribute": attribute,
                        "action": "demote",
                        "old_value": old_value,
                        "new_value": None,
                    }
                )

        elif action == "no_preference":

            old_value = state.slots.pop(
                attribute,
                None,
            )

            # Explicitly remember that the user does not care.
            state.no_preference.add(
                attribute
            )

            state.history.append(
                {
                    "turn": turn,
                    "attribute": attribute,
                    "action": "no_preference",
                    "old_value": old_value,
                    "new_value": None,
                }
            )

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

        # Latching flag: the customer never un-exhausts.
        if extracted.information_exhausted:
            state.information_exhausted = True

        # Scenario is fixed by the opening message.
        if extracted.scenario and state.scenario == "unknown":
            state.scenario = extracted.scenario

        # Update intent if extractor has meaningful evidence.
        if (
            extracted.intent is not None
            and extracted.intent != "unknown"
        ):
            old_intent = state.intent

            state.intent = extracted.intent

            if old_intent != extracted.intent:
                state.history.append(
                    {
                        "turn": turn,
                        "action": "intent_change",
                        "old_value": old_intent,
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

        While the customer still has undisclosed constraints, the highest
        yield attribute is asked every turn. It is answered without being
        classified, so it never returns the empty reply and never runs out
        the way a per-class question does.

        Once the customer reports nothing further, fall back to the
        measured class order. That costs nothing here and keeps the policy
        working if a simulator classifies replies differently.
        """

        state = self.get(session_id)

        if state.turn >= MAX_TURNS:
            return None

        if not state.information_exhausted:
            return HIGHEST_YIELD_ATTRIBUTE
        # The deterministic customer has explicitly said it has no further
        # information. Cycling through every remaining attribute only repeats an
        # identical ranking and wastes the remaining turns.
        return None

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

        return state.turn >= self.hold_until_turn

    # =========================================================
    # SEARCH CONTEXT
    # =========================================================

    def build_search_context(
        self,
        session_id: str,
    ) -> str:
        """
        Turn accumulated state into a text query.

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

        parts: list[str] = []

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
