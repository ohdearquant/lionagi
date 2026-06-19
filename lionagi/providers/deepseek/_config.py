# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType


class DeepSeekConfigs(ProviderConfig, Enum):
    CHAT = (
        "chat/completions",
        ["chat"],
        EndpointType.API,
        LazyType("lionagi.providers.deepseek.chat:DeepseekChatCompletionsRequest"),
        "https://api.deepseek.com/v1",
        "bearer",
    )


DeepSeekConfigs._PROVIDER = "deepseek"
DeepSeekConfigs._PROVIDER_ALIASES = []
DeepSeekConfigs._API_KEY_ENV = "DEEPSEEK_API_KEY"

__all__ = ("DeepSeekConfigs",)
