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
from lionagi.providers.ag2 import AG2GroupChatEndpoint

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

Each provider is a directory under `lionagi/providers/{company}/`. The endpoint files
(one `{endpoint}.py` per capability) are the capability map — the file listing names the capabilities.

```text
lionagi/providers/
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
from lionagi.providers.openai import OpenaiChatEndpoint
from lionagi.providers.anthropic import AnthropicMessagesEndpoint
```

Touching a name imports only that endpoint's module (plus the provider `_config`); the
other endpoints in the folder stay unloaded. `dir(lionagi.providers.openai)` lists the
full export set without triggering any import.

## Adding a new provider

**Step 1 — create the folder tree**

```text
lionagi/providers/{name}/
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
        LazyType("lionagi.providers.myprovider.chat:MyChatRequest"),
        "https://api.myprovider.com/v1",
        "bearer",                    # auth_type
    )

MyProviderConfigs._PROVIDER = "myprovider"
MyProviderConfigs._PROVIDER_ALIASES = ["my-provider"]
```

**Step 3 — write `chat.py` using the config member's registration decorator**

```python
from lionagi.service.connections import Endpoint, EndpointConfig
from ._config import MyProviderConfigs  # noqa: F401 — side effect: registers

__all__ = ("MyProviderChatEndpoint", "MyChatRequest")

@MyProviderConfigs.CHAT.register
class MyProviderChatEndpoint(Endpoint):
    pass  # config is auto-created from _ENDPOINT_META injected by the decorator
```

List the endpoint and request classes in `__all__` — the `__init__.py` lazy map derives
its export set from each module's `__all__`.

**Step 4 — add the import to `registry.py`**

In `lionagi/service/connections/registry.py`, add the module to `_modules` inside
`_import_all_providers()`:

```python
def _import_all_providers():
    import importlib

    _modules: list[str] = [
        # ... existing entries ...
        "lionagi.providers.myprovider._config",
        "lionagi.providers.myprovider.chat",
    ]
    for mod in _modules:
        try:
            importlib.import_module(mod)
        except ImportError:
            pass
```

`EndpointRegistry.match(provider="myprovider", endpoint="chat")` will then find the
endpoint automatically. This registry edit is for built-in providers. Third-party
providers should normally be declared by a plugin; trusted, enabled plugin provider
targets are imported lazily after a built-in miss, and cannot override a built-in
provider name.

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
