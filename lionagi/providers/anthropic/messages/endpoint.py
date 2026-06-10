# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from pydantic import BaseModel

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from .._config import AnthropicConfigs

CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-sonnet-4-5": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-3-opus": 200_000,
}


@AnthropicConfigs.MESSAGES.register
class AnthropicMessagesEndpoint(Endpoint):
    def __init__(
        self,
        config: EndpointConfig = None,
        **kwargs,
    ):
        if config is None:
            from lionagi.config import settings

            kwargs.setdefault("api_key", settings.ANTHROPIC_API_KEY or "dummy-key-for-testing")
            kwargs.setdefault("default_headers", {"anthropic-version": "2023-06-01"})
        super().__init__(config, **kwargs)

    def create_payload(
        self,
        request: dict | BaseModel,
        extra_headers: dict | None = None,
        **kwargs,
    ):
        # Extract system message before validation if present
        request_dict = request if isinstance(request, dict) else request.model_dump()
        system = None

        if "messages" in request_dict and request_dict["messages"]:
            first_message = request_dict["messages"][0]
            if first_message.get("role") == "system":
                system = first_message["content"]
                # Remove system message before validation
                request_dict["messages"] = request_dict["messages"][1:]
                request = request_dict

        payload, headers = super().create_payload(request, extra_headers=extra_headers, **kwargs)

        # Remove api_key from payload if present
        payload.pop("api_key", None)

        if "cache_control" in payload:
            cache_control = payload.pop("cache_control")
            if cache_control:
                cache_control = {"type": "ephemeral"}
                last_message = payload["messages"][-1]["content"]
                if isinstance(last_message, str):
                    last_message = {
                        "type": "text",
                        "text": last_message,
                        "cache_control": cache_control,
                    }
                elif isinstance(last_message, list) and isinstance(last_message[-1], dict):
                    last_message[-1]["cache_control"] = cache_control
                payload["messages"][-1]["content"] = (
                    [last_message] if not isinstance(last_message, list) else last_message
                )

        # If we extracted a system message earlier, add it to payload
        if system:
            system = [{"type": "text", "text": system}]
            payload["system"] = system

        return (payload, headers)
