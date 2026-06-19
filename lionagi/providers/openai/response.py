# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from lionagi.service.connections.endpoint import Endpoint

from ._config import OpenAIConfigs


class OpenAIResponsesRequest(BaseModel):
    """Request body for OpenAI Responses API (POST /v1/responses); explicit schema filters internal kwargs."""

    model: str | None = Field(default=None, description="Model name.")
    input: str | list[Any] | None = Field(
        default=None,
        description="Input text or structured items.",
    )
    instructions: str | None = None
    previous_response_id: str | None = None
    prompt: str | dict[str, Any] | None = None
    store: bool | None = None
    stream: bool | None = None
    stream_options: dict[str, Any] | None = None
    include: list[str] | None = None
    metadata: dict[str, Any] | None = None
    user: str | None = None
    safety_identifier: str | None = None

    background: bool | None = None
    conversation: str | dict[str, Any] | None = None
    context_management: dict[str, Any] | None = None
    max_output_tokens: int | None = None
    max_tool_calls: int | None = None
    temperature: float | None = None
    top_logprobs: int | None = None
    top_p: float | None = None
    truncation: Literal["auto", "disabled"] | None = None
    service_tier: str | None = None
    prompt_cache_key: str | None = None
    prompt_cache_retention: str | None = None

    reasoning: dict[str, Any] | None = None
    text: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | dict[str, Any] | None = None
    parallel_tool_calls: bool | None = None


__all__ = ("OpenAIResponsesRequest", "OpenaiResponseEndpoint")


@OpenAIConfigs.RESPONSE.register
class OpenaiResponseEndpoint(Endpoint):
    def __init__(self, config=None, **kwargs):
        if config is None:
            kwargs.setdefault("requires_tokens", True)
        super().__init__(config, **kwargs)
