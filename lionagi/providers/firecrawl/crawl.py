# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Firecrawl crawl request model."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from lionagi.providers.firecrawl._schemas import OutputFormat
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from ._config import FirecrawlConfigs


class CrawlScrapeOptions(BaseModel):
    """Per-page scrape options applied during a crawl."""

    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
    )

    formats: list[OutputFormat] | None = Field(
        default=None,
        description="Output formats for each scraped page.",
    )
    only_main_content: bool | None = Field(
        default=True,
        alias="onlyMainContent",
        description="Strip navbars/footers and return only main content.",
    )
    include_tags: list[str] | None = Field(
        default=None,
        alias="includeTags",
        description="HTML tags to include during extraction.",
    )
    exclude_tags: list[str] | None = Field(
        default=None,
        alias="excludeTags",
        description="HTML tags to exclude during extraction.",
    )
    wait_for: int | None = Field(
        default=None,
        alias="waitFor",
        description="Milliseconds to wait before scraping each page.",
    )


class FirecrawlCrawlRequest(BaseModel):
    """Request body for Firecrawl /v1/crawl — async full-site crawl; returns jobId."""

    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
    )

    url: str = Field(
        ...,
        description="The base URL to start crawling from.",
    )
    exclude_paths: list[str] | None = Field(
        default=None,
        alias="excludePaths",
        description="URL path patterns to exclude from the crawl.",
    )
    include_paths: list[str] | None = Field(
        default=None,
        alias="includePaths",
        description="Only crawl URL paths matching these patterns.",
    )
    max_depth: int | None = Field(
        default=None,
        alias="maxDepth",
        description="Maximum link-follow depth from the start URL.",
    )
    ignore_sitemap: bool | None = Field(
        default=None,
        alias="ignoreSitemap",
        description="Skip the site's sitemap when crawling.",
    )
    limit: int | None = Field(
        default=None,
        description="Maximum number of pages to crawl.",
    )
    allow_backward_links: bool | None = Field(
        default=None,
        alias="allowBackwardLinks",
        description="Allow crawling links that point to parent pages.",
    )
    allow_external_links: bool | None = Field(
        default=None,
        alias="allowExternalLinks",
        description="Allow crawling links that go to external domains.",
    )
    webhook: str | None = Field(
        default=None,
        description="URL to receive a POST webhook when crawl completes.",
    )
    scrape_options: CrawlScrapeOptions | None = Field(
        default=None,
        alias="scrapeOptions",
        description="Per-page scraping options.",
    )


__all__ = ("FirecrawlCrawlRequest", "CrawlScrapeOptions", "FirecrawlCrawlEndpoint")


@FirecrawlConfigs.CRAWL.register
class FirecrawlCrawlEndpoint(Endpoint):
    """Firecrawl crawl endpoint — kick off an async full-site crawl job."""

    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            kwargs.setdefault("serialize_by_alias", True)
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)
