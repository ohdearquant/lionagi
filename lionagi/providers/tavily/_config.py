# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType


class TavilyConfigs(ProviderConfig, Enum):
    SEARCH = (
        "search",
        [],
        EndpointType.API,
        LazyType("lionagi.providers.tavily.search:TavilySearchRequest"),
        "https://api.tavily.com",
        "bearer",
    )
    EXTRACT = (
        "extract",
        [],
        EndpointType.API,
        LazyType("lionagi.providers.tavily.search:TavilyExtractRequest"),
        "https://api.tavily.com",
        "bearer",
    )


TavilyConfigs._PROVIDER = "tavily"
TavilyConfigs._PROVIDER_ALIASES = []
TavilyConfigs._API_KEY_ENV = "TAVILY_API_KEY"

__all__ = ("TavilyConfigs",)
