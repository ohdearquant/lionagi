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
        LazyType("lionagi.providers.anthropic.messages.models:CreateMessageRequest"),
        "https://api.anthropic.com/v1",
        "x-api-key",
    )


AnthropicConfigs._PROVIDER = "anthropic"
AnthropicConfigs._PROVIDER_ALIASES = []


class ClaudeCodeConfigs(ProviderConfig, Enum):
    CLI = (
        "query_cli",
        ["cli", "code"],
        EndpointType.AGENTIC,
        LazyType("lionagi.providers.anthropic.claude_code.models:ClaudeCodeRequest"),
    )


ClaudeCodeConfigs._PROVIDER = "claude_code"
ClaudeCodeConfigs._PROVIDER_ALIASES = ["claude-code", "claude"]

__all__ = ("AnthropicConfigs", "ClaudeCodeConfigs")
