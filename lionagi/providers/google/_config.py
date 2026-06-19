# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType


class GeminiChatConfigs(ProviderConfig, Enum):
    CHAT = (
        "chat/completions",
        ["chat"],
        EndpointType.API,
        LazyType("lionagi.providers.openai.chat.models:OpenAIChatCompletionsRequest"),
        "https://generativelanguage.googleapis.com/v1beta/openai",
        "bearer",
    )


GeminiChatConfigs._PROVIDER = "gemini"
GeminiChatConfigs._PROVIDER_ALIASES = ["gemini-api"]
GeminiChatConfigs._API_KEY_ENV = "GEMINI_API_KEY"


class GeminiCodeConfigs(ProviderConfig, Enum):
    CLI = (
        "query_cli",
        ["cli"],
        EndpointType.AGENTIC,
        LazyType("lionagi.providers.google.gemini_code.models:GeminiCodeRequest"),
    )


GeminiCodeConfigs._PROVIDER = "gemini_code"
GeminiCodeConfigs._PROVIDER_ALIASES = ["gemini-code", "gemini_cli", "gemini-cli"]


__all__ = ("GeminiChatConfigs", "GeminiCodeConfigs")
