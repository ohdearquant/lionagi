# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Exa contents extraction request model."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from lionagi.providers.exa._schemas import Contents
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from ._config import ExaConfigs


class ExaContentsRequest(BaseModel):
    """Request body for Exa /contents — extract page content from known URLs."""

    model_config = ConfigDict(
        populate_by_name=True,
        serialize_by_alias=True,
    )

    ids: list[str] = Field(
        ...,
        description="List of document IDs (URLs) to retrieve contents for.",
    )
    contents: Contents | None = Field(
        default=None,
        description="Content options: text, highlights, summary, livecrawl, etc.",
    )


__all__ = ("ExaContentsRequest", "ExaContentsEndpoint")


@ExaConfigs.CONTENTS.register
class ExaContentsEndpoint(Endpoint):
    """Exa contents endpoint — extract page text, highlights, and summaries from URLs."""

    def __init__(self, config: EndpointConfig = None, **kwargs):
        if config is None:
            kwargs.setdefault("serialize_by_alias", True)
            kwargs.setdefault("timeout", 120)
            kwargs.setdefault("max_retries", 3)
        super().__init__(config=config, **kwargs)
