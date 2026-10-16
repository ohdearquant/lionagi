# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Lattice `POST /v1/embeddings` request model."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from lionagi.service.connections.endpoint import Endpoint

from ._config import LatticeConfigs, LatticeRedirectsRefused, _setup_lattice_endpoint

__all__ = ("LatticeEmbedEndpoint", "LatticeEmbedRequest")


class LatticeEmbedRequest(BaseModel):
    """Request body for Lattice `POST /v1/embeddings`. Vectors come back L2-normalized."""

    model: str = Field(..., description="The model id the server serves.")
    input: str | list[str | dict[str, Any]] = Field(
        ...,
        description=(
            "Text, or a list of texts and inline images "
            '(`{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}`).'
        ),
    )
    pooling: Literal["mean_visual", "last_token"] | None = Field(
        default=None,
        description="How a vision-language checkpoint pools an item; the server default is mean_visual.",
    )


@LatticeConfigs.EMBED.register
class LatticeEmbedEndpoint(LatticeRedirectsRefused, Endpoint):
    """Embeddings from a running `lattice serve`; no API key, and no redirect is followed."""

    def __init__(self, config=None, **kwargs):
        config = _setup_lattice_endpoint(config, kwargs, LatticeEmbedRequest)
        super().__init__(config, **kwargs)
