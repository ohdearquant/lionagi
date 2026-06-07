# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from enum import Enum

from lionagi.service.connections.provider_config import LazyType, ProviderConfig
from lionagi.service.connections.registry import EndpointType


class FirecrawlConfigs(ProviderConfig, Enum):
    SCRAPE = (
        "v1/scrape",
        ["scrape"],
        EndpointType.API,
        LazyType("lionagi.providers.firecrawl.scrape.models:FirecrawlScrapeRequest"),
        "https://api.firecrawl.dev",
        "bearer",
    )
    MAP = (
        "v1/map",
        ["map"],
        EndpointType.API,
        LazyType("lionagi.providers.firecrawl.map.models:FirecrawlMapRequest"),
        "https://api.firecrawl.dev",
        "bearer",
    )
    CRAWL = (
        "v1/crawl",
        ["crawl"],
        EndpointType.API,
        LazyType("lionagi.providers.firecrawl.crawl.models:FirecrawlCrawlRequest"),
        "https://api.firecrawl.dev",
        "bearer",
    )


FirecrawlConfigs._PROVIDER = "firecrawl"
FirecrawlConfigs._PROVIDER_ALIASES = []

__all__ = ("FirecrawlConfigs",)
