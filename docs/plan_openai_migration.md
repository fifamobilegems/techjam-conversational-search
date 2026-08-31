# Plan — migrate the LLM tier from Anthropic to OpenAI-compatible

## Context

`state/llm_extractor.py` implements the optional Tier-2 extraction layer against
the **Anthropic Messages API**. The team has an **OpenRouter** key, which speaks
the **OpenAI Chat Completions** protocol, so the current client cannot
authenticate or even form a valid request against it.

Two facts worth stating before anyone starts:

1. **The tier has never run.** Neither `anthropic` nor `openai` is installed, and
   neither is in `requirements.txt`. `_build_client()` returns `None` at the
   `ImportError` guard, so the extractor silently falls back to the deterministic
   path *regardless* of `TECHJAM_LLM_EXTRACTOR`. Every measured number
   (official 0.843, esci×esci 0.816) is a pure-offline result.
2. **Failures are silent by design.** `extract()` wraps the call in a bare
   `except Exception` and returns the deterministic result. A wrong key, wrong
   model id, or unsupported parameter produces **no error** — only
   `reported_token_usage` staying at 0. Any verification step must therefore
   assert on tokens, never on "it ran without crashing".

**Blast radius is small:** 7 provider-specific lines in 2 methods, plus one
constant and one docstring. Nothing in the extraction contract, the gate, the
verbatim guard, or the additive-merge logic changes.

## Ownership

`state/llm_extractor.py` is **Role B**'s file under the exclusive-ownership rule
in `docs/ARCHITECTURE.md`. Role A owns `docs/` and `.env_example` only. This plan
is therefore written to be executed by Role B, with a request filed in
`docs/handover/REQUESTS.md`. Role A should not apply the code change directly.

---

## Changes

### 1. Dependency

Add to `requirements.txt` (or a new `requirements-llm.txt`, to keep the scored
path dependency-free):

```
openai>=1.40
```

Keep it optional. The `ImportError` guard in `_build_client()` already degrades
cleanly when the package is absent, which preserves the offline-scoring
guarantee.

### 2. `_build_client()` — lines 199–209

```python
# before
try:
    import anthropic
except ImportError:
    return None
try:
    return anthropic.Anthropic(max_retries=1, timeout=10.0)
except Exception:
    return None
```

```python
# after
try:
    import openai
except ImportError:
    return None
try:
    # The SDK reads OPENAI_API_KEY and OPENAI_BASE_URL from the environment.
    # For OpenRouter set OPENAI_BASE_URL=https://openrouter.ai/api/v1
    return openai.OpenAI(max_retries=1, timeout=10.0)
except Exception:
    return None
```

### 3. `_call()` — the request, lines 219–225

Three structural differences: the system prompt moves *into* the messages array,
`output_config` becomes `response_format`, and the schema gains a `name`.

```python
# before
response = self._client.messages.create(
    model=self.model,
    max_tokens=MAX_TOKENS,
    system=SYSTEM_PROMPT,
    messages=[{"role": "user", "content": content}],
    output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
)
```

```python
# after
response = self._client.chat.completions.create(
    model=self.model,
    max_tokens=MAX_TOKENS,
    messages=[
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ],
    response_format={
        "type": "json_schema",
        "json_schema": {
            "name": "constraints",
            "strict": True,
            "schema": OUTPUT_SCHEMA,
        },
    },
)
```

`OUTPUT_SCHEMA` needs **no change**: it already sets
`additionalProperties: false` on every object and lists every property in
`required`, which is exactly what OpenAI strict mode demands.

### 4. `_call()` — usage accounting, lines 227–232

This feeds the evaluator's `reported_token_usage`, which the README requires to
be disclosed. The field names differ between providers.

```python
# before
"prompt_tokens": int(getattr(usage, "input_tokens", 0) or 0),
"completion_tokens": int(getattr(usage, "output_tokens", 0) or 0),
```

```python
# after
"prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
"completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
```

### 5. `_call()` — response parsing, lines 234–236

```python
# before
text = "".join(
    block.text for block in response.content if getattr(block, "type", "") == "text"
)
```

```python
# after
text = response.choices[0].message.content or ""
```

### 6. `DEFAULT_MODEL` — line 30, and the docstring — line 17

```python
DEFAULT_MODEL = "gpt-4o-mini"          # direct OpenAI
# DEFAULT_MODEL = "openai/gpt-4o-mini" # via OpenRouter (namespaced id required)
```

Update the docstring's enable block to name the new variables:

```
export TECHJAM_LLM_EXTRACTOR=1
export OPENAI_API_KEY=...
export OPENAI_BASE_URL=https://openrouter.ai/api/v1   # OpenRouter only
```

### 7. `.env_example` — Role A applies this half

Replace the `ANTHROPIC_API_KEY` entry with `OPENAI_API_KEY`, add
`OPENAI_BASE_URL`, and change the `TECHJAM_LLM_MODEL` default comment to
`gpt-4o-mini`.

---

## Gotchas that will cost an afternoon if missed

| Risk | Detail |
|---|---|
| **Model id namespacing** | OpenRouter requires `openai/gpt-4o-mini`; direct OpenAI requires `gpt-4o-mini`. The wrong form 404s, and the bare `except` swallows it into a silent no-op. |
| **Structured output support** | Not every OpenRouter-proxied model honours `response_format: json_schema`. If the provider rejects it, fall back to `response_format={"type": "json_object"}` and append the schema to `SYSTEM_PROMPT`. Verify on the exact model you intend to ship. |
| **`max_tokens` vs `max_completion_tokens`** | Reasoning models (o1/o3 family) reject `max_tokens`. `gpt-4o-mini` accepts it. Stay on a chat model or switch the parameter name. |
| **Silent failure** | The bare `except Exception` means every mistake above looks identical: no error, 0 tokens, deterministic output. Debug by calling `_call()` directly, outside the guard. |
| **Cost guard** | `TECHJAM_LLM_MAX_CALLS` (default 250) still applies and is provider-agnostic. Leave it in place. |

---

## Verification

Run in this order. Step 3 is the one that actually proves the migration.

```bash
# 1. client constructs
TECHJAM_LLM_EXTRACTOR=1 python3 -c "
from starter.extractor import HeuristicTurnExtractor
from state.llm_extractor import LLMTurnExtractor
print('active:', LLMTurnExtractor(HeuristicTurnExtractor()).active)"
# expect: active: True   (False = package missing or client construction failed)

# 2. one real call succeeds, bypassing the silent guard
TECHJAM_LLM_EXTRACTOR=1 python3 -c "
from starter.extractor import HeuristicTurnExtractor
from state.llm_extractor import LLMTurnExtractor
e = LLMTurnExtractor(HeuristicTurnExtractor())
print(e._call('waterproof hiking boots without laces under \$120'))
print('usage:', e.last_usage)"
# expect: a list of verbatim spans, and non-zero usage

# 3. tokens are actually reported end to end
TECHJAM_LLM_EXTRACTOR=1 python3 -m tools.bench \
  --datasets esci1000 --simulators esci --limit 50
# expect: the tokens column is non-zero. Still 0 => the tier is inert.

# 4. the offline path is untouched with the flag off
python3 -m tools.bench --datasets esci1000 --simulators esci --limit 50
# expect: identical to the pre-migration number, tokens 0

# 5. regressions
python3 -m unittest discover tests
```

`tests/test_lexicon_cascade.py` references the extractor and must stay green.

## Acceptance

- Flag **off**: byte-identical behaviour and 0 tokens (this is the scored path
  and it must not move).
- Flag **on**: non-zero `reported_token_usage`, no regression in exact constraint
  precision, and any score change measured on all three simulators.
- Cost and latency recorded for `docs/submission.md`, which the README requires.

## Alternative considered — provider-agnostic client

A `TECHJAM_LLM_PROVIDER=openai|anthropic` switch keeping both code paths was
considered and **not recommended** for this deadline: it doubles the surface that
must be tested for a tier that is additive-only and off by default. The swap
above is ~10 lines and trivially revertible via git.

## Honest cost/benefit

Worth stating plainly, because this work is optional:

- The tier is **additive-only**. It contributes spans the rules missed; it cannot
  improve ranking, which is where the remaining headroom actually is.
- It only fires when Tier 0 **and** Tier 1 both produce nothing and no template
  matched — a narrow slice.
- `docs/submission_rules.md` warns scoring may run **without network**, so the
  deterministic path must carry the full score regardless.
- "$0 cost, 0 tokens, fully offline" is a legitimate strength in the submission
  disclosure, not a gap.

Recommendation: do this only if a demo of the LLM tier is explicitly wanted, and
measure it before shipping it.
