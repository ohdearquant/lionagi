# `iModel`

```python
class iModel:
    ...
```

Uniform wrapper around one resolved provider endpoint, with rate limiting, queuing,
streaming, and invocation hooks. `iModel` is intentionally lower-level than
`Branch`; most applications should make model turns through a branch.

## Constructor

```python
model = li.iModel(
    provider="openai",
    model="gpt-4o",
    api_key=None,           # falls back to OPENAI_API_KEY env var
    limit_requests=100,
    limit_tokens=100_000,
)
```

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `provider` | `str \| None` | `None` | `"openai"`, `"anthropic"`, etc. Inferred from `model` if set |
| `base_url` | `str \| None` | `None` | Custom API URL (for proxies, local endpoints) |
| `endpoint` | `str \| Endpoint` | `"chat"` | Endpoint type (see table below) |
| `api_key` | `str \| None` | `None` | Explicit key; falls back to env var |
| `queue_capacity` | `int \| None` | auto | Max queued requests before backpressure |
| `capacity_refresh_time` | `float` | `60` | Seconds between queue capacity refreshes |
| `interval` | `float \| None` | auto | Queue processing interval in seconds |
| `limit_requests` | `int \| None` | `None` | Max requests per rate-limit cycle |
| `limit_tokens` | `int \| None` | `None` | Max tokens per rate-limit cycle |
| `concurrency_limit` | `int \| None` | `None` | Max concurrent streams |
| `streaming_process_func` | `Callable \| None` | `None` | Custom chunk processor for streaming responses |
| `provider_metadata` | `dict \| None` | `None` | Auxiliary provider metadata; non-CLI session lookup reads its `session_id` key |
| `hook_registry` | `HookRegistry \| dict \| None` | `HookRegistry()` | Pre/post invocation hooks |
| `**kwargs` | — | — | Provider-specific config (e.g., `model="gpt-4o"`, `temperature=0.7`) |

## Endpoint types

### Chat / LLM

| `provider=` | Default `endpoint=` | Key env var |
|-------------|---------------------|-------------|
| `"openai"` | `"chat"` | `OPENAI_API_KEY` |
| `"anthropic"` | `"chat"` | `ANTHROPIC_API_KEY` |
| `"gemini"` | `"chat"` | `GEMINI_API_KEY` |
| `"ollama"` | `"chat"` | — (local) |
| `"groq"` | `"chat"` | `GROQ_API_KEY` |
| `"deepseek"` | `"chat"` | `DEEPSEEK_API_KEY` |
| `"perplexity"` | `"chat"` | `PERPLEXITY_API_KEY` |
| `"openrouter"` | `"chat"` | `OPENROUTER_API_KEY` |
| `"nvidia_nim"` | `"chat"` | `NVIDIA_NIM_API_KEY` |

### Embed

| `provider=` | `endpoint=` | Key env var |
|-------------|-------------|-------------|
| `"openai"` | `"embed"` / `"embeddings"` | `OPENAI_API_KEY` |
| `"ollama"` | `"embed"` / `"embeddings"` | — (local) |
| `"nvidia_nim"` | `"embed"` | `NVIDIA_NIM_API_KEY` |

These endpoints are all registered and routable through `match_endpoint()`.

### OpenAI responses API

| `provider=` | `endpoint=` | Notes |
|-------------|-------------|-------|
| `"openai"` | `"response"` | Stateful Responses API (`/v1/responses`) |

### CLI / Agentic

| `provider=` | Aliases | Notes |
|-------------|---------|-------|
| `"claude_code"` | `"claude"`, `"claude-code"` | Claude Code CLI |
| `"codex"` | — | OpenAI Codex CLI |
| `"gemini_code"` | `"gemini-code"`, `"gemini_cli"`, `"gemini-cli"` | Gemini CLI |
| `"pi"` | `"pi-code"`, `"pi_code"` | Pi CLI |
| `"ag2"` | `"autogen"` | AG2 GroupChat/Agent run in-process; NLIP uses remote HTTP (stream-only; requires `pip install lionagi[ag2]`) |

Agentic endpoints report `is_cli = True`, which makes `Branch.operate()` route them
to `run_and_collect` instead of `communicate`. Only the CLI-backed providers
(`claude_code`, `codex`, `gemini_code`, and `pi`) launch installed subprocess tools
and use those tools' authentication. AG2 GroupChat and Agent execute in-process;
AG2 NLIP connects to a remote HTTP endpoint. See
[operations.md#middle-protocol](operations.md#middle-protocol).

### Search

| `provider=` | `endpoint=` | Key env var |
|-------------|-------------|-------------|
| `"exa"` | `"search"` | `EXA_API_KEY` |
| `"tavily"` | `"search"` | `TAVILY_API_KEY` |
| `"tavily"` | `"extract"` | `TAVILY_API_KEY` |

### Scrape / Crawl

| `provider=` | `endpoint=` | Key env var |
|-------------|-------------|-------------|
| `"firecrawl"` | `"scrape"` | `FIRECRAWL_API_KEY` |
| `"firecrawl"` | `"map"` | `FIRECRAWL_API_KEY` |

### Fallback

After built-ins and trusted, enabled plugin providers are consulted, an unrecognized
`provider` raises `ProviderNotFoundError` naming the requested provider and every
registered provider, unless the caller opts in explicitly. Pass `openai_compatible=True`
to route the unrecognized provider to a generic OpenAI-compatible endpoint. A registered
provider is never rejected this way, even if the requested `endpoint=` isn't one of its
own -- only a `provider` name that matches no registration falls into this path.

## Endpoint matching

```text
iModel(provider="openai", endpoint="chat")
  → match_endpoint("openai", "chat")
  → OpenaiChatEndpoint
```

`match_endpoint()` dispatches through `EndpointRegistry` using exact canonical
provider/endpoint names or their declared aliases:

- Default `endpoint="chat"` resolves to the provider's chat class.
- A single-endpoint provider can resolve its sole endpoint even when the caller uses
  the default `endpoint="chat"`.
- On a built-in miss, trusted and enabled plugin provider targets are loaded lazily.
- Built-in provider names win if a plugin declares a collision.
- A final miss raises `ProviderNotFoundError` unless the caller passes
  `openai_compatible=True` (or the deprecated `base_url=` migration path), in which case
  it returns the generic OpenAI-compatible `Endpoint` fallback. This only applies to a
  `provider` name that matches no registration at all.

## Common construction patterns

```python
import lionagi as li

# OpenAI (default)
model = li.iModel(model="gpt-4o")

# Anthropic
model = li.iModel(provider="anthropic", model="claude-opus-4-7-20251001")

# With rate limits
model = li.iModel(model="gpt-4o", limit_requests=100, limit_tokens=100_000)

# Ollama local
model = li.iModel(
    provider="ollama",
    base_url="http://localhost:11434",
    model="llama3",
)

# NVIDIA NIM
model = li.iModel(provider="nvidia_nim", model="meta/llama-3.1-70b-instruct")

# DeepSeek
model = li.iModel(provider="deepseek", model="deepseek-chat")

# OpenAI Responses API
model = li.iModel(provider="openai", endpoint="response", model="gpt-4o")

# CLI endpoints (use Branch.run() for chunks or Branch.operate() to collect)
model = li.iModel(provider="claude_code", model="sonnet")
model = li.iModel(provider="codex", model="codex-mini-latest")
model = li.iModel(provider="gemini_code", model="gemini-2.5-pro")
model = li.iModel(provider="pi", model="pi")

# Search
exa   = li.iModel(provider="exa", endpoint="search")
tvly  = li.iModel(provider="tavily", endpoint="search")

# Scrape / crawl
crawl = li.iModel(provider="firecrawl", endpoint="scrape")
cmap  = li.iModel(provider="firecrawl", endpoint="map")

# OpenAI-compatible custom host
model = li.iModel(
    provider="my_provider",
    base_url="https://my-api.example.com/v1",
    openai_compatible=True,
    model="my-model",
)
```

## Public methods

### `invoke()`

```python
api_call = await model.invoke(
    messages=[{"role": "user", "content": "hello"}],
    temperature=0.7,
)
response_text = api_call.response
```

Sends a rate-limited request. Returns `APICalling` with `.response` attribute.

### `stream()`

```python
async for chunk in model.stream(messages=[...]):
    print(chunk, end="", flush=True)
```

Streaming request. Prefer `Branch.run()` for managed streaming with message history.

### `create_api_calling()`

```python
api_call = model.create_api_calling(
    messages=[{"role": "user", "content": "hello"}],
)
# inspect before invoking
result = await model.invoke(api_call)
```

Constructs an `APICalling` object without sending the request.

### `copy()`

```python
model2 = model.copy(share_session=False)
```

Creates a fresh `iModel` with the same config but a new ID and executor.
Use when you need independent rate-limit buckets for parallel workflows.

### `close()`

```python
await model.close()
```

Stops the executor and releases resources. Not needed when using as context manager.

## Context manager

```python
async with li.iModel(model="gpt-4o") as model:
    api_call = await model.invoke(messages=[{"role": "user", "content": "hello"}])
    print(api_call.response)
# executor closed automatically
```

## Properties

| Property | Type | Notes |
|----------|------|-------|
| `model_name` | `str` | Model identifier string |
| `is_cli` | `bool` | `True` for agentic/CLI endpoints such as `claude_code`, `codex`, `gemini_code`, `pi`, and AG2 |
| `request_options` | `type[BaseModel] \| None` | Endpoint-specific request schema |
| `provider_session_id` | `str \| None` | `endpoint.session_id` for agentic/CLI endpoints; otherwise `provider_metadata["session_id"]` |

## Provider resolution

Provider is inferred from `model` kwarg when it contains a slash (e.g., `"anthropic/claude-opus-4-7"`).
Otherwise set `provider` explicitly. The `provider` string must match exactly (see aliases in the
CLI table above for accepted variants).

| `provider` string | API | Key env var |
|------------------|-----|------------|
| `"openai"` | OpenAI | `OPENAI_API_KEY` |
| `"anthropic"` | Anthropic | `ANTHROPIC_API_KEY` |
| `"gemini"` | Google AI (OpenAI-compat) | `GEMINI_API_KEY` |
| `"ollama"` | Ollama local | — (no key needed) |
| `"nvidia_nim"` | NVIDIA NIM | `NVIDIA_NIM_API_KEY` |
| `"perplexity"` | Perplexity Sonar | `PERPLEXITY_API_KEY` |
| `"groq"` | Groq | `GROQ_API_KEY` |
| `"openrouter"` | OpenRouter | `OPENROUTER_API_KEY` |
| `"deepseek"` | DeepSeek | `DEEPSEEK_API_KEY` |
| `"exa"` | Exa Search | `EXA_API_KEY` |
| `"tavily"` | Tavily | `TAVILY_API_KEY` |
| `"firecrawl"` | Firecrawl | `FIRECRAWL_API_KEY` |
| `"claude_code"` | Claude Code CLI | — |
| `"codex"` | OpenAI Codex CLI | — |
| `"gemini_code"` | Gemini CLI | — |
| `"pi"` | Pi CLI | — |

## HookRegistry

Pre/post invocation hooks for logging, caching, or metrics:

```python
from lionagi.service.hooks import HookRegistry, HookEventTypes

async def log_pre(event, **kw):
    print(f"Sending: {type(event).__name__}")

async def log_post(event, **kw):
    print(f"Received: {type(event).__name__}")

hooks = HookRegistry(
    hooks={
        HookEventTypes.PreInvocation: log_pre,
        HookEventTypes.PostInvocation: log_post,
    }
)

model = li.iModel(model="gpt-4o", hook_registry=hooks)
```

## Serialization

```python
data = model.to_dict()
restored = li.iModel.from_dict(data)
```

Next: [Operations & extension](operations.md) — Middle protocol and param types
