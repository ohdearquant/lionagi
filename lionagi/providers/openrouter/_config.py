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
        LazyType("lionagi.providers.openrouter.chat:OpenRouterRequest"),
        "https://openrouter.ai/api/v1",
        "bearer",
    )


OpenRouterConfigs._PROVIDER = "openrouter"
OpenRouterConfigs._PROVIDER_ALIASES = ["open-router"]
OpenRouterConfigs._API_KEY_ENV = "OPENROUTER_API_KEY"

__all__ = ("OpenRouterConfigs",)
