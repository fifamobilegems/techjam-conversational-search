"""Opt-in JSONL tracing for evaluator diagnosis.

Set ``AGENT_DEBUG_LOG=debug/deterministic_trace.jsonl`` before running an
evaluation.  Set ``AGENT_TRACE_CANDIDATES=1`` as well when you need to tell
whether a miss was absent from BM25's candidate pool or lost in reranking.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def candidates_enabled() -> bool:
    """True when per-turn candidate ids should be recorded.

    Off by default: the id list is ~500 entries per turn and would dominate
    the trace file.
    """
    return os.environ.get("AGENT_TRACE_CANDIDATES", "").lower() in {"1", "true", "yes"}


def write_trace(event: dict[str, Any]) -> None:
    """Append one turn to the debug trace, if tracing is enabled.

    Silently does nothing when `AGENT_DEBUG_LOG` is unset, so the scored path
    never pays for diagnostics.
    """
    configured_path = os.environ.get("AGENT_DEBUG_LOG", "").strip()
    if not configured_path:
        return
    path = Path(configured_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
