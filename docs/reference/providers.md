# Provider Reference

`iModel` resolves a canonical `(provider, endpoint)` pair through
`EndpointRegistry`. Canonical names and declared aliases match exactly. Built-ins are
loaded lazily; on a miss, trusted and enabled plugin providers are consulted; only
then does lionagi create a generic OpenAI-compatible fallback.

API and CLI providers are different authentication lanes. For example,
`provider="anthropic"` calls the Anthropic API and reads `ANTHROPIC_API_KEY`, while
`provider="claude_code"` launches the installed `claude` CLI and uses its login.
Likewise, `gemini` is the Google API and `gemini_code` is the Gemini CLI.

## API providers

Pass `provider=` to `iModel()`, or let lionagi infer from the model name.

| Provider | `provider=` string | Aliases | Key env var | Endpoints |
|----------|-------------------|---------|------------|-----------|
| OpenAI | `"openai"` | — | `OPENAI_API_KEY` | `chat/completions`, `responses`, `embeddings`, `audio/speech`, `audio/transcriptions`, `images/generations`, `images/edits` |
| Anthropic | `"anthropic"` | — | `ANTHROPIC_API_KEY` | `messages` |
| Google Gemini | `"gemini"` | `"gemini-api"` | `GEMINI_API_KEY` | `chat/completions` |
| Ollama (local) | `"ollama"` | — | — (no key needed) | `chat/completions`, `embeddings`, `generate` |
| Lattice (local) | `"lattice"` | — | — (no key needed) | `chat/completions`, `embeddings` |
| NVIDIA NIM | `"nvidia_nim"` | `"nvidia"`, `"nim"` | `NVIDIA_NIM_API_KEY` | `chat/completions`, `embeddings` |
| Perplexity | `"perplexity"` | — | `PERPLEXITY_API_KEY` | `chat/completions` |
| Groq | `"groq"` | — | `GROQ_API_KEY` | `chat/completions`, `audio/transcriptions` |
| OpenRouter | `"openrouter"` | `"open-router"` | `OPENROUTER_API_KEY` | `chat/completions` |
| DeepSeek | `"deepseek"` | — | `DEEPSEEK_API_KEY` | `chat/completions` |

```python
# Explicit provider
model = li.iModel(provider="anthropic", model="claude-opus-4-7-20251001")

# Default provider inferred (openai)
model = li.iModel(model="gpt-4o")

# Slash notation — provider inferred from prefix
model = li.iModel(model="anthropic/claude-opus-4-7")

# Custom base URL (proxy, local inference)
model = li.iModel(
    provider="openai",
    base_url="http://localhost:8080/v1",
    model="my-model",
)
```

DeepSeek's `reasoning_effort` field maps lionagi effort levels: `low`/`medium` → `"high"`,
`xhigh` → `"max"`. DeepSeek native values (`low`, `medium`, `high`, `max`) pass through unchanged.

Lattice is a local inference server started with `lattice serve`. lionagi calls it at
`http://127.0.0.1:8080/v1`. By default a Lattice endpoint sets `allow_local_network=True`, and
with it the address guard admits only the loopback hosts `localhost`, `127.0.0.1` and `::1`: a
`base_url=` may change the port or the path but not the host. Passing
`allow_local_network=False` applies the ordinary guard instead, which refuses loopback and
private addresses. The guard checks the URL once, before the request, so a Lattice request never
follows a redirect: the redirect comes back as the failed response. `model` must be the id the
server reports at `GET /v1/models`.

An `api_key` endpoint setting is dropped whether supplied as a keyword, in a config dict, or
in an `EndpointConfig`; the caller's config object is left unchanged. Rebuilding a
`LatticeChatEndpoint` or `LatticeEmbedEndpoint` from its serialized config dict, an
`EndpointConfig` containing that schema or its JSON text, or its `from_dict()` restores the
Lattice request model. An absent request model also receives the provider default; a
caller-supplied request model class is preserved.

A request that omits `max_tokens` gets the server's default of 256 tokens, and `lattice serve`
refuses a `max_tokens` above its fixed cap of 4096. A reply cut at the `max_tokens` budget carries
`finish_reason: "length"`, which lionagi reads as neither an error nor an unfinished reply. The
server also refuses tool calling, `n` above 1, a `response_format` other than `{"type": "text"}`,
`top_logprobs` above 20 or without `logprobs`, and `logprobs` on a stream; lionagi forwards these
and the server answers 400. lionagi itself refuses a `top_p` of 0 and a `seed` outside the
unsigned 64-bit range `0` through `2**64 - 1`, which the server cannot take. Requests also take
`reasoning_budget` and `lora`, an ordered list of resident
adapters (`{"id": ..., "scale": ...}`); an omitted `lora` selects the base model on today's
servers, and `[]` pins it. `top_k` and `repetition_penalty` are accepted and ignored by
`lattice serve` (the standalone `lattice_serve` daemon applies them), and `lattice serve` reads
none of `frequency_penalty`, `presence_penalty`, `logit_bias` and `user`.

A reply's leading `<think>...</think>` block is moved from `content` to `reasoning_content`, so structured
output parses the answer after it; a block with no closing tag, from a reply cut off while it was
still reasoning, stays in `content`, and streamed replies keep the block where it arrives. A
generation that fails after its stream has started arrives as a chunk with `is_error` set, and the
call then fails: `iModel.stream` yields that chunk and raises once the stream ends.
Embeddings need `lattice serve` started on a vision-language checkpoint; a checkpoint without one
gets `400 vision_unsupported`.

```python
model = li.iModel(provider="lattice", model="qwen3.5-0.8b")
```

## CLI / agentic providers

CLI endpoints spawn subprocess tools instead of calling REST APIs.
Pass `provider=` to select one; `is_cli` is set `True` automatically.
Use with `Branch.run()` or `Branch.operate()`.

| Provider | `provider=` string | Aliases | CLI tool | Auth |
|----------|--------------------|---------|----------|------|
| Claude Code | `"claude_code"` | `"claude-code"`, `"claude"` | `claude` | `claude login` or `ANTHROPIC_API_KEY` |
| Codex | `"codex"` | `"openai-codex"` | `codex` | `codex login` (ChatGPT Plus/Pro) |
| Gemini CLI | `"gemini_code"` | `"gemini-code"`, `"gemini_cli"`, `"gemini-cli"` | `gemini` | `gemini auth` |
| Pi | `"pi"` | `"pi-code"`, `"pi_code"` | `pi` | local `pi` binary |

```python
# Claude Code CLI endpoint
claude_model = li.iModel(provider="claude_code", model="sonnet")

# Codex CLI endpoint
codex_model = li.iModel(provider="codex", model="o3")

async for msg in branch.run("Refactor this function:", chat_model=claude_model):
    print(msg.content, end="", flush=True)
```

`Branch.operate()` detects `is_cli=True` and routes to `run_and_collect` instead of `communicate`.
See [operations.md](../api/operations.md#middle-protocol) for routing details.

## Search and scraping providers

These providers integrate as callable tools via `branch.connect()` — not as chat models.

| Provider | Purpose | `provider=` string | Key env var | Endpoints |
|----------|---------|-------------------|------------|-----------|
| Exa | Neural search | `"exa"` | `EXA_API_KEY` | `search`, `contents` (alias: `get_contents`), `findSimilar` (aliases: `similar`, `find_similar`) |
| Firecrawl | Web scraping | `"firecrawl"` | `FIRECRAWL_API_KEY` | `v1/scrape` (alias: `scrape`), `v1/map` (alias: `map`), `v1/crawl` (alias: `crawl`) |
| Tavily | Research search | `"tavily"` | `TAVILY_API_KEY` | `search`, `extract` |

```python
branch.connect(provider="exa", endpoint="search", name="search")
results = await branch.operate(
    instruction="Find recent papers on diffusion models",
    actions=True,
    tools=["search"],
)
```

## AG2 (multi-agent)

AG2 GroupChat wraps AG2 v0.12's `GroupChat` as a streaming lionagi endpoint.
Requires the optional `ag2` extra:

```bash
pip install lionagi[ag2]
```

| Provider | `provider=` string | Aliases | Endpoint | Type |
|----------|--------------------|---------|----------|------|
| AG2 GroupChat | `"ag2"` | `"autogen"` | `group_chat` (aliases: `groupchat`, `chat`) | `agentic` |
| AG2 beta Agent | `"ag2"` | `"autogen"` | `agent` (aliases: `beta`, `ask`) | `agentic` |
| AG2 NLIP remote | `"ag2"` | `"autogen"` | `nlip` (aliases: `nlip_remote`, `remote`) | `agentic` |

AG2GroupChatEndpoint is stream-only — it yields `StreamChunk` events for each agent turn.
`_call()` raises `NotImplementedError`; use `branch.run()` or iterate `endpoint.stream()` directly.

```python
# requires: pip install lionagi[ag2]
from lionhub.providers.ag2 import AG2GroupChatEndpoint

endpoint = AG2GroupChatEndpoint(
    agent_configs=[
        {"name": "coder", "system_message": "Write Python code."},
        {"name": "reviewer", "system_message": "Review and critique code."},
    ],
    llm_config={"model": "gpt-4o-mini"},
)

async for chunk in endpoint.stream({"prompt": "Build a Fibonacci function"}):
    print(chunk.content, end="", flush=True)
```

## Provider folder structure

Each provider is a directory under `lionhub/src/lionhub/providers/{company}/`. The endpoint files
(one `{endpoint}.py` per capability) are the capability map — the file listing names the capabilities.

```text
lionhub/src/lionhub/providers/
├── openai/
│   ├── _config.py          # OpenAIConfigs + CodexConfigs enums
│   ├── chat.py             # chat/completions endpoint
│   ├── codex.py            # Codex CLI endpoint
│   ├── audio.py            # speech + transcription
│   ├── embed.py
│   ├── images.py
│   └── response.py         # Responses API
├── anthropic/
│   ├── _config.py          # AnthropicConfigs + ClaudeCodeConfigs enums
│   ├── messages.py
│   └── claude_code.py      # CLI endpoint
├── google/
│   ├── _config.py          # GeminiChatConfigs + GeminiCodeConfigs enums
│   ├── chat.py
│   └── gemini_code.py      # CLI endpoint
├── deepseek/
│   ├── _config.py
│   └── chat.py
├── ollama/
│   ├── _config.py
│   ├── chat.py
│   ├── embed.py
│   └── generate.py
├── lattice/
│   ├── _config.py
│   ├── chat.py
│   └── embed.py
├── nvidia_nim/
│   ├── _config.py
│   ├── chat.py
│   └── embed.py
├── perplexity/
│   ├── _config.py
│   └── chat.py
├── groq/
│   ├── _config.py
│   ├── chat.py
│   └── audio_transcription.py
├── openrouter/
│   ├── _config.py
│   └── chat.py
├── exa/
│   ├── _config.py
│   ├── search.py
│   ├── contents.py
│   └── find_similar.py
├── firecrawl/
│   ├── _config.py
│   ├── scrape.py
│   ├── map.py
│   └── crawl.py
├── tavily/
│   ├── _config.py
│   └── search.py
├── pi/
│   ├── _config.py
│   └── cli.py
└── ag2/
    ├── _config.py          # AG2Configs enum
    ├── agent.py
    ├── groupchat.py
    ├── nlip.py
    └── sandbox.py          # shared worktree helpers
```

Each endpoint has a focused module containing its `Endpoint` subclass; larger
providers may keep shared request/response schemas in private sibling modules. The
`_config.py` at the provider root declares one or more `ProviderConfig` enums; each
member carries the endpoint path, aliases, type, options class, base URL, and auth
type.

Each provider's `__init__.py` lazily re-exports every endpoint and request class, so the
import collapses to two layers and loads nothing until first access:

```python
from lionhub.providers.openai import OpenaiChatEndpoint
from lionhub.providers.anthropic import AnthropicMessagesEndpoint
```

Touching a name imports only that endpoint's module (plus the provider `_config`); the
other endpoints in the folder stay unloaded. `dir(lionhub.providers.openai)` lists the
full export set without triggering any import.

## Adding a new provider

**Step 1 — create the folder tree**

```text
lionhub/src/lionhub/providers/{name}/
    __init__.py     # lazy re-export map (see existing providers)
    _config.py
    chat.py         # one file per endpoint: Endpoint subclass + request/response models
```

**Step 2 — declare the config enum in `_config.py`**

```python
from enum import Enum
from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType

class MyProviderConfigs(ProviderConfig, Enum):
    CHAT = (
        "chat/completions",          # endpoint path
        ["chat"],                    # aliases
        EndpointType.API,
        LazyType("lionhub.providers.myprovider.chat:MyChatRequest"),
        "https://api.myprovider.com/v1",
        "bearer",                    # auth_type
    )

MyProviderConfigs._PROVIDER = "myprovider"
MyProviderConfigs._PROVIDER_ALIASES = ["my-provider"]
```

**Step 3 — write `chat.py` using the config member's registration decorator**

```python
from pydantic import BaseModel

from lionagi.service.connections import Endpoint
from ._config import MyProviderConfigs

__all__ = ("MyProviderChatEndpoint", "MyChatRequest")

class MyChatRequest(BaseModel):
    model: str
    messages: list[dict[str, str]]

@MyProviderConfigs.CHAT.register
class MyProviderChatEndpoint(Endpoint):
    pass  # config is auto-created from _ENDPOINT_META injected by the decorator
```

Importing `_config.py` defines the config members; importing the decorated endpoint
module registers the endpoint. The registration decorator resolves the member's
`LazyType` request-options reference when it builds the registry arguments.

List the endpoint and request classes in `__all__` — the `__init__.py` lazy map derives
its export set from each module's `__all__`.

**Step 4 — add the provider to the built-in manifest**

In `lionhub/src/lionhub/providers/manifest.py`, add a `_Provider(...)` row to
`_DECLARATIONS`. The row names the provider package, the config enum in that package's
`_config.py`, the context-window table in `_config.py` (or `None` when the provider has
none), and one `_Endpoint(member, "module:Class")` per endpoint:

```python
_DECLARATIONS = (
    # ... existing rows ...
    _Provider(
        "myprovider",
        "MyProviderConfigs",
        None,
        (_Endpoint("CHAT", "myprovider.chat:MyProviderChatEndpoint"),),
    ),
)
```

`member` is the name of the enum member, and `module:Class` names the endpoint class and
its module, relative to `lionhub.providers`. Put the provider's default endpoint first: the
first endpoint is the one `EndpointRegistry.match(provider="myprovider")` resolves to when
no endpoint is named. Keep the rows in registration order, so add the new row after the
existing ones.

If the provider has a context-window table, name it in the row instead of `None` and add
the provider's key to `_CONTEXT_WINDOW_ORDER` in the same file. That order decides which
table answers when two tables hold the same model name, and a non-empty order that leaves
out a provider with a table raises a `ValueError`.

The manifest reads `_config.py` for the provider's keys, aliases and settings and imports no
endpoint module. The first time the registry is used, it calls each declaration's factory,
which imports the endpoint module and so runs its registration decorator. If that import
raises an `ImportError`, for example because the module needs an optional third-party
dependency that is not installed, the registry logs a warning (`provider module
'lionhub.providers.myprovider.chat' failed to import and was not registered: ...`) and
registers the rest; the error is not silently discarded. `_PROVIDER_OPTIONAL_DEPENDENCIES` in
`registry.py` does not apply to these modules: it covers only modules imported through
`_import_provider_module`, which today is the test-only scripted provider, and it has no
entries.

`EndpointRegistry.match(provider="myprovider", endpoint="chat")` will then find the
endpoint automatically. This manifest row is for built-in providers. Third-party
providers should normally be declared by a plugin; trusted, enabled plugin provider
targets are imported lazily after a built-in miss, and cannot override a built-in
provider name.

**Step 5 — add the provider to the inventories**

The provider list above, the chat/embed and resolution tables in `docs/api/imodel.md`,
and the provider package list in `CLAUDE.md` are each hand-maintained. Add the provider to
all three; nothing else prompts the update.

## Default provider config

Set environment variables to avoid repeating `provider=` on every `iModel()` call:

```bash
export LIONAGI_CHAT_PROVIDER=openai
export LIONAGI_CHAT_MODEL=gpt-4.1-mini
```

Or configure per branch:

```python
branch = li.Branch(
    chat_model=li.iModel(provider="anthropic", model="claude-opus-4-7-20251001"),
    parse_model=li.iModel(model="gpt-4o-mini"),
)
```

Next: [Troubleshooting](troubleshooting.md)
