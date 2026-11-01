# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType


class OutcrawlConfigs(ProviderConfig, Enum):
    SCRAPE = (
        "v1/scrape",
        ["scrape"],
        EndpointType.API,
        LazyType("lionhub.providers.outcrawl.scrape:OutcrawlScrapeRequest"),
        "https://api.outcrawl.ai",
        "bearer",
    )
    SEARCH = (
        "v1/search",
        ["search"],
        EndpointType.API,
        LazyType("lionhub.providers.outcrawl.search:OutcrawlSearchRequest"),
        "https://api.outcrawl.ai",
        "bearer",
    )


OutcrawlConfigs._PROVIDER = "outcrawl"
OutcrawlConfigs._PROVIDER_ALIASES = []
OutcrawlConfigs._API_KEY_ENV = "OUTCRAWL_API_KEY"

__all__ = ("OutcrawlConfigs",)
