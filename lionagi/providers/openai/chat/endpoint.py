# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from pydantic import BaseModel

from lionagi.service.connections.endpoint import Endpoint

from .._config import OpenAIConfigs


@OpenAIConfigs.CHAT.register
class OpenaiChatEndpoint(Endpoint):
    def __init__(self, config=None, **kwargs):
        if config is None:
            kwargs.setdefault("kwargs", {"model": "gpt-4.1-mini"})
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
        # Convert system role to developer role for reasoning models
        if "messages" in payload and payload["messages"]:
            if payload["messages"][0].get("role") == "system":
                payload["messages"][0]["role"] = "developer"

        return (payload, headers)
