# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType


class PerplexityConfigs(ProviderConfig, Enum):
    CHAT = (
        "chat/completions",
        ["chat"],
        EndpointType.API,
        LazyType("lionagi.providers.perplexity.chat.models:PerplexityChatRequest"),
        "https://api.perplexity.ai",
        "bearer",
    )


PerplexityConfigs._PROVIDER = "perplexity"
PerplexityConfigs._PROVIDER_ALIASES = []
PerplexityConfigs._API_KEY_ENV = "PERPLEXITY_API_KEY"

__all__ = ("PerplexityConfigs",)
