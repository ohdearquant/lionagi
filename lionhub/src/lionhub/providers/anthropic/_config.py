# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType


class AnthropicConfigs(ProviderConfig, Enum):
    MESSAGES = (
        "messages",
        ["chat"],
        EndpointType.API,
        LazyType("lionhub.providers.anthropic.messages:CreateMessageRequest"),
        "https://api.anthropic.com/v1",
        "x-api-key",
    )


AnthropicConfigs._PROVIDER = "anthropic"
AnthropicConfigs._PROVIDER_ALIASES = []
AnthropicConfigs._API_KEY_ENV = "ANTHROPIC_API_KEY"


class ClaudeCodeConfigs(ProviderConfig, Enum):
    CLI = (
        "query_cli",
        ["cli", "code"],
        EndpointType.AGENTIC,
        LazyType("lionhub.providers.anthropic.claude_code:ClaudeCodeRequest"),
    )


ClaudeCodeConfigs._PROVIDER = "claude_code"
ClaudeCodeConfigs._PROVIDER_ALIASES = ["claude-code", "claude"]


ANTHROPIC_CONTEXT_WINDOWS: dict[str, int] = {
    "claude-fable-5-1": 1_000_000,
    "claude-opus-5-5": 1_000_000,
    "claude-sonnet-5-5": 1_000_000,
    "claude-haiku-5-5": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-sonnet-4-5": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
}


CLAUDE_CODE_CONTEXT_WINDOWS: dict[str, int] = {
    "fable-5-1": 1_000_000,
    "opus-5-5": 1_000_000,
    "sonnet-5-5": 1_000_000,
    "haiku-5-5": 1_000_000,
    "opus-4-7": 1_000_000,
    "opus-4-6": 1_000_000,
    "opus": 1_000_000,
    "sonnet-5": 1_000_000,
    "sonnet-4-6": 1_000_000,
    "sonnet-4-5": 200_000,
    "sonnet": 1_000_000,
    "haiku-4-5": 200_000,
    "haiku": 200_000,
    "fable-5": 1_000_000,
    "fable": 1_000_000,
}

__all__ = (
    "AnthropicConfigs",
    "ClaudeCodeConfigs",
    "ANTHROPIC_CONTEXT_WINDOWS",
    "CLAUDE_CODE_CONTEXT_WINDOWS",
)
