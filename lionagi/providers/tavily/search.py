# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from ._config import TavilyConfigs


class SearchDepth(str, Enum):
    basic = "basic"
    advanced = "advanced"


class SearchTopic(str, Enum):
    general = "general"
    news = "news"
    finance = "finance"


class TavilySearchRequest(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
    )

    query: str = Field(..., description="The search query.")
    search_depth: SearchDepth | None = Field(
        default=SearchDepth.basic,
        description="'basic' for fast results, 'advanced' for deeper search.",
    )
    topic: SearchTopic | None = Field(
        default=SearchTopic.general,
        description="Category of the search: general, news, or finance.",
    )
    max_results: int | None = Field(
        default=5,
        description="Max number of search results to return (1-20).",
    )
    include_domains: list[str] | None = Field(
        default=None,
        description="Restrict search to these domains.",
    )
    exclude_domains: list[str] | None = Field(
        default=None,
        description="Exclude these domains from search.",
    )
    include_answer: bool | None = Field(
        default=False,
        description="Include a short LLM-generated answer.",
    )
    include_raw_content: bool | None = Field(
        default=False,
        description="Include cleaned and parsed HTML content of each result.",
    )
    include_images: bool | None = Field(
        default=False,
        description="Include query-related images in results.",
    )
    days: int | None = Field(
        default=None,
        description="Number of days back to search (news topic only).",
    )


class TavilyExtractRequest(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
    )

    urls: list[str] = Field(..., description="List of URLs to extract content from.")


__all__ = ("TavilySearchEndpoint", "TavilyExtractEndpoint")


@TavilyConfigs.SEARCH.register
class TavilySearchEndpoint(Endpoint):
    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)


@TavilyConfigs.EXTRACT.register
class TavilyExtractEndpoint(Endpoint):
    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)
