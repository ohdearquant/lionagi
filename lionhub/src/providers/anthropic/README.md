# lionhub.providers.anthropic

The Anthropic routes: the Messages API endpoint and the Claude Code CLI endpoint, with their request models. It is not the CLI-subprocess handling, secret resolution or error types that all providers share; those are outside this folder.

Final import path: `lionhub.providers.anthropic`. The old path `lionagi.providers.anthropic` keeps working through 1.x as the same module object, with a `DeprecationWarning` naming the new path.

| Slot | What it holds | Source |
| --- | --- | --- |
| `__init__.py` | lazy exports: `AnthropicMessagesEndpoint`, `ClaudeCodeCLIEndpoint`, `ClaudeCodeRequest`, `CreateMessageRequest`, `stream_claude_code_cli` | `lionagi/providers/anthropic/__init__.py` |
| `messages.py` | Messages API endpoint and the `CreateMessageRequest` model | `lionagi/providers/anthropic/messages.py` |
| `claude_code.py` | Claude Code CLI endpoint, `ClaudeCodeRequest` and the streaming runner | `lionagi/providers/anthropic/claude_code.py` |

Two support modules sit beside these on the development line and have no slot in the draft: `lionagi/providers/anthropic/_config.py` and `lionagi/providers/anthropic/_prompt_cache.py`. They move with the package.

## Who uses it

- `lionagi/service/providers.py` and `lionagi/service/token_budget.py`: provider tables that name this package.
- `lionagi/studio/operator/engine.py`: the Claude Code CLI route.

## Status

Moves next, with `lionhub.providers`. The code is still at `lionagi/providers/anthropic/`.
