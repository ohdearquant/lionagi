# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import logging

from pydantic import Field, model_validator
from typing_extensions import Self

from ..hooks.hooked_event import HookedEvent
from ..token_budget import lookup_context_window
from .endpoint import Endpoint

logger = logging.getLogger(__name__)


__all__ = ("APICalling",)


class APICalling(HookedEvent):
    """Async API call with automatic token usage tracking; supports regular and streaming responses."""

    endpoint: Endpoint = Field(
        ...,
        description="Endpoint instance for making the API call",
        exclude=True,
    )

    payload: dict = Field(..., description="Request payload to send to the API")

    headers: dict = Field(
        default_factory=dict,
        description="Additional headers for the request",
        exclude=True,
    )

    call_kwargs: dict = Field(
        default_factory=dict,
        description="Transport/runtime kwargs to pass to the endpoint call",
        exclude=True,
    )

    cache_control: bool = Field(
        default=False,
        description="Whether to use cache control for this request",
        exclude=True,
    )

    include_token_usage_to_model: bool = Field(
        default=False,
        description="Whether to include token usage information in messages",
        exclude=True,
    )

    @model_validator(mode="after")
    def _validate_streaming(self) -> Self:
        """Validate streaming configuration and add token usage if requested."""
        if self.payload.get("stream") is True:
            self.streaming = True

        if self.include_token_usage_to_model and self.endpoint.config.requires_tokens:
            if "messages" in self.payload and isinstance(self.payload["messages"][-1], dict):
                required_tokens = self.required_tokens
                content = self.payload["messages"][-1]["content"]
                token_msg = f"\n\nEstimated Current Token Usage: {required_tokens}"

                if "model" in self.payload:
                    limit = lookup_context_window(self.payload["model"])
                    token_msg += f"/{limit:,}"

                if isinstance(content, str):
                    content += token_msg
                elif isinstance(content, dict) and "text" in content:
                    content["text"] += token_msg
                elif isinstance(content, list):
                    for item in reversed(content):
                        if isinstance(item, dict) and "text" in item:
                            item["text"] += token_msg
                            break

                self.payload["messages"][-1]["content"] = content

        return self

    @property
    def required_tokens(self) -> int | None:
        """Estimate token count for this request payload (messages, responses API, or embeddings format)."""
        from lionagi.service.token_calculator import TokenCalculator

        if not self.endpoint.config.requires_tokens:
            return None

        if "messages" in self.payload:
            return TokenCalculator.calculate_message_tokens(
                self.payload["messages"], **self.payload
            )
        elif "input" in self.payload:
            input_val = self.payload["input"]
            if isinstance(input_val, str):
                messages = [{"role": "user", "content": input_val}]
            elif isinstance(input_val, list):
                messages = []
                for item in input_val:
                    if isinstance(item, str):
                        messages.append({"role": "user", "content": item})
                    elif isinstance(item, dict) and "type" in item:
                        if item["type"] == "message":
                            messages.append(item)
            else:
                return None
            return TokenCalculator.calculate_message_tokens(messages, **self.payload)
        elif "embed" in self.endpoint.config.endpoint:
            return TokenCalculator.calculate_embed_token(**self.payload)

        return None

    async def _core_invoke(self):
        return await self.endpoint.call(
            request=self.payload,
            cache_control=self.cache_control,
            skip_payload_creation=True,
            extra_headers=self.headers if self.headers else None,
            **self.call_kwargs,
        )

    async def _core_stream(self):
        async for i in self.endpoint.stream(
            request=self.payload,
            extra_headers=self.headers if self.headers else None,
            **self.call_kwargs,
        ):
            yield i

    @property
    def request(self) -> dict:
        """Return request metadata dict (currently: required_tokens)."""
        return {
            "required_tokens": self.required_tokens,
        }
