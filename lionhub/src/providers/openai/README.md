# lionhub.providers.openai

The OpenAI routes: chat completions, the Responses API, the Codex CLI endpoint and embeddings, with their request models. It is not the shared CLI-subprocess handling, secret resolution or error types used by all providers.

Final import path: `lionhub.providers.openai`. The old path `lionagi.providers.openai` keeps working through 1.x as the same module object, with a `DeprecationWarning` naming the new path.

| Slot | What it holds | Source |
| --- | --- | --- |
| `__init__.py` | lazy exports for the endpoints and request models of every module in this package | `lionagi/providers/openai/__init__.py` |
| `chat_completions.py` | chat-completions endpoint and `OpenAIChatCompletionsRequest`, with its schema module | `lionagi/providers/openai/chat.py`, `lionagi/providers/openai/_chat_schemas.py` |
| `responses.py` | Responses API endpoint and `OpenAIResponsesRequest` | `lionagi/providers/openai/response.py` |
| `codex.py` | Codex CLI endpoint and request, its profile and project-layer helpers | `lionagi/providers/openai/codex.py`, `lionagi/providers/openai/_codex_profile.py`, `lionagi/providers/openai/_codex_project_layers.py` |
| `embeddings.py` | embedding endpoint and `OpenAIEmbeddingRequest` | `lionagi/providers/openai/embed.py` |

Modules with no slot in the draft: `lionagi/providers/openai/audio.py`, `lionagi/providers/openai/batch.py`, `lionagi/providers/openai/images.py`, and the support modules `_audio_schemas.py` and `_config.py`. They move with the package; the slot layout is settled at move time.

## Who uses it

- `lionagi/agent/factory.py`: the Codex profile and endpoint.
- `lionagi/service/providers.py`: provider tables, and a lazy import of `lionagi.providers.openai._codex_profile`.
- `lionagi/providers/google/chat.py`: imports the chat-completions schemas (see `gemini/`).

## Status

Moves next, with `lionhub.providers`. The code is still at `lionagi/providers/openai/`.
