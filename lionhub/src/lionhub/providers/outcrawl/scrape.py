# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from ._config import OutcrawlConfigs
from ._schemas import OutputFormat

__all__ = ("OutcrawlScrapeRequest", "OutcrawlScrapeEndpoint")


class OutcrawlScrapeRequest(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
    )

    url: str = Field(..., description="The URL to scrape.")
    formats: list[OutputFormat] | None = Field(
        default=None,
        description="Output formats to return (markdown, html, links). Defaults to markdown.",
    )
    only_main_content: bool | None = Field(
        default=True,
        alias="onlyMainContent",
        description="Only return the main content of the page, excluding navs/footers.",
    )


@OutcrawlConfigs.SCRAPE.register
class OutcrawlScrapeEndpoint(Endpoint):
    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            kwargs.setdefault("serialize_by_alias", True)
            kwargs.setdefault("timeout", 120)
        super().__init__(config=config, **kwargs)
