# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from pydantic import BaseModel

from lionagi.service.connections.endpoint import Endpoint

from .._config import DeepSeekConfigs
from .models import DeepseekChatCompletionsRequest, normalize_deepseek_usage

CONTEXT_WINDOWS: dict[str, int] = {
    "deepseek-v4-pro": 1_000_000,
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4": 1_000_000,
    "deepseek-coder-v2": 128_000,
    "deepseek-chat": 1_000_000,
    "deepseek-reasoner": 1_000_000,
    "deepseek-v3": 128_000,
    "deepseek-r1": 64_000,
}


@DeepSeekConfigs.CHAT.register
class DeepseekChatEndpoint(Endpoint):
    def __init__(self, config=None, **kwargs):
        if config is None:
            from lionagi.config import settings

            kwargs.setdefault("api_key", settings.DEEPSEEK_API_KEY or "dummy-key-for-testing")
            kwargs.setdefault("kwargs", {"model": "deepseek-chat"})
            kwargs.setdefault("requires_tokens", True)
        super().__init__(config, **kwargs)

    def create_payload(
        self,
        request: dict | BaseModel,
        extra_headers: dict | None = None,
        **kwargs,
    ):
        payload, headers = super().create_payload(request, extra_headers, **kwargs)
        original_messages = payload.get("messages")
        req = DeepseekChatCompletionsRequest.model_validate(payload)
        payload = req.model_dump(exclude_none=True, mode="json")
        if original_messages is not None:
            payload["messages"] = original_messages
        return payload, headers

    async def _call(self, payload: dict, headers: dict, **kwargs):
        response = await super()._call(payload, headers, **kwargs)
        return normalize_deepseek_usage(response)
