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
        LazyType("lionhub.providers.perplexity.chat:PerplexityChatRequest"),
        "https://api.perplexity.ai",
        "bearer",
    )


PerplexityConfigs._PROVIDER = "perplexity"
PerplexityConfigs._PROVIDER_ALIASES = []
PerplexityConfigs._API_KEY_ENV = "PERPLEXITY_API_KEY"


CONTEXT_WINDOWS: dict[str, int] = {
    "sonar-pro": 200_000,
    "sonar": 128_000,
}

__all__ = ("PerplexityConfigs", "CONTEXT_WINDOWS")
