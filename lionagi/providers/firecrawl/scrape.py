# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from ._config import FirecrawlConfigs
from ._schemas import OutputFormat

__all__ = ("FirecrawlScrapeRequest", "FirecrawlScrapeEndpoint")


class FirecrawlScrapeRequest(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
    )

    url: str = Field(..., description="The URL to scrape.")
    formats: list[OutputFormat] | None = Field(
        default=None,
        description="Output formats to return (markdown, html, rawHtml, links, screenshot).",
    )
    only_main_content: bool | None = Field(
        default=True,
        alias="onlyMainContent",
        description="Only return the main content of the page, excluding navs/footers.",
    )
    include_tags: list[str] | None = Field(
        default=None,
        alias="includeTags",
        description="HTML tags to include in extraction.",
    )
    exclude_tags: list[str] | None = Field(
        default=None,
        alias="excludeTags",
        description="HTML tags to exclude from extraction.",
    )
    wait_for: int | None = Field(
        default=None,
        alias="waitFor",
        description="Milliseconds to wait before scraping (for JS-rendered pages).",
    )
    timeout: int | None = Field(
        default=None,
        description="Timeout in milliseconds for the scrape request.",
    )


@FirecrawlConfigs.SCRAPE.register
class FirecrawlScrapeEndpoint(Endpoint):
    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            kwargs.setdefault("serialize_by_alias", True)
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)
