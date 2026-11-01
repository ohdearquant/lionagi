# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from lionagi.service.connections._chat_schemas import OpenAIChatCompletionsRequest
from lionagi.service.connections.endpoint import Endpoint

# CONTEXT_WINDOWS is defined in ._config and re-exported here.
from ._config import GEMINI_CONTEXT_WINDOWS as CONTEXT_WINDOWS  # noqa: F401
from ._config import GeminiChatConfigs

__all__ = ("OpenAIChatCompletionsRequest", "GeminiChatEndpoint")


@GeminiChatConfigs.CHAT.register
class GeminiChatEndpoint(Endpoint):
    def __init__(self, config=None, **kwargs):
        if config is None:
            kwargs.setdefault("kwargs", {"model": "gemini-2.5-flash"})
            kwargs.setdefault("requires_tokens", True)
        super().__init__(config, **kwargs)
