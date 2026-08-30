from __future__ import annotations

from pathlib import Path

from starter.extractor import HeuristicTurnExtractor
from starter.retriever import CatalogRetriever
from state.state_manager import StateManager


QUESTION_PRIORITY = [
    "other",
    "feature",
    "material",
    "color",
    "size",
    "use_case",
    "budget",
    "brand",
    "style",
    "category",
]

QUESTION_TEXT = {
    "category": "What product category should I focus on?",
    "material": "Do you have a material preference?",
    "color": "Do you have a color preference?",
    "size": "What size or fit detail matters most?",
    "style": "What style should I prioritize?",
    "brand": "Is there a brand you prefer?",
    "budget": "What budget range should I stay within?",
    "feature": "What product feature matters most?",
    "use_case": "What will you mainly use it for?",
    "other": "What other must-have detail should I prioritize?",
}


class Agent:
    """Conversational shopping agent with deterministic retrieval and reranking."""

    def __init__(self, catalog_path: str | Path = "data/catalog.jsonl") -> None:
        self.retriever = CatalogRetriever(catalog_path)
        self.manager = StateManager()
        self.extractor = HeuristicTurnExtractor()
        self._sessions: set[str] = set()
        self._profiles: dict[str, dict] = {}

    def reset(self, session_id: str, user_profile: dict) -> None:
        self._sessions.add(session_id)
        self._profiles[session_id] = user_profile
        self.manager.reset(session_id)

    def respond(
        self,
        session_id: str,
        user_message: str,
        turn: int,
        top_k: int,
    ) -> dict:
        if session_id not in self._sessions:
            raise RuntimeError("reset must be called before respond")

        current_state = self.manager.get(session_id)
        extracted = self.extractor.extract(user_message, current_state)
        self.manager.update(session_id, extracted, turn)
        state = self.manager.export(session_id)

        recommendations = [
            {"parent_asin": parent_asin}
            for parent_asin in self.retriever.retrieve_and_rerank(
                state["search_query"],
                state["constraints"],
                state["no_preference"],
                top_k=top_k,
            )
        ]

        ask_attribute = self._choose_ask_attribute(session_id, turn)
        message = QUESTION_TEXT[ask_attribute] if ask_attribute else "Here are the closest matches I found."

        return {
            "message": message,
            "ask_attribute": ask_attribute,
            "recommendations": recommendations,
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        }

    def _choose_ask_attribute(self, session_id: str, turn: int) -> str | None:
        if turn >= 10:
            return None

        state = self.manager.get(session_id)
        for attribute in QUESTION_PRIORITY:
            if attribute in state.slots:
                continue
            if attribute in state.no_preference:
                continue
            if attribute in state.asked_attributes:
                continue
            self.manager.mark_asked(session_id, attribute)
            return attribute

        return None
