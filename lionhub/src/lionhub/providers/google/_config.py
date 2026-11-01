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
        LazyType("lionagi.service.connections._chat_schemas:OpenAIChatCompletionsRequest"),
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
        LazyType("lionhub.providers.google.gemini_code:GeminiCodeRequest"),
    )


GeminiCodeConfigs._PROVIDER = "gemini_code"
GeminiCodeConfigs._PROVIDER_ALIASES = ["gemini-code", "gemini_cli", "gemini-cli"]


GEMINI_CONTEXT_WINDOWS: dict[str, int] = {
    "gemini-3.8-flash": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.0-flash": 1_048_576,
}


# agy models expose ~1M-token context (verified against `agy models`).
GEMINI_CODE_CONTEXT_WINDOWS: dict[str, int] = {
    "gemini-4-argon": 1_048_576,
    # Matched as substrings, longest row first, and lookup_context_window falls back
    # across every provider's table, so these give a name containing them this window
    # on any provider unless a longer row matches it: the `gemini-4` and `argon`
    # aliases, the "Gemini 4 Argon (...)" display names, but also `gemini-4-flash`, or
    # `gemini-4.5-flash` on `gemini`; `gemini-2.5-pro-argon` keeps its provider's 2.5 Pro
    # window.
    # The windows follow these substrings, not resolve_agy_model's token rules, so
    # `gemini-4k-flash` reads the `gemini-4` row while it resolves to 3.5 Flash.
    "gemini-4": 1_048_576,
    "argon": 1_048_576,
    "gemini-3.8-flash": 1_048_576,
    "gemini-3.6-flash": 1_048_576,
    "gemini-3.5-flash": 1_048_576,
    "gemini-3.1-pro": 1_048_576,
    "gemini-3-flash-preview": 1_048_576,
    "gemini-3-pro-preview": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.5-pro": 2_097_152,
}


__all__ = (
    "GeminiChatConfigs",
    "GeminiCodeConfigs",
    "GEMINI_CONTEXT_WINDOWS",
    "GEMINI_CODE_CONTEXT_WINDOWS",
)
