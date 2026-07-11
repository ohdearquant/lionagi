# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pydantic import BaseModel

from lionagi.service.connections.endpoint import Endpoint

from ._chat_schemas import OpenAIChatCompletionsRequest, uses_developer_messages
from ._config import OpenAIConfigs

__all__ = ("OpenAIChatCompletionsRequest", "OpenaiChatEndpoint")


@OpenAIConfigs.CHAT.register
class OpenaiChatEndpoint(Endpoint):
    def __init__(self, config=None, **kwargs):
        if config is None:
            from lionagi.config import settings

            kwargs.setdefault("kwargs", {"model": settings.OPENAI_DEFAULT_MODEL})
            kwargs.setdefault("requires_tokens", True)
        super().__init__(config, **kwargs)

    def create_payload(
        self,
        request: dict | BaseModel,
        extra_headers: dict | None = None,
        **kwargs,
    ):
        """Override to handle model-specific parameter filtering."""
        payload, headers = super().create_payload(request, extra_headers, **kwargs)
        messages = payload.get("messages")
        if messages and uses_developer_messages(payload.get("model")):
            payload["messages"] = [
                {**message, "role": "developer"}
                if message.get("role") == "system"
                else dict(message)
                for message in messages
            ]

        return (payload, headers)
