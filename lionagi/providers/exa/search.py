# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from enum import Enum

from pydantic import Field

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from ._config import ExaConfigs
from ._schemas import Contents, _ExaBase


class SearchCategory(str, Enum):
    company = "company"
    research_paper = "research paper"
    news = "news"
    pdf = "pdf"
    github = "github"
    tweet = "tweet"
    personal_site = "personal site"
    linkedin_profile = "linkedin profile"
    financial_report = "financial report"


class SearchType(str, Enum):
    keyword = "keyword"
    neural = "neural"
    auto = "auto"


class ExaSearchRequest(_ExaBase):
    query: str = Field(..., description="What to search for.")
    category: None | SearchCategory = Field(default=None)
    type: None | SearchType = Field(default=None)
    use_autoprompt: None | bool = Field(
        default=False,
        alias="useAutoprompt",
        description="Auto-optimize query (neural/auto search only).",
    )
    num_results: int | None = Field(default=10, alias="numResults")
    include_domains: None | list[str] = Field(default=None, alias="includeDomains")
    exclude_domains: None | list[str] = Field(default=None, alias="excludeDomains")
    start_crawl_date: None | str = Field(
        default=None,
        alias="startCrawlDate",
        description="ISO date, e.g. '2023-01-01T00:00:00.000Z'.",
    )
    end_crawl_date: None | str = Field(default=None, alias="endCrawlDate")
    start_published_date: None | str = Field(default=None, alias="startPublishedDate")
    end_published_date: None | str = Field(default=None, alias="endPublishedDate")
    include_text: None | list[str] = Field(
        default=None,
        alias="includeText",
        description="Strings that must appear in results. One string, max 5 words.",
    )
    exclude_text: None | list[str] = Field(
        default=None,
        alias="excludeText",
        description="Strings that must NOT appear. One string, max 5 words.",
    )
    contents: None | Contents = Field(default=None)


__all__ = ("ExaSearchRequest", "ExaSearchEndpoint")


@ExaConfigs.SEARCH.register
class ExaSearchEndpoint(Endpoint):
    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            kwargs.setdefault("serialize_by_alias", True)
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)
