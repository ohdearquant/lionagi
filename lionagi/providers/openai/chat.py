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
    caches_system_prefix = True

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
        request = (
            dict(request) if isinstance(request, dict) else request.model_dump(exclude_none=True)
        )
        system_blocks = request.pop("system_blocks", None)
        if "system_blocks" in kwargs:
            system_blocks = kwargs.pop("system_blocks")
        payload, headers = super().create_payload(request, extra_headers, **kwargs)
        messages = payload.get("messages")
        if self.places_prompt_cache and system_blocks and messages:

            def blocks(content):
                if isinstance(content, str | dict):
                    content = [content]
                if not isinstance(content, list | tuple):
                    return []
                result = []
                for item in content:
                    if isinstance(item, str):
                        item = {"type": "text", "text": item}
                    if not isinstance(item, dict) or item.get("type") != "text":
                        raise ValueError("OpenAI system blocks must be text")
                    if isinstance(item.get("text"), str) and item["text"].strip():
                        result.append(dict(item))
                return result

            stable = blocks(system_blocks)
            if stable:
                messages = list(messages)
                system = {"role": "system"}
                if messages[0].get("role") == "system":
                    system = dict(messages.pop(0))
                    stable = blocks(system.get("content")) + stable
                system["content"] = stable
                messages.insert(0, system)
                payload["messages"] = messages
        if messages and uses_developer_messages(payload.get("model")):
            payload["messages"] = [
                {**message, "role": "developer"}
                if message.get("role") == "system"
                else dict(message)
                for message in messages
            ]

        return (payload, headers)
