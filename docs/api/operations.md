# Operations & Extension API

`Branch.operate()` dispatches through a **Middle** — a callable that runs one assistant turn.
The two built-in middles are `communicate` (API endpoints) and `run_and_collect` (CLI endpoints).
Inject a custom middle via `branch.operate(..., middle=my_middle)`.

## `Middle` Protocol

```python
from lionagi.operations.types import Middle

class Middle(Protocol):
    async def __call__(
        self,
        branch: Branch,
        instruction: JsonValue | Instruction,
        chat_param: ChatParam,
        parse_param: ParseParam | None = None,
        clear_messages: bool = False,
        skip_validation: bool = False,
    ) -> Any: ...
```

A Middle receives the branch state and instruction, runs the model, optionally parses output,
and returns text, dict, or `BaseModel`. It advances the branch by exactly one assistant turn.

**Built-in middles**:

| Middle | Module | Used when |
|--------|--------|----------|
| `communicate` | `lionagi.operations.communicate` | `iModel.is_cli == False` (API endpoints) |
| `run_and_collect` | `lionagi.operations.run` | `iModel.is_cli == True` (CLI endpoints) |

**Custom middle use cases**: caching (cache-hit → skip model call), retry wrapping,
recorded replay for deterministic tests, logging/tracing decorators.

## `MorphParam` Base Class

All param types are frozen, slotted dataclasses. Freezing prevents field rebinding, but
it is shallow: nested lists and dictionaries remain mutable, and values such as those
can make a param instance unhashable.

```python
@dataclass(slots=True, frozen=True, init=False)
class MorphParam(Params): ...
```

Pass param instances to `operate()` or build them explicitly for advanced control.

## Param types

### `ChatParam` — for `chat()` / `communicate()`

```python
from lionagi.operations.types import ChatParam
```

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `guidance` | `JsonValue` | `None` | Additional guidance injected into instruction |
| `context` | `JsonValue` | `None` | Prompt context visible to the model |
| `sender` | `SenderRecipient` | `None` | Message sender identity |
| `recipient` | `SenderRecipient` | `None` | Message recipient identity |
| `response_format` | `type[BaseModel] \| dict` | `None` | Structured output schema |
| `structure` | `type[Structure] \| str \| None` | `None` | Structure class or string selector used to render the response format in the instruction |
| `progression` | `ID.RefSeq` | `None` | Custom message ordering |
| `tool_schemas` | `list[dict]` | `None` | Raw tool schemas (override registered tools) |
| `images` | `list` | `None` | Image inputs |
| `image_detail` | `"low" \| "high" \| "auto"` | `None` | Image resolution hint |
| `plain_content` | `str` | `None` | Bypass instruction formatting |
| `include_token_usage_to_model` | `bool` | `False` | Inject token stats into next prompt |
| `imodel` | `iModel` | `None` | Override branch's chat model for this call |
| `imodel_kw` | `dict` | `None` | Extra kwargs merged into model invocation |
| `turn_origin` | `TurnOrigin` | `None` | Tri-state user-turn disposition for `USER_PROMPT_SUBMIT`; `None` resolves to an unset origin, while internal calls use forwarded or no-origin values to avoid duplicate hooks |

### `RunParam` — for `run()` (extends `ChatParam`)

```python
from lionagi.operations.types import RunParam
```

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `stream_persist` | `bool` | `False` | Write chunks to JSONL as they arrive |
| `persist_dir` | `str \| Path` | `~/.lionagi/logs/runs` | JSONL output directory |
| `snapshot_dir` | `str \| Path \| None` | `None` | Optional branch-snapshot directory for streaming persistence; falls back to `persist_dir` when unset |

### `ParseParam` — for `parse()`

```python
from lionagi.operations.types import ParseParam
```

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `response_format` | `type[BaseModel] \| dict` | `None` | Target Pydantic model |
| `structure` | `Structure \| None` | `None` | Concrete structure instance used to parse the response; may be propagated from the instruction |
| `fuzzy_match_params` | `FuzzyMatchKeysParams \| dict` | `None` | Fuzzy key matching config |
| `handle_validation` | `HandleValidation` | `"raise"` | Failure behavior (see below) |
| `alcall_params` | `AlcallParams \| dict` | `None` | Async call params for retry loop |
| `imodel` | `iModel` | `None` | Model for parse retries |
| `imodel_kw` | `dict` | `None` | Extra kwargs for parse model |

### `InterpretParam` — for `interpret()`

```python
from lionagi.operations.types import InterpretParam
```

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `domain` | `str` | `None` | Target domain (e.g., `"scientific"`, `"legal"`) |
| `style` | `str` | `None` | Writing style (e.g., `"formal"`, `"concise"`) |
| `sample_writing` | `str` | `None` | Example text for style matching |
| `imodel` | `iModel` | `None` | Override interpret model |
| `imodel_kw` | `dict` | `None` | Extra kwargs |

### `ActionParam` — for `act()`

```python
from lionagi.operations.types import ActionParam
```

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `action_call_params` | `AlcallParams` | `None` | Async call config (concurrency, timeout) |
| `tools` | `ToolRef` | `None` | Subset of tools to expose |
| `strategy` | `"concurrent" \| "sequential"` | `"concurrent"` | Tool execution order |
| `suppress_errors` | `bool` | `True` | Catch tool errors instead of raising |
| `verbose_action` | `bool` | `False` | Log each tool invocation |

## `HandleValidation`

```python
HandleValidation = Literal["raise", "return_value", "return_none"]
```

| Value | Behavior on parse failure |
|-------|--------------------------|
| `"raise"` | Raise `ValueError` |
| `"return_value"` | Return the raw string |
| `"return_none"` | Return `None` |

## Custom middle example

```python
import lionagi as li
from lionagi.operations.types import ChatParam, ParseParam
from lionagi.operations.communicate.communicate import communicate

class CachedMiddle:
    """Skip the model call if this exact instruction was seen before."""

    def __init__(self):
        self._cache: dict[str, Any] = {}

    async def __call__(
        self,
        branch,
        instruction,
        chat_param: ChatParam,
        parse_param: ParseParam | None = None,
        clear_messages: bool = False,
        skip_validation: bool = False,
    ):
        key = str(instruction)
        if key in self._cache:
            return self._cache[key]
        result = await communicate(
            branch, instruction, chat_param, parse_param,
            clear_messages, skip_validation,
        )
        self._cache[key] = result
        return result


async def main():
    cache = CachedMiddle()
    branch = li.Branch(chat_model=li.iModel(model="gpt-4o-mini"))

    r1 = await branch.operate(instruction="What is the capital of France?", middle=cache)
    r2 = await branch.operate(instruction="What is the capital of France?", middle=cache)
    # r2 returned from cache — no API call

import asyncio
asyncio.run(main())
```

## Built-in `communicate` Function

```python
from lionagi.operations.communicate.communicate import communicate

result = await communicate(
    branch=branch,
    instruction="Summarize this text: ...",
    chat_param=ChatParam(response_format=SummaryModel),
    parse_param=ParseParam(handle_validation="return_value"),
    clear_messages=False,
    skip_validation=False,
)
```

## Built-in `run_and_collect` Function

```python
from lionagi.operations.run.run import run_and_collect

result = await run_and_collect(
    branch=branch,
    instruction="Generate a report on ...",
    chat_param=ChatParam(),
    parse_param=None,
)
```

Streams from a CLI endpoint, collects all chunks, and optionally parses the result.
Satisfies the `Middle` protocol — can be passed to `branch.operate(middle=run_and_collect)`.

Next: [Advanced usage](../reference/advanced.md) — hooks, rate limiting, and custom middles in depth
