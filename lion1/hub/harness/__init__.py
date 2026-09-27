"""Harness integrations: the programs that run a model for us (ADR-0009).

Two shapes. A chat-completion harness takes the view in and gives the model's text out, and this
runtime does everything else. An agentic harness runs its own tools inside one call; the runtime
rides its hooks. `claude_code` has both for the Claude Code CLI.
"""

from .claude_code import ClaudeCode, ClaudeCodeAgentic, ClaudeCodeError, ClaudeCodeSession
from .codex import Codex, CodexError
from .openrouter import OpenRouter, OpenRouterError

__all__ = (
    "ClaudeCode",
    "ClaudeCodeAgentic",
    "ClaudeCodeError",
    "ClaudeCodeSession",
    "Codex",
    "CodexError",
    "OpenRouter",
    "OpenRouterError",
)
