# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""OpenRouter models — extends OpenAI-compatible request with reasoning control."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from lionagi.providers.openai._chat_schemas import OpenAIChatCompletionsRequest
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from ._config import OpenRouterConfigs

__all__ = ("ReasoningConfig", "OpenRouterRequest", "OpenRouterEndpoint")


class ReasoningConfig(BaseModel):
    effort: Literal["none", "low", "medium", "high"] = "none"


class OpenRouterRequest(OpenAIChatCompletionsRequest):
    reasoning: ReasoningConfig | dict[str, Any] | None = Field(
        default=None,
        description="Reasoning/thinking config. Set {'effort':'none'} to disable thinking.",
    )
    include_reasoning: bool | None = Field(
        default=None,
        description="Whether to include reasoning tokens in response.",
    )


CONTEXT_WINDOWS: dict[str, int] = {
    "google/gemini-2.5-flash": 1_048_576,
    "google/gemini-2.5-pro": 1_048_576,
    "anthropic/claude-opus-4-5": 200_000,
    "anthropic/claude-sonnet-4-5": 200_000,
    "openai/gpt-4.1": 1_000_000,
    "meta-llama/llama-3.3-70b-instruct": 128_000,
}


@OpenRouterConfigs.CHAT.register
class OpenRouterEndpoint(Endpoint):
    """OpenRouter chat endpoint; adds reasoning effort control over the OpenAI-compatible base."""

    def __init__(
        self,
        config: EndpointConfig | None = None,
        **kwargs,
    ):
        if config is None:
            kwargs.setdefault("kwargs", {"model": "google/gemini-2.5-flash"})
            kwargs.setdefault("requires_tokens", True)
        super().__init__(config=config, **kwargs)
