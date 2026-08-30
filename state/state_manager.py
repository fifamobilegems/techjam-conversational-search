from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


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


# Initial heuristic, tune later based on dev-set results.
CLARIFICATION_PRIORITY = [
    "use_case",
    "size",
    "category",
    "material",
    "color",
    "feature",
    "budget",
    "brand",
    "style",
    "other",
]


@dataclass
class AttributeUpdate:
    """
    Represents ONE change to ONE attribute.
    """

    attribute: str
    action: AttributeAction
    value: Any = None


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

    turn: int = 0

    # Useful for debugging and presentation.
    history: list[dict] = field(
        default_factory=list
    )


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

    def __init__(self):
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
        """

        missing = self.get_missing_attributes(
            session_id
        )

        if not missing:
            return None

        return missing[0]

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
        Decide whether this turn should:

            retrieve

        or:

            clarify

        This is intentionally simple for the first version.
        """

        state = self.get(session_id)

        known_attributes = len(
            state.slots
        )

        # -----------------------------------------------------
        # Buying mode
        #
        # If the user already has concrete buying intent,
        # retrieve immediately instead of wasting turns.
        # -----------------------------------------------------

        if state.intent in {"buying", "override"}:

            return {
                "next_action": "retrieve",
                "ask_attribute": None,
            }

        # -----------------------------------------------------
        # Enough information already accumulated
        # -----------------------------------------------------

        if known_attributes >= 2:

            return {
                "next_action": "retrieve",
                "ask_attribute": None,
            }

        # -----------------------------------------------------
        # Browsing / vague request -> clarify
        # -----------------------------------------------------

        attribute = self.choose_next_attribute(
            session_id
        )

        if attribute is None:

            # Nothing useful left to ask.
            return {
                "next_action": "retrieve",
                "ask_attribute": None,
            }

        return {
            "next_action": "clarify",
            "ask_attribute": attribute,
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

            "constraints": dict(
                state.slots
            ),

            "no_preference": sorted(
                state.no_preference
            ),

            "asked_attributes": sorted(
                state.asked_attributes
            ),

            "search_query":
                self.build_search_context(
                    session_id
                ),

            "next_action":
                decision["next_action"],

            "ask_attribute":
                decision["ask_attribute"],

            "turn": state.turn,
        }
