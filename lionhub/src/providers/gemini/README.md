# lionhub.providers.gemini

The Gemini routes: the chat endpoint and the Antigravity CLI endpoint (the `agy` command), with their request models. It is not the shared CLI-subprocess handling or secret resolution used by all providers.

Final import path: `lionhub.providers.gemini`. On the development line the package is named `google`; the draft and the final layout call it `gemini`. The old path `lionagi.providers.google` keeps working through 1.x as the same module object, with a `DeprecationWarning` naming the new path.

| Slot | What it holds | Source |
| --- | --- | --- |
| `__init__.py` | lazy exports: `GeminiChatEndpoint`, `GeminiCLIEndpoint`, `GeminiChunk`, `GeminiCodeRequest`, `GeminiSession`, `OpenAIChatCompletionsRequest`, `stream_gemini_cli` | `lionagi/providers/google/__init__.py` |
| `antigravity.py` | the Gemini code route through the Antigravity CLI: request model, session, chunks and the streaming runner | `lionagi/providers/google/gemini_code.py` |
| `chat_completions.py` | Gemini chat endpoint over the OpenAI-compatible chat-completions schema | `lionagi/providers/google/chat.py` |

The chat endpoint imports its schemas from `lionagi/providers/openai/_chat_schemas.py`, so `lionhub.providers.openai` must stay importable from here. `lionagi/providers/google/_config.py` has no slot in the draft and moves with the package.

## Who uses it

- `lionagi/service/providers.py`, `lionagi/service/token_budget.py`: provider tables that name this package.
- `lionagi/studio/operator/engine.py`: the Gemini CLI route.

## Status

Moves next, with `lionhub.providers`, and is renamed from `google` to `gemini` in the move. The code is still at `lionagi/providers/google/`.
