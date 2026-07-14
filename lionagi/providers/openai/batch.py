# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from lionagi.service.connections.endpoint import Endpoint

from ._config import OpenAIConfigs


class OpenAIBatchRequest(BaseModel):
    """Request body for creating an OpenAI batch."""

    input_file_id: str
    endpoint: Literal[
        "/v1/responses",
        "/v1/chat/completions",
        "/v1/embeddings",
        "/v1/completions",
        "/v1/moderations",
        "/v1/images/generations",
        "/v1/images/edits",
        "/v1/videos",
    ]
    completion_window: Literal["24h"]
    metadata: dict[str, str] | None = None
    output_expires_after: dict[str, Any] | None = None


__all__ = ("OpenAIBatchRequest", "OpenaiBatchEndpoint")


@OpenAIConfigs.BATCH.register
class OpenaiBatchEndpoint(Endpoint):
    """OpenAI batch creation endpoint."""
