# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""OpenRouter models — extends OpenAI-compatible request with reasoning control."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from lionagi.service.connections._chat_schemas import OpenAIChatCompletionsRequest
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

# CONTEXT_WINDOWS is defined in ._config and re-exported here.
from ._config import CONTEXT_WINDOWS, OpenRouterConfigs  # noqa: F401

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
