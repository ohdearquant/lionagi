# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Exa findSimilar request model."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from lionagi.providers.exa.search import Contents
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from ._config import ExaConfigs


class ExaFindSimilarRequest(BaseModel):
    """Request body for Exa /findSimilar — find pages similar to a given URL."""

    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
    )

    url: str = Field(
        ...,
        description="The URL to find similar links to.",
    )
    num_results: int | None = Field(
        default=10,
        alias="numResults",
        description="Number of similar results to return.",
    )
    include_domains: list[str] | None = Field(
        default=None,
        alias="includeDomains",
        description="Restrict results to these domains.",
    )
    exclude_domains: list[str] | None = Field(
        default=None,
        alias="excludeDomains",
        description="Exclude results from these domains.",
    )
    start_crawl_date: str | None = Field(
        default=None,
        alias="startCrawlDate",
        description="ISO date lower bound on crawl date.",
    )
    end_crawl_date: str | None = Field(
        default=None,
        alias="endCrawlDate",
        description="ISO date upper bound on crawl date.",
    )
    start_published_date: str | None = Field(
        default=None,
        alias="startPublishedDate",
        description="ISO date lower bound on published date.",
    )
    end_published_date: str | None = Field(
        default=None,
        alias="endPublishedDate",
        description="ISO date upper bound on published date.",
    )
    exclude_source_domain: bool | None = Field(
        default=None,
        alias="excludeSourceDomain",
        description="If True, exclude results from the same domain as the input URL.",
    )
    contents: Contents | None = Field(
        default=None,
        description="Optional content retrieval options (text, highlights, summary).",
    )


__all__ = ("ExaFindSimilarRequest", "ExaFindSimilarEndpoint")


@ExaConfigs.FIND_SIMILAR.register
class ExaFindSimilarEndpoint(Endpoint):
    """Exa findSimilar endpoint — discover pages semantically similar to a URL."""

    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            kwargs.setdefault("serialize_by_alias", True)
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)
