# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from lionagi.service.connections.endpoint import Endpoint

from ._config import GroqConfigs

CONTEXT_WINDOWS: dict[str, int] = {
    "llama-3.3-70b-versatile": 128_000,
    "llama-3.1-8b-instant": 128_000,
    "mixtral-8x7b-32768": 32_768,
    "gemma2-9b-it": 8_192,
}


@GroqConfigs.CHAT.register
class GroqChatEndpoint(Endpoint):
    def __init__(self, config=None, **kwargs):
        if config is None:
            kwargs.setdefault("kwargs", {"model": "llama-3.3-70b-versatile"})
            kwargs.setdefault("requires_tokens", True)
        super().__init__(config, **kwargs)
