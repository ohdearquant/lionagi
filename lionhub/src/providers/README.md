# lionhub.providers

Vendor endpoints for model and search providers: the request schemas, endpoint classes and per-vendor configuration that `iModel(provider=...)` resolves to, grouped by vendor. It is not the `iModel` class, the connection registry or the token-budget logic; those stay in the core under `lionagi/service/`.

Final import path: `lionhub.providers`. The core reaches it only through a lazy lookup (a module path in a table, or an import inside a function), so `import lionagi` alone loads nothing from here and `iModel(provider=...)` keeps resolving through all of 1.x. Every `lionagi.providers.*` path keeps working through 1.x as the same module object, with a `DeprecationWarning` naming the new path; 2.0 removes the old paths.

| Slot | What it holds | Source |
| --- | --- | --- |
| `__init__.py` | package marker; vendor modules are loaded by name, on use | `lionagi/providers/__init__.py` (empty today) |
| `exa.py` | Exa search, contents and find-similar endpoints | `lionagi/providers/exa/` |
| `openrouter.py` | OpenRouter chat endpoint | `lionagi/providers/openrouter/` |
| `nvidia_nim.py` | NVIDIA NIM chat and embedding endpoints | `lionagi/providers/nvidia_nim/` |
| `lattice.py` | Lattice chat and embedding endpoints | `lionagi/providers/lattice/` |
| `groq.py` | Groq chat and audio-transcription endpoints | `lionagi/providers/groq/` |
| `perplexity.py` | Perplexity chat endpoint | `lionagi/providers/perplexity/` |
| `ollama.py` | Ollama chat, embedding and generate endpoints | `lionagi/providers/ollama/` |
| `anthropic/` | Anthropic Messages API and the Claude Code CLI route; see [anthropic/README.md](anthropic/README.md) | `lionagi/providers/anthropic/` |
| `gemini/` | Gemini chat and the Antigravity CLI route; see [gemini/README.md](gemini/README.md) | `lionagi/providers/google/` |
| `openai/` | OpenAI chat completions, responses, Codex CLI and embeddings; see [openai/README.md](openai/README.md) | `lionagi/providers/openai/` |

The draft reserves one file per vendor, while each vendor is a package of several modules on the development line, so a slot may become a package when it moves. The development line also holds providers with no slot here (ag2, deepseek, firecrawl, tavily, pi) and helpers shared by all providers (`lionagi/providers/_cli_subprocess.py`, `lionagi/providers/_secret_resolution.py`, `lionagi/providers/errors.py`); where they land is decided with the provider move.

## Who uses it

- `lionagi/service/providers.py`: provider tables, and a lazy import of `lionagi.providers.openai._codex_profile`.
- `lionagi/service/token_budget.py`: table from provider name to module path.
- `lionagi/service/connections/registry.py`: the list of provider modules to load.
- `lionagi/agent/factory.py`: the Codex profile and endpoint.
- `lionagi/cli/_agent_depth.py`, `lionagi/cli/monitor.py`: the admission helper `lionagi/providers/_subagent_admission.py`.
- `lionagi/studio/operator/engine.py`: the Claude Code and Gemini CLI routes.

## Status

Moves next, ahead of the rest of the third wave (`docs/adr/ADR-0136-moving-non-core-packages-into-lionhub.md`, D6 and D7). The code is still at `lionagi/providers/`.
