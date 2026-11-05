# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType


class NvidiaNimConfigs(ProviderConfig, Enum):
    CHAT = (
        "chat/completions",
        ["chat"],
        EndpointType.API,
        LazyType("lionagi.service.connections._chat_schemas:OpenAIChatCompletionsRequest"),
        "https://integrate.api.nvidia.com/v1",
        "bearer",
    )
    EMBED = (
        "embeddings",
        ["embed"],
        EndpointType.API,
        LazyType("lionhub.providers.nvidia_nim.embed:NvidiaNimEmbeddingRequest"),
        "https://integrate.api.nvidia.com/v1",
        "bearer",
    )


NvidiaNimConfigs._PROVIDER = "nvidia_nim"
NvidiaNimConfigs._PROVIDER_ALIASES = ["nvidia", "nim"]
NvidiaNimConfigs._API_KEY_ENV = "NVIDIA_NIM_API_KEY"


CONTEXT_WINDOWS: dict[str, int] = {
    "nemotron-ultra": 131_072,
    "nemotron-super": 131_072,
    "llama-4-scout": 10_485_760,
    "llama-4-maverick": 1_048_576,
    "llama-3": 128_000,
}

__all__ = ("NvidiaNimConfigs", "CONTEXT_WINDOWS")
