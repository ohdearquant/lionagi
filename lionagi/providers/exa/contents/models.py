# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Exa contents extraction request model."""

from pydantic import BaseModel, ConfigDict, Field

from lionagi.providers.exa.search.models import Contents


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


__all__ = ("ExaContentsRequest",)
