# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""DeepSeek chat completions models; OpenAI-compatible with thinking-mode extensions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from lionagi.providers.openai.chat import OpenAIChatCompletionsRequest
from lionagi.service.connections.endpoint import Endpoint

from ._config import DeepSeekConfigs

DeepseekThinkingType = Literal["enabled", "disabled"]
DeepseekReasoningEffort = Literal["low", "medium", "high", "xhigh", "max"]


class DeepseekThinking(BaseModel):
    """DeepSeek thinking-mode switch."""

    type: DeepseekThinkingType = "enabled"


class DeepseekChatCompletionsRequest(OpenAIChatCompletionsRequest):
    """Request body for DeepSeek chat completions; extends OpenAI-compatible surface with thinking-mode params."""

    thinking: DeepseekThinking | None = Field(
        default=None,
        description="DeepSeek thinking-mode switch.",
    )
    reasoning_effort: DeepseekReasoningEffort | None = Field(
        default=None,
        description="DeepSeek reasoning effort; common effort values are mapped.",
    )

    @model_validator(mode="after")
    def _normalize_deepseek_reasoning(self):
        # DeepSeek accepts: low, medium, high, max.
        # Map lionagi/OpenAI effort names to DeepSeek equivalents.
        if self.reasoning_effort in {"low", "medium"}:
            self.reasoning_effort = "high"
        elif self.reasoning_effort == "xhigh":
            self.reasoning_effort = "max"
        return self


def normalize_deepseek_usage(response: Any) -> Any:
    """Alias reasoning_tokens as thinking_tokens in DeepSeek usage; preserves provider-native fields."""
    if not isinstance(response, dict):
        return response

    usage = response.get("usage")
    if not isinstance(usage, dict):
        return response

    details = usage.get("completion_tokens_details")
    if not isinstance(details, dict):
        details = {}

    # Prefer provider-native location as canonical source.
    # Use sentinel to distinguish 0 from missing.
    _missing = object()
    thinking_tokens = None
    for src, key in (
        (details, "reasoning_tokens"),
        (details, "thinking_tokens"),
        (usage, "reasoning_tokens"),
        (usage, "thinking_tokens"),
    ):
        val = src.get(key, _missing)
        if val is not _missing:
            thinking_tokens = val
            break
    if thinking_tokens is not None:
        usage["thinking_tokens"] = thinking_tokens
        usage["reasoning_tokens"] = thinking_tokens
        if isinstance(usage.get("completion_tokens_details"), dict):
            usage["completion_tokens_details"]["thinking_tokens"] = thinking_tokens
            usage["completion_tokens_details"]["reasoning_tokens"] = thinking_tokens

    return response


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
