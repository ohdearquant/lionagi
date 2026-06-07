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
    """OpenRouter API endpoint with reasoning control.

    Extends the standard OpenAI-compatible endpoint with:
    - reasoning effort control (none/low/medium/high)
    - reasoning token inclusion in response metadata

    Note: CONTEXT_WINDOWS lists common models; the actual context window depends
    on the underlying model routed through OpenRouter.
    """

    def __init__(
        self,
        config: EndpointConfig | None = None,
        **kwargs,
    ):
        if config is None:
            from lionagi.config import settings

            kwargs.setdefault("api_key", settings.OPENROUTER_API_KEY or "dummy-key-for-testing")
            kwargs.setdefault("kwargs", {"model": "google/gemini-2.5-flash"})
            kwargs.setdefault("requires_tokens", True)
        super().__init__(config=config, **kwargs)
