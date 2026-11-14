# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from ._config import OutcrawlConfigs
from ._schemas import OutputFormat

__all__ = ("OutcrawlSearchRequest", "OutcrawlSearchEndpoint")


class OutcrawlSearchRequest(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
    )

    query: str = Field(..., description="The search query.")
    limit: int | None = Field(
        default=5,
        description="Max number of search hits to return.",
    )
    scrape: bool | None = Field(
        default=None,
        description="Also scrape each hit and return its content in the requested formats.",
    )
    formats: list[OutputFormat] | None = Field(
        default=None,
        description="Output formats for scraped hits (markdown, html, links).",
    )


@OutcrawlConfigs.SEARCH.register
class OutcrawlSearchEndpoint(Endpoint):
    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            kwargs.setdefault("serialize_by_alias", True)
            kwargs.setdefault("timeout", 120)
        super().__init__(config=config, **kwargs)
