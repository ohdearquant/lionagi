# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import logging

from pydantic import Field, PrivateAttr, model_validator
from typing_extensions import Self

from lionagi._errors import ProviderStreamError

from ..hooks.hooked_event import HookedEvent
from ..token_budget import lookup_context_window
from ..types.stream_chunk import StreamChunk
from .endpoint import Endpoint, _call_failure_detail

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

    # Carries the authorization header, so it stays out of repr()/str() as well as dumps.
    headers: dict = Field(
        default_factory=dict,
        description="Additional headers for the request",
        exclude=True,
        repr=False,
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

    _routing_scope: object | None = PrivateAttr(default=None)

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
        scope_kwargs = (
            {"_routing_scope": self._routing_scope} if self._routing_scope is not None else {}
        )
        return await self.endpoint.call(
            request=self.payload,
            cache_control=self.cache_control,
            skip_payload_creation=True,
            extra_headers=self.headers if self.headers else None,
            **scope_kwargs,
            **self.call_kwargs,
        )

    async def _core_stream(self):
        """Yield the endpoint's chunks; a stream whose provider reported the call failed
        raises ``ProviderStreamError`` once it ends.

        The provider may say the call failed inside a stream it accepted with HTTP 200
        (the endpoint marks that chunk ``call_failed``). The chunk is still yielded and
        the stream read to its end, then the call fails, so ``Event.stream`` records it
        as FAILED rather than COMPLETED and a consumer reading the call sees the cause.
        """
        scope_kwargs = (
            {"_routing_scope": self._routing_scope} if self._routing_scope is not None else {}
        )
        failure: StreamChunk | None = None
        async for i in self.endpoint.stream(
            request=self.payload,
            extra_headers=self.headers if self.headers else None,
            **scope_kwargs,
            **self.call_kwargs,
        ):
            if failure is None and isinstance(i, StreamChunk) and i.metadata.get("call_failed"):
                failure = i
            yield i
        if failure is not None:
            raw = failure.metadata.get("raw")
            raise ProviderStreamError(
                f"The provider reported a failed call inside its stream: "
                f"{_call_failure_detail(failure)}",
                details={"event": raw} if isinstance(raw, dict) else None,
            )

    @property
    def request(self) -> dict:
        """Return request metadata dict (currently: required_tokens)."""
        return {
            "required_tokens": self.required_tokens,
        }
