# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from ._config import ExaConfigs


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


class LivecrawlType(str, Enum):
    never = "never"
    fallback = "fallback"
    always = "always"


class SearchType(str, Enum):
    keyword = "keyword"
    neural = "neural"
    auto = "auto"


class _ExaBase(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
    )


class ContentsText(_ExaBase):
    include_html_tags: bool | None = Field(default=False, alias="includeHtmlTags")
    max_characters: int | None = Field(default=None, alias="maxCharacters")


class ContentsHighlights(_ExaBase):
    highlights_per_url: int | None = Field(default=1, alias="highlightsPerUrl")
    num_sentences: int | None = Field(default=5, alias="numSentences")
    query: None | str = Field(default=None)


class ContentsSummary(_ExaBase):
    query: None | str = Field(default=None)


class ContentsExtras(_ExaBase):
    links: int | None = Field(default=None)
    image_links: int | None = Field(default=None, alias="imageLinks")


class Contents(_ExaBase):
    text: None | ContentsText = Field(default=None)
    highlights: None | ContentsHighlights = Field(default=None)
    summary: None | ContentsSummary = Field(default=None)
    livecrawl: None | LivecrawlType = Field(default=LivecrawlType.never)
    livecrawl_timeout: int | None = Field(
        default=10000,
        alias="livecrawlTimeout",
        description="Timeout in ms for livecrawling.",
    )
    subpages: int | None = Field(default=None)
    subpage_target: None | str | list[str] = Field(
        default=None,
        alias="subpageTarget",
        description="Target subpage(s) to crawl, e.g. 'cited papers'.",
    )
    extras: None | ContentsExtras = Field(default=None)


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


__all__ = ("ExaSearchEndpoint",)


@ExaConfigs.SEARCH.register
class ExaSearchEndpoint(Endpoint):
    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            kwargs.setdefault("serialize_by_alias", True)
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)
