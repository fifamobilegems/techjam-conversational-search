# Submission layout map

`docs/submission_rules.md` gives a **recommended** file layout. This folder
provides it, mapped onto the layout the official evaluator actually requires.

| Recommended | Actual | Why |
|---|---|---|
| `submission/agent.py` | `starter/agent.py` | `evaluator/local_evaluator.py:12` does `from starter.agent import Agent`. The evaluator is organizer-owned and must stay unmodified, so the implementation cannot move. `submission/agent.py` re-exports it. |
| `submission/requirements.txt` | `requirements*.txt` at the root | Included with `-r`, so there is one source of truth and no version drift. |
| `submission/README.md` | `README.md` at the root | The full report — method, results, disclosure, limitations — is in the root README. This file is only the layout map. |
| `submission/src/` | `starter/`, `state/`, `retrieval/` | The helper modules, left at import paths the evaluator and the tests already use. |

Nothing here re-implements anything. Moving the real code into this folder
would break the one import the official scorer depends on, so the folder is a
façade over it instead.

## What actually runs

```bash
python3 -m evaluator.local_evaluator      # the official scorer
```

That imports `starter.agent`. `submission/agent.py` exists so that

```python
from submission.agent import Agent
```

also resolves to the same class, for anyone following the recommended layout.

## Where the required submission content lives

| Requirement | Location |
|---|---|
| Python agent entry file exporting `Agent` | `starter/agent.py` (aliased here) |
| Required local helper modules | `starter/`, `state/`, `retrieval/` |
| Setup instructions | root `README.md` § Setup |
| Method, model choice, limitations | root `README.md` |
| Latency, token usage, estimated cost | root `README.md` § Disclosure |
| Dependency manifest | root `requirements*.txt` (included here) |
