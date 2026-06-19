# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Perplexity Sonar chat endpoint (real-time web search via the Sonar API)."""

from lionagi.service.connections.endpoint import Endpoint

from .._config import PerplexityConfigs

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
