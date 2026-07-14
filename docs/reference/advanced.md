# Advanced Usage

## HookRegistry

Attach callbacks that fire before or after every model invocation on an `iModel`.

```python
from lionagi.service.hooks import HookRegistry, HookEventTypes
import lionagi as li

async def log_pre(event, **kw):
    print(f"→ invoking: {type(event).__name__}")

async def log_post(event, **kw):
    print(f"← done: {type(event).__name__}")

hooks = HookRegistry(
    hooks={
        HookEventTypes.PreInvocation: log_pre,
        HookEventTypes.PostInvocation: log_post,
    }
)

model = li.iModel(model="gpt-4o", hook_registry=hooks)
```

`PreInvocation` fires when the event is dequeued, just before the API call.
`PostInvocation` fires after the response arrives. Both receive the `Event` instance.

**Sharing hooks across models**:

```python
chat = li.iModel(model="gpt-4o", hook_registry=hooks)
parse = li.iModel(model="gpt-4o-mini", hook_registry=hooks)
branch = li.Branch(chat_model=chat, parse_model=parse)
```

## Rate limiting

Control throughput with `limit_requests` and `limit_tokens` per rate-limit window:

```python
model = li.iModel(
    model="gpt-4o",
    limit_requests=500,          # max requests per window
    limit_tokens=100_000,        # max tokens per window
    capacity_refresh_time=60,    # window duration in seconds
    queue_capacity=200,          # max queued requests (backpressure)
    concurrency_limit=10,        # max concurrent in-flight requests
)
```

When the queue fills, new requests block until capacity frees.
`capacity_refresh_time` sets the periodic reset interval for the request and token
counters. At each interval, available capacity is restored to the configured limits;
this is a fixed reset schedule, not a rolling or sliding window.

**Independent buckets for parallel workflows** — use `model.copy()` for separate counters:

```python
workers = [model.copy(share_session=False) for _ in range(5)]
branches = [li.Branch(chat_model=w) for w in workers]
```

## Custom middle

The `Middle` protocol is a callable that advances the branch by one assistant turn.
Inject via `branch.operate(middle=...)`.

**Retry-wrapping example**:

```python
from lionagi.operations.types import ChatParam, ParseParam
from lionagi.operations.communicate.communicate import communicate

class RetryMiddle:
    def __init__(self, max_retries: int = 3):
        self._max = max_retries

    async def __call__(
        self, branch, instruction, chat_param: ChatParam,
        parse_param: ParseParam | None = None,
        clear_messages: bool = False,
        skip_validation: bool = False,
    ):
        for attempt in range(self._max):
            try:
                return await communicate(
                    branch, instruction, chat_param, parse_param,
                    clear_messages, skip_validation,
                )
            except Exception:
                if attempt == self._max - 1:
                    raise

retry = RetryMiddle(max_retries=3)
result = await branch.operate(instruction="...", middle=retry)
```

**Deterministic replay (testing)**:

```python
class ReplayMiddle:
    def __init__(self, responses: list[str]):
        self._queue = list(responses)

    async def __call__(self, branch, instruction, chat_param, parse_param=None, **_):
        return self._queue.pop(0)

replay = ReplayMiddle(["Paris", "42"])
result = await branch.operate(instruction="Capital of France?", middle=replay)
# No API call made — returns "Paris" from queue
```

**Cache middle**:

```python
class CacheMiddle:
    def __init__(self):
        self._cache: dict[str, Any] = {}

    async def __call__(self, branch, instruction, chat_param, parse_param=None,
                       clear_messages=False, skip_validation=False):
        key = str(instruction)
        if key in self._cache:
            return self._cache[key]
        result = await communicate(
            branch, instruction, chat_param, parse_param,
            clear_messages, skip_validation,
        )
        self._cache[key] = result
        return result
```

## Structured output edge cases

### `handle_validation` Modes

When the model returns output that does not parse into your `response_format` schema:

| Mode | Behavior |
|------|---------|
| `"raise"` | Raise `ValueError` immediately |
| `"return_value"` | Return the raw string (default for `operate()`) |
| `"return_none"` | Return `None` |

```python
# Strict: raises on any parse failure
result = await branch.operate(
    instruction="Extract entity",
    response_format=EntityModel,
    handle_validation="raise",
)

# Lenient: raw string on failure — check type before using
result = await branch.operate(
    instruction="Extract entity",
    response_format=EntityModel,
    handle_validation="return_value",
)
if not isinstance(result, EntityModel):
    print("Parse failed:", result)
```

### Fuzzy Key Matching

`branch.parse()` enables fuzzy key matching by default — handles minor key name variations
from the model (e.g., `"key_points"` vs `"keyPoints"`):

```python
verdict = await branch.parse(
    text=raw_llm_output,
    response_format=VerdictModel,
    fuzzy_match=True,
    similarity_threshold=0.85,  # lower = more tolerant
    handle_validation="raise",
)
```

Disable with `fuzzy_match=False` when you need exact key matching.

### Streaming + Structured Output

With CLI endpoints and `stream_persist=True`, chunks write to JSONL as they arrive:

```python
result = await branch.operate(
    instruction="Generate a detailed report on...",
    response_format=ReportModel,
    stream_persist=True,
    persist_dir="./logs/streams",
    chat_model=li.iModel(provider="claude_code", model="sonnet"),
)
# chunks → ./logs/streams/{branch_id}.buffer.jsonl
# return value → ReportModel (parsed from accumulated text)
```

## `FieldModel` Dynamic Extensions

Add fields to any `operate()` call without modifying your base schema:

```python
from lionagi import FieldModel

result = await branch.operate(
    instruction="Analyze this article",
    response_format=ArticleAnalysis,
    field_models=[
        FieldModel(
            name="confidence",
            annotation=float,
            default=0.0,
            description="Confidence score 0–1",
        ),
    ],
)
```

Next: [Provider reference](providers.md)
