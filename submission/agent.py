"""Submission entry point — re-exports the agent, does not define it.

`docs/submission_rules.md` recommends a `submission/agent.py` entry file. The
official evaluator, however, imports the agent from a fixed path:

    evaluator/local_evaluator.py:12   from starter.agent import Agent

That file is organizer-owned and must stay unmodified, so moving the
implementation here would break scoring. Instead the implementation stays at
`starter/agent.py` and this module re-exports it, which satisfies the
recommended layout while leaving the scored import path untouched.

    from submission.agent import Agent      # equivalent to starter.agent

DO NOT add behaviour to this file. It is an alias. Edit `starter/agent.py`.
"""

from __future__ import annotations

from starter.agent import Agent

__all__ = ["Agent"]
