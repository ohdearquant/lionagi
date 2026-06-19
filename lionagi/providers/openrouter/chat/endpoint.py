# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from .._config import OpenRouterConfigs

__all__ = ("OpenRouterEndpoint",)

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
