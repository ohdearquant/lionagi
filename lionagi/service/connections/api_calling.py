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
    """Handles asynchronous API calls with automatic token usage tracking.

    This class manages API calls through endpoints, handling both regular
    and streaming responses with optional token usage tracking.
    """

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

        # Add token usage information to the last message if requested
        if self.include_token_usage_to_model and self.endpoint.config.requires_tokens:
            # Handle both messages format (chat completions) and input format (responses API)
            if "messages" in self.payload and isinstance(self.payload["messages"][-1], dict):
                required_tokens = self.required_tokens
                content = self.payload["messages"][-1]["content"]
                token_msg = f"\n\nEstimated Current Token Usage: {required_tokens}"

                if "model" in self.payload:
                    limit = lookup_context_window(self.payload["model"])
                    token_msg += f"/{limit:,}"

                # Update content based on its type
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
        """Calculate the number of tokens required for this request."""
        from lionagi.service.token_calculator import TokenCalculator

        if not self.endpoint.config.requires_tokens:
            return None

        # Handle chat completions format
        if "messages" in self.payload:
            return TokenCalculator.calculate_message_tokens(
                self.payload["messages"], **self.payload
            )
        # Handle responses API format
        elif "input" in self.payload:
            # Convert input to messages format for token calculation
            input_val = self.payload["input"]
            if isinstance(input_val, str):
                messages = [{"role": "user", "content": input_val}]
            elif isinstance(input_val, list):
                # Handle array input format
                messages = []
                for item in input_val:
                    if isinstance(item, str):
                        messages.append({"role": "user", "content": item})
                    elif isinstance(item, dict) and "type" in item:
                        # Handle structured input items
                        if item["type"] == "message":
                            messages.append(item)
            else:
                return None
            return TokenCalculator.calculate_message_tokens(messages, **self.payload)
        # Handle embeddings endpoint
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
        """Get request information including token usage."""
        return {
            "required_tokens": self.required_tokens,
        }
