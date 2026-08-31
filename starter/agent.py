from __future__ import annotations

import os
from pathlib import Path

from starter.debug import candidates_enabled, write_trace
from starter.env import load_project_env
from starter.extractor import HeuristicTurnExtractor
from starter.retriever import CALIBRATED_WEIGHTS, WEIGHT_PRESETS, CatalogRetriever
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
        # Robustness-first integration decision (Role A): ship the calibrated
        # reranker weights by default. Measured trade vs `default`, items 1&3
        # applied: realistic/esci +0.076 mean technical, official -0.030 mean
        # (concentrated on public200-official). setdefault keeps it fully
        # reversible — an explicit RERANK_WEIGHTS in the shell or .env wins.
        # Passed explicitly rather than via os.environ.setdefault. Writing to
        # the process environment made "unset means default" untrue for every
        # later reader in the same process, so a bench sweeping presets scored
        # both arms with the calibrated weights. An explicit RERANK_WEIGHTS in
        # the shell or .env still wins, which is what keeps it reversible.
        self.retriever = CatalogRetriever(
            catalog_path,
            weights=WEIGHT_PRESETS.get(
                os.environ.get("RERANK_WEIGHTS", "calibrated").strip().lower(),
                CALIBRATED_WEIGHTS,
            ),
        )
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
        # Record the user's words BEFORE extraction/state export so
        # build_search_context() can fold them into the turn-1 query. Otherwise
        # real-phrasing sessions whose template extraction is empty search an
        # empty string on turn 1 and waste it (C -> A request in REQUESTS.md).
        # The assistant reply is still recorded after it is built, so there is
        # exactly one record per message.
        self.manager.record_message(session_id, "user", user_message, turn)
        extracted = self.extractor.extract(user_message, current_state)
        self.manager.update(session_id, extracted, turn)

        # Provisional retrieval runs BEFORE export, on every turn.
        #
        # Both dialogue policies score the live candidate set: the
        # clarification formula needs per-attribute statistics over the
        # products still in play, and the credibility test needs this turn's
        # scores. Exporting first left `retrieval_diagnostics` empty forever,
        # so both silently fell back to their legacy branches -- the agent
        # asked "other" every turn and never emitted early. Retrieval is the
        # same call either way; only whether the list is *exposed* is a
        # decision, and that is made below.
        live = self.manager.get(session_id)
        ranked = self.retriever.retrieve_and_rerank(
            self.manager.build_search_context(session_id),
            dict(live.slots),
            sorted(live.no_preference),
            top_k=top_k,
            raw_constraints=[dict(span) for span in live.raw_constraints],
            user_profile=self._profiles.get(session_id),
        )
        self.manager.set_retrieval_diagnostics(
            session_id, self.retriever.last_diagnostics
        )

        state = self.manager.export(session_id)

        ask_attribute = state["ask_attribute"]
        if ask_attribute:
            self.manager.mark_asked(session_id, ask_attribute)

        # Asking and retrieving are not alternatives: the contract carries
        # both fields and the evaluator reads both every turn. The only
        # decision is whether the ranking is worth a permanent rank record.
        recommendations = (
            [{"parent_asin": parent_asin} for parent_asin in ranked]
            if state["should_emit_recommendations"]
            else []
        )

        message = self._message(ask_attribute, bool(recommendations))
        self.manager.record_message(session_id, "assistant", message, turn)
        diagnostics = self.retriever.last_diagnostics
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
