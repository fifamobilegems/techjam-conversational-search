from __future__ import annotations

from pathlib import Path

from starter.debug import candidates_enabled, write_trace
from starter.env import load_project_env
from starter.extractor import HeuristicTurnExtractor
from starter.retriever import CatalogRetriever
from state.llm_extractor import LLMTurnExtractor, is_enabled as llm_enabled
from state.state_manager import StateManager


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

HOLDING_MESSAGE = "Let me narrow this down first. {question}"
RESULTS_MESSAGE = "Here are the closest matches I found. {question}"
NO_QUESTION_MESSAGE = "Here are the closest matches I found."


class Agent:
    """Conversational shopping agent with deterministic retrieval and reranking."""

    def __init__(
        self,
        catalog_path: str | Path = "data/catalog.jsonl",
        hold_until_turn: int = 2,
    ) -> None:
        load_project_env()
        self.retriever = CatalogRetriever(catalog_path)
        self.manager = StateManager(hold_until_turn=hold_until_turn)
        # The rules extractor is authoritative. The LLM layer, when enabled,
        # only adds spans the rules missed -- scoring must not depend on it,
        # because official scoring may run without network access.
        self.extractor = HeuristicTurnExtractor()
        if llm_enabled():
            self.extractor = LLMTurnExtractor(self.extractor)
        self._sessions: set[str] = set()
        self._profiles: dict[str, dict] = {}
        self._runs: dict[str, int] = {}

    def reset(self, session_id: str, user_profile: dict) -> None:
        self._sessions.add(session_id)
        self._profiles[session_id] = user_profile
        self._runs[session_id] = self._runs.get(session_id, 0) + 1
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
        previous_constraints = dict(current_state.slots)
        previous_no_preference = sorted(current_state.no_preference)
        extracted = self.extractor.extract(user_message, current_state)
        self.manager.update(session_id, extracted, turn)
        state = self.manager.export(session_id)

        ask_attribute = state["ask_attribute"]
        if ask_attribute:
            self.manager.mark_asked(session_id, ask_attribute)

        # Asking and retrieving are not alternatives: the contract carries
        # both fields and the evaluator reads both every turn. The only
        # decision is whether the ranking is worth a permanent rank record.
        if state["should_emit_recommendations"]:
            recommendations = [
                {"parent_asin": parent_asin}
                for parent_asin in self.retriever.retrieve_and_rerank(
                    state["search_query"],
                    state["constraints"],
                    state["no_preference"],
                    raw_constraints=state["raw_constraints"],
                    top_k=top_k,
                    raw_constraints=state["raw_constraints"],
                )
            ]
        else:
            recommendations = []

        message = self._message(ask_attribute, bool(recommendations))
        self.manager.record_message(session_id, "user", user_message, turn)
        self.manager.record_message(session_id, "assistant", message, turn)
        diagnostics = self.retriever.last_diagnostics if recommendations else {}
        trace_event = {
            "session_id": session_id,
            "run_index": self._runs.get(session_id, 0),
            "turn": turn,
            "scenario": state["scenario"],
            "intent": state["intent"],
            "user_message": user_message,
            "search_query": state["search_query"],
            "constraints": state["constraints"],
            "previous_constraints": previous_constraints,
            "raw_constraints": state["raw_constraints"],
            "no_preference": state["no_preference"],
            "previous_no_preference": previous_no_preference,
            "extracted_operations": [
                {
                    "attribute": item.attribute,
                    "action": item.action,
                    "value": item.value,
                    "raw_text": item.raw_text,
                }
                for item in extracted.operations
            ],
            "information_exhausted": extracted.information_exhausted,
            "ask_attribute": ask_attribute,
            "should_emit_recommendations": state["should_emit_recommendations"],
            "recommendations": [item["parent_asin"] for item in recommendations],
            "candidate_count": diagnostics.get("candidate_count", 0),
            "extractor": type(self.extractor).__name__,
        }
        if candidates_enabled():
            trace_event["bm25_candidate_ids"] = diagnostics.get("bm25_candidate_ids", [])
            trace_event["candidate_ids"] = diagnostics.get("candidate_ids", [])
        write_trace(trace_event)
        return {
            "message": message,
            "ask_attribute": ask_attribute,
            "recommendations": recommendations,
            "usage": dict(
                getattr(
                    self.extractor,
                    "last_usage",
                    {"prompt_tokens": 0, "completion_tokens": 0},
                )
            ),
        }

    def _message(self, ask_attribute: str | None, has_results: bool) -> str:
        """
        Prose for the human reading a transcript.

        The simulator reads `ask_attribute` and ignores this string, so the
        wording carries no score. It still has to make sense in the demo.
        """

        if not ask_attribute:
            return NO_QUESTION_MESSAGE

        question = QUESTION_TEXT.get(ask_attribute, QUESTION_TEXT["other"])
        template = RESULTS_MESSAGE if has_results else HOLDING_MESSAGE
        return template.format(question=question)
