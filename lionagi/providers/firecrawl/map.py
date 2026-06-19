# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from ._config import FirecrawlConfigs

__all__ = ("FirecrawlMapRequest", "FirecrawlMapEndpoint")


class FirecrawlMapRequest(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
    )

    url: str = Field(..., description="The base URL to map.")
    search: str | None = Field(
        default=None,
        description="Optional search term to filter mapped URLs.",
    )
    ignore_sitemap: bool | None = Field(
        default=None,
        alias="ignoreSitemap",
        description="Ignore the website sitemap when crawling.",
    )
    include_subdomains: bool | None = Field(
        default=None,
        alias="includeSubdomains",
        description="Include subdomains of the website.",
    )
    limit: int | None = Field(
        default=None,
        description="Maximum number of links to return.",
    )


@FirecrawlConfigs.MAP.register
class FirecrawlMapEndpoint(Endpoint):
    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            kwargs.setdefault("serialize_by_alias", True)
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)
