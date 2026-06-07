# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType


class OllamaConfigs(ProviderConfig, Enum):
    CHAT = (
        "chat/completions",
        ["chat"],
        EndpointType.API,
        LazyType("lionagi.providers.ollama.chat.models:OpenAIChatCompletionsRequest"),
        "http://localhost:11434/v1",
        "none",
    )
    EMBED = (
        "embeddings",
        ["embed"],
        EndpointType.API,
        LazyType("lionagi.providers.ollama.embed.models:OllamaEmbedRequest"),
        "http://localhost:11434/api",
        "none",
    )
    GENERATE = (
        "generate",
        ["generate", "completion"],
        EndpointType.API,
        LazyType("lionagi.providers.ollama.generate.models:OllamaGenerateRequest"),
        "http://localhost:11434/api",
        "none",
    )


OllamaConfigs._PROVIDER = "ollama"
OllamaConfigs._PROVIDER_ALIASES = []

__all__ = ("OllamaConfigs",)
