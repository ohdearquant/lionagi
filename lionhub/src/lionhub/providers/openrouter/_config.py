# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType


class OpenRouterConfigs(ProviderConfig, Enum):
    CHAT = (
        "chat/completions",
        ["chat", "chat/completions"],
        EndpointType.API,
        LazyType("lionhub.providers.openrouter.chat:OpenRouterRequest"),
        "https://openrouter.ai/api/v1",
        "bearer",
    )


OpenRouterConfigs._PROVIDER = "openrouter"
OpenRouterConfigs._PROVIDER_ALIASES = ["open-router"]
OpenRouterConfigs._API_KEY_ENV = "OPENROUTER_API_KEY"


CONTEXT_WINDOWS: dict[str, int] = {
    "google/gemini-3.8-flash": 1_048_576,
    "anthropic/claude-fable-5.1": 1_000_000,
    "anthropic/claude-opus-5.5": 1_000_000,
    "anthropic/claude-sonnet-5.5": 1_000_000,
    "anthropic/claude-haiku-5.5": 1_000_000,
    "openai/gpt-6.1-sol": 1_050_000,
    "openai/gpt-6.1-terra": 1_050_000,
    "openai/gpt-6-astra": 1_050_000,
    "openai/gpt-6-sol": 1_050_000,
    "openai/gpt-6-luna": 1_050_000,
    "google/gemini-2.5-flash": 1_048_576,
    "google/gemini-2.5-pro": 1_048_576,
    "anthropic/claude-opus-4-5": 200_000,
    "anthropic/claude-sonnet-4-5": 200_000,
    "openai/gpt-4.1": 1_000_000,
    "meta-llama/llama-3.3-70b-instruct": 128_000,
}

__all__ = ("OpenRouterConfigs", "CONTEXT_WINDOWS")
