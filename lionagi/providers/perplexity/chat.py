# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Perplexity Sonar chat endpoint (real-time web search via the Sonar API)."""

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from lionagi.service.connections.endpoint import Endpoint

from ._config import PerplexityConfigs


class PerplexityModels(str, Enum):
    SONAR = "sonar"
    SONAR_PRO = "sonar-pro"
    SONAR_REASONING = "sonar-reasoning"
    SONAR_REASONING_PRO = "sonar-reasoning-pro"
    SONAR_DEEP_RESEARCH = "sonar-deep-research"


class PerplexityMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class WebSearchOptions(BaseModel):
    search_context_size: Literal["low", "medium", "high"] = Field(
        default="low",
        description="How much search context to retrieve: low (cheaper), "
        "medium (balanced), high (comprehensive).",
    )


class PerplexityChatRequest(BaseModel):
    """Request body for Perplexity Chat Completions API."""

    model: PerplexityModels = Field(default=PerplexityModels.SONAR)
    messages: list[PerplexityMessage] = Field(..., description="Conversation messages.")
    search_mode: Literal["default", "academic"] | None = Field(
        default=None,
        description="'academic' restricts to scholarly sources.",
    )
    frequency_penalty: float | None = Field(default=1, ge=0, le=2.0)
    presence_penalty: float | None = Field(default=None, ge=0, le=2.0)
    max_tokens: int | None = Field(default=None)
    return_related_questions: bool | None = Field(default=False)
    search_domain_filter: list[Any] | None = Field(
        default=None,
        description="Domains to include or exclude (prefix with '-'). Max 10.",
    )
    search_recency_filter: str | None = Field(
        default=None,
        description="Time filter: 'month', 'week', 'day', or 'hour'.",
    )
    temperature: float | None = Field(default=None, ge=0.0, lt=2.0)
    top_k: int | None = Field(default=None, ge=0, le=2048)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)


CONTEXT_WINDOWS: dict[str, int] = {
    "sonar-pro": 200_000,
    "sonar": 128_000,
}

__all__ = ("PerplexityChatEndpoint",)


@PerplexityConfigs.CHAT.register
class PerplexityChatEndpoint(Endpoint):
    def __init__(self, config=None, **kwargs):
        if config is None:
            kwargs.setdefault("kwargs", {"model": "sonar"})
            kwargs.setdefault("requires_tokens", True)
        super().__init__(config, **kwargs)
