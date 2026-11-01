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
        LazyType("lionhub.providers.deepseek.chat:DeepseekChatCompletionsRequest"),
        "https://api.deepseek.com/v1",
        "bearer",
    )


DeepSeekConfigs._PROVIDER = "deepseek"
DeepSeekConfigs._PROVIDER_ALIASES = []
DeepSeekConfigs._API_KEY_ENV = "DEEPSEEK_API_KEY"


CONTEXT_WINDOWS: dict[str, int] = {
    "deepseek-v4-pro": 1_000_000,
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4": 1_000_000,
    "deepseek-coder-v2": 128_000,
    "deepseek-chat": 1_000_000,
    "deepseek-reasoner": 1_000_000,
    "deepseek-v3": 128_000,
    "deepseek-r1": 64_000,
}

__all__ = ("DeepSeekConfigs", "CONTEXT_WINDOWS")
