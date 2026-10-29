# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import logging
import math
from contextvars import ContextVar
from typing import Any, Literal

from pydantic import Field, PrivateAttr, model_validator
from typing_extensions import Self

from lionagi._errors import ProviderStreamError

from .._api_telemetry import (
    InputDecision,
    RequestFrame,
    activate_request,
    capture_request,
    is_collecting,
    mark_dispatch,
)
from .._prompt_cache import PromptCacheCall, activate_call, note_attempt
from .._prompt_cache import current_call as current_prompt_cache_call
from ..hooks.hooked_event import HookedEvent
from ..token_budget import lookup_context_window
from ..types.stream_chunk import StreamChunk
from .endpoint import Endpoint, _call_failure_detail

logger = logging.getLogger(__name__)

_input_chars: ContextVar[tuple[dict, list[int]] | None] = ContextVar(
    "api_input_chars", default=None
)


__all__ = ("APICalling", "estimate_payload_input_tokens")


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

    max_input_tokens: int | None = Field(
        default=None,
        description="Refuse the request when input_token_estimate exceeds this; None = no cap",
        exclude=True,
    )

    input_token_scale: float = Field(
        default=1.0,
        ge=1.0,
        description="Provider-reported input tokens per estimated token, learned by the iModel; "
        "max_input_tokens is compared with the estimate times this",
        exclude=True,
    )

    _routing_scope: object | None = PrivateAttr(default=None)
    _counted_input_tokens: int | None = PrivateAttr(default=None)
    _telemetry_initialized: bool = PrivateAttr(default=False)
    _telemetry_frame: RequestFrame | None = PrivateAttr(default=None)
    # The prompt-cache call open where this request was built; the send runs in the
    # processor's context, so the attempt is recorded on this object, not on the ambient one.
    _prompt_cache_call: PromptCacheCall | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _capture_prompt_cache_call(self) -> Self:
        self._prompt_cache_call = current_prompt_cache_call()
        return self

    @model_validator(mode="after")
    def _validate_streaming(self) -> Self:
        """Validate streaming configuration and add token usage if requested."""
        if self.payload.get("stream") is True:
            self.streaming = True

        if (
            self.include_token_usage_to_model
            and self.endpoint.config.requires_tokens
            and not self.endpoint.places_prompt_cache
        ):
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

    @model_validator(mode="after")
    def _refuse_over_input_cap(self) -> Self:
        """Refuse, never truncate: a shortened history silently drops what the next turn reasons over.

        Runs after the validator above, so the estimate covers the payload as it will be sent.
        """
        observe = not self._telemetry_initialized and is_collecting()
        self._telemetry_initialized = True
        if self.max_input_tokens is None:
            if observe:
                self._capture_input_decision("uncapped", None, None, None, None)
            return self
        chars: list[int] = []
        token = _input_chars.set((self.payload, chars)) if observe else None
        try:
            counted = self.input_token_estimate
        finally:
            if token is not None:
                _input_chars.reset(token)
        self._counted_input_tokens = counted
        scale = self.input_token_scale
        estimate = math.ceil(counted * scale) if scale > 1.0 else counted
        if observe:
            self._capture_input_decision(
                "refused" if estimate > self.max_input_tokens else "checked",
                counted,
                scale,
                estimate,
                chars[0] if chars else None,
            )
        if estimate > self.max_input_tokens:
            from lionagi._errors import InputTokenCapError

            scaled = (
                f", times {scale:.3f}, the provider's own count per counted token so far"
                if scale > 1.0
                else ""
            )
            raise InputTokenCapError(
                f"request refused before sending: estimated input {estimate:,} tokens exceeds "
                f"max_input_tokens={self.max_input_tokens:,} (model "
                f"{self.payload.get('model')!r}; estimate = o200k_base tokens over the request's "
                f"system, messages and tools{scaled})",
                details={
                    "estimate": estimate,
                    "cap": self.max_input_tokens,
                    "counted": counted,
                    "scale": scale,
                },
            )
        return self

    def _capture_input_decision(
        self,
        kind: Literal["checked", "refused", "uncapped"],
        counted: int | None,
        scale: float | None,
        estimate: int | None,
        prompt_chars: int | None,
    ) -> None:
        if prompt_chars is None:
            try:
                text = _input_payload_text(self.payload)
                prompt_chars = len(text) if text is not None else 0
            except Exception:
                # An optional measurement can be unavailable without losing the row.
                prompt_chars = None
        try:
            provider = self.endpoint.config.provider
            model = self.payload.get("model")
            self._telemetry_frame = capture_request(
                InputDecision(
                    kind=kind,
                    counted=counted,
                    scale=scale,
                    estimate=estimate,
                    cap=self.max_input_tokens,
                    prompt_chars=prompt_chars,
                    provider=provider if isinstance(provider, str) else None,
                    model=model if isinstance(model, str) else None,
                ),
                api_call_id=None if kind == "refused" else str(self.id),
            )
        except Exception:
            # Optional uncapped measurement and observation cannot change the request.
            return

    @property
    def input_token_estimate(self) -> int:
        """Estimated input tokens of the whole request, for max_input_tokens.

        Unlike required_tokens this does not depend on the endpoint's requires_tokens. It counts
        ``system`` (outside ``messages`` in an Anthropic payload), ``instructions``, ``messages``,
        ``input``, ``prompt``, ``tools`` and ``response_format``, not ``tool_choice``. Each part is
        counted as its JSON text with the o200k_base encoding; the JSON punctuation makes it
        count somewhat high, never low on that account. tiktoken is called directly, with
        special-token text encoded as plain text: TokenCalculator.tokenize answers 0 when
        encoding raises (it does on text such as ``<|endoftext|>``), and a 0 here would let
        any request through the cap.
        """
        return estimate_payload_input_tokens(self.payload)

    @property
    def counted_input_tokens(self) -> int | None:
        """The estimate the cap checked when this call was built, before scaling; None without a cap."""
        return self._counted_input_tokens

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

    def _note_direct_attempt(self) -> None:
        # A routed request records each attempt where the router picks its account.
        routed = getattr(self.endpoint, "_account_routing_enabled", None)
        if not (routed and routed()):
            note_attempt("direct")

    async def _core_invoke(self):
        scope_kwargs = (
            {"_routing_scope": self._routing_scope} if self._routing_scope is not None else {}
        )
        mark_dispatch(self._telemetry_frame)
        with activate_request(self._telemetry_frame), activate_call(self._prompt_cache_call):
            self._note_direct_attempt()
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
        mark_dispatch(self._telemetry_frame)
        with activate_request(self._telemetry_frame), activate_call(self._prompt_cache_call):
            self._note_direct_attempt()
            source = self.endpoint.stream(
                request=self.payload,
                extra_headers=self.headers if self.headers else None,
                **scope_kwargs,
                **self.call_kwargs,
            ).__aiter__()
        unwinding = False
        try:
            while True:
                with activate_request(self._telemetry_frame):
                    try:
                        i = await source.__anext__()
                    except StopAsyncIteration:
                        break
                if failure is None and isinstance(i, StreamChunk) and i.metadata.get("call_failed"):
                    failure = i
                # Reset the request context before control returns to consumer code.
                yield i
        except BaseException:
            unwinding = True
            raise
        finally:
            try:
                if close := getattr(source, "aclose", None):
                    with activate_request(self._telemetry_frame):
                        await close()
            except Exception:
                if not unwinding:
                    raise
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


def _input_payload_text(payload: dict[str, Any]) -> str | None:
    import json

    keys = ("system", "instructions", "messages", "input", "prompt", "tools", "response_format")
    parts = [payload[key] for key in keys if payload.get(key) is not None]
    return json.dumps(parts, ensure_ascii=False, default=str) if parts else None


def estimate_payload_input_tokens(payload: dict[str, Any]) -> int:
    """The input-token estimate max_input_tokens is checked against, for a provider payload.

    APICalling.input_token_estimate counts its own payload with it; a caller that builds a
    payload without sending it (a host-side fold deciding whether the next request fits) counts
    the same way.
    """
    import tiktoken

    text = _input_payload_text(payload)
    if measurement := _input_chars.get():
        if measurement[0] is payload:
            measurement[1].append(len(text) if text is not None else 0)
    if text is None:
        return 0
    return len(tiktoken.get_encoding("o200k_base").encode(text, disallowed_special=()))
