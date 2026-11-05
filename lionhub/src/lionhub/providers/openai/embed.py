# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from lionagi.service.connections.endpoint import Endpoint

from ._config import OpenAIConfigs


class OpenAIEmbeddingRequest(BaseModel):
    """Request body for OpenAI-compatible embeddings endpoints."""

    model: str = Field(..., description="Embedding model name.")
    input: str | list[str] | list[int] | list[list[int]] = Field(
        ...,
        description="Input text or token array to embed.",
    )
    dimensions: int | None = Field(
        default=None,
        description="Optional dimensionality for supported embedding models.",
    )
    encoding_format: Literal["float", "base64"] | None = Field(
        default=None,
        description="Embedding vector encoding format.",
    )
    user: str | None = Field(
        default=None,
        description="End-user identifier for abuse monitoring.",
    )


__all__ = ("OpenAIEmbeddingRequest", "OpenaiEmbedEndpoint")


@OpenAIConfigs.EMBED.register
class OpenaiEmbedEndpoint(Endpoint):
    def __init__(self, config=None, **kwargs):
        if config is None:
            kwargs.setdefault("kwargs", {"model": "text-embedding-3-small"})
            kwargs.setdefault("requires_tokens", True)
        super().__init__(config, **kwargs)
