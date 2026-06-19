# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from lionagi.service.connections.endpoint import Endpoint

from ._config import NvidiaNimConfigs


class NvidiaNimEmbeddingRequest(BaseModel):
    """Request body for NVIDIA NIM OpenAI-compatible embeddings."""

    model: str = Field(..., description="NVIDIA NIM embedding model name.")
    input: str | list[str] = Field(..., description="Input text to embed.")
    encoding_format: Literal["float", "base64"] | None = Field(default=None)
    dimensions: int | None = Field(default=None)
    input_type: Literal["query", "passage"] | None = Field(
        default=None,
        description="Optional NIM embedding input type.",
    )
    truncate: Literal["NONE", "START", "END"] | None = Field(default=None)
    user: str | None = Field(default=None)


__all__ = ("NvidiaNimEmbeddingRequest",)


@NvidiaNimConfigs.EMBED.register
class NvidiaNimEmbedEndpoint(Endpoint):
    """NVIDIA NIM embedding endpoint."""

    def __init__(self, config=None, **kwargs):
        if config is None:
            kwargs.setdefault("kwargs", {"model": "nvidia/nv-embed-v1"})
        super().__init__(config, **kwargs)
