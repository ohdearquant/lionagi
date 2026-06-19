# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType


class ExaConfigs(ProviderConfig, Enum):
    SEARCH = (
        "search",
        [],
        EndpointType.API,
        LazyType("lionagi.providers.exa.search:ExaSearchRequest"),
        "https://api.exa.ai",
        "x-api-key",
    )
    CONTENTS = (
        "contents",
        ["get_contents"],
        EndpointType.API,
        LazyType("lionagi.providers.exa.contents:ExaContentsRequest"),
        "https://api.exa.ai",
        "x-api-key",
    )
    FIND_SIMILAR = (
        "findSimilar",
        ["similar", "find_similar"],
        EndpointType.API,
        LazyType("lionagi.providers.exa.find_similar:ExaFindSimilarRequest"),
        "https://api.exa.ai",
        "x-api-key",
    )


ExaConfigs._PROVIDER = "exa"
ExaConfigs._PROVIDER_ALIASES = []
ExaConfigs._API_KEY_ENV = "EXA_API_KEY"

__all__ = ("ExaConfigs",)
