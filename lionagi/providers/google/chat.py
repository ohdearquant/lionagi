# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from lionagi.providers.openai._chat_schemas import OpenAIChatCompletionsRequest
from lionagi.service.connections.endpoint import Endpoint

from ._config import GeminiChatConfigs

__all__ = ("OpenAIChatCompletionsRequest", "GeminiChatEndpoint")


CONTEXT_WINDOWS: dict[str, int] = {
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.0-flash": 1_048_576,
}


@GeminiChatConfigs.CHAT.register
class GeminiChatEndpoint(Endpoint):
    def __init__(self, config=None, **kwargs):
        if config is None:
            kwargs.setdefault("kwargs", {"model": "gemini-2.5-flash"})
            kwargs.setdefault("requires_tokens", True)
        super().__init__(config, **kwargs)
