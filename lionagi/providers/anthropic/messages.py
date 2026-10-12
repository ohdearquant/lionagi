# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Anthropic messages API request/response models."""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from ._config import AnthropicConfigs

__all__ = ("CreateMessageRequest", "AnthropicMessagesEndpoint")

# Delta fields a consumer joins across the events of one content block.
_JOINED_DELTA_FIELDS = {
    "text_delta": "text",
    "thinking_delta": "thinking",
    "input_json_delta": "partial_json",
}


class TextContentBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str
    cache_control: dict | None = None


class ImageSource(BaseModel):
    type: Literal["base64"] = "base64"
    media_type: Literal["image/jpeg", "image/png", "image/gif", "image/webp"]
    data: str


class ImageContentBlock(BaseModel):
    type: Literal["image"] = "image"
    source: ImageSource


ContentBlock = Union[TextContentBlock, ImageContentBlock]


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str | list[str | ContentBlock]

    @field_validator("content", mode="before")
    def validate_content(cls, v):
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            # Ensure all items are either strings or proper content blocks
            result = []
            for item in v:
                if isinstance(item, str):
                    result.append({"type": "text", "text": item})
                else:
                    result.append(item)
            return result
        return v


class ToolDefinition(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern="^[a-zA-Z0-9_-]+$")
    description: str | None = None
    input_schema: dict


class ToolChoice(BaseModel):
    type: Literal["auto", "any", "tool"]
    name: str | None = None


class CreateMessageRequest(BaseModel):
    """Request model for Anthropic messages API."""

    model: str = Field(..., min_length=1, max_length=256)
    messages: list[Message]
    max_tokens: int = Field(..., ge=1)

    # Optional fields
    system: str | list[ContentBlock] | None = None
    temperature: float | None = Field(None, ge=0, le=1)
    top_p: float | None = Field(None, ge=0, le=1)
    top_k: int | None = Field(None, ge=0)
    stop_sequences: list[str] | None = None
    stream: bool | None = False
    metadata: dict | None = None
    tools: list[ToolDefinition] | None = None
    tool_choice: ToolChoice | dict | None = None

    model_config = ConfigDict(extra="forbid")


class Usage(BaseModel):
    """Token usage information."""

    input_tokens: int
    output_tokens: int


class ContentBlockResponse(BaseModel):
    """Response content block."""

    type: Literal["text"]
    text: str


class CreateMessageResponse(BaseModel):
    """Response model for Anthropic messages API."""

    id: str
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: list[ContentBlockResponse]
    model: str
    stop_reason: None | (Literal["end_turn", "max_tokens", "stop_sequence", "tool_use"]) = None
    stop_sequence: str | None = None
    usage: Usage


# Streaming response models
class MessageStartEvent(BaseModel):
    type: Literal["message_start"] = "message_start"
    message: CreateMessageResponse


class ContentBlockStartEvent(BaseModel):
    type: Literal["content_block_start"] = "content_block_start"
    index: int
    content_block: ContentBlockResponse


class ContentBlockDeltaEvent(BaseModel):
    type: Literal["content_block_delta"] = "content_block_delta"
    index: int
    delta: dict


class ContentBlockStopEvent(BaseModel):
    type: Literal["content_block_stop"] = "content_block_stop"
    index: int


class MessageDeltaEvent(BaseModel):
    type: Literal["message_delta"] = "message_delta"
    delta: dict
    usage: Usage | None = None


class MessageStopEvent(BaseModel):
    type: Literal["message_stop"] = "message_stop"


StreamEvent = Union[
    MessageStartEvent,
    ContentBlockStartEvent,
    ContentBlockDeltaEvent,
    ContentBlockStopEvent,
    MessageDeltaEvent,
    MessageStopEvent,
]


CONTEXT_WINDOWS: dict[str, int] = {
    "claude-fable-5-1": 1_000_000,
    "claude-opus-5-5": 1_000_000,
    "claude-sonnet-5-5": 1_000_000,
    "claude-haiku-5-5": 1_000_000,
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
    _account_routing_provider = "anthropic"

    def __init__(
        self,
        config: EndpointConfig = None,
        **kwargs,
    ):
        if config is None:
            kwargs.setdefault("default_headers", {"anthropic-version": "2023-06-01"})
        super().__init__(config, **kwargs)

    async def _request_attempt(
        self,
        *,
        payload: dict,
        headers: dict[str, str],
        body_kwargs: dict,
        request_kwargs: dict,
        response_mode: str,
        error_context: str,
        _routing_scope=None,
    ):
        if not self._account_routing_enabled():
            return await super()._request_attempt(
                payload=payload,
                headers=headers,
                body_kwargs=body_kwargs,
                request_kwargs=request_kwargs,
                response_mode=response_mode,
                error_context=error_context,
                _routing_scope=_routing_scope,
            )

        import asyncio

        import aiohttp

        from lionagi.ln.concurrency import get_cancelled_exc_class
        from lionagi.service.accounts._router import (
            get_router,
            redact_value,
            validate_routed_request,
        )
        from lionagi.service.accounts._types import (
            AccountRoutingError,
            RouterErrorCode,
        )
        from lionagi.service.connections.endpoint import (
            _RoutedNoRetryError,
            _RoutedRetryableError,
        )

        try:
            validate_routed_request(
                self,
                headers,
                request_kwargs=request_kwargs,
                body_kwargs=body_kwargs,
                payload=payload,
            )
        except AccountRoutingError as exc:
            raise _RoutedNoRetryError(
                exc.code.value,
                account=exc.account,
                attempt_id=exc.attempt_id,
                http_status=exc.http_status,
            ) from None
        router = get_router()
        scope = self._account_scope(_routing_scope)
        attempt = await router.begin(
            scope,
            payload,
            headers,
            endpoint=self,
            request_kwargs=request_kwargs,
            body_kwargs=body_kwargs,
        )
        request_headers = {
            name: value for name, value in headers.items() if name.strip().lower() != "content-type"
        }
        request_headers["Content-Type"] = "application/json"
        request_headers["x-api-key"] = attempt.key.get_secret_value()

        response = None
        request_entered = False
        try:
            async with self._create_http_session() as session:
                request_entered = True
                response = await session.request(
                    method=self.config.method,
                    url=self.config.full_url,
                    headers=request_headers,
                    data=attempt.body,
                    allow_redirects=False,
                    **request_kwargs,
                )
                status = response.status
                if status in (401, 403):
                    await router.settle_unknown(attempt, "auth_required")
                    await router.block(attempt, "auth_required")
                    raise _RoutedNoRetryError(
                        RouterErrorCode.AUTH_FAILED.value,
                        account=attempt.account,
                        attempt_id=attempt.attempt_id,
                        http_status=status,
                    )
                if status == 429:
                    await router.settle_unknown(attempt, "rate_limited")
                    raise _RoutedRetryableError(
                        RouterErrorCode.RATE_LIMITED.value,
                        account=attempt.account,
                        attempt_id=attempt.attempt_id,
                        http_status=status,
                    )
                if status >= 500:
                    await router.settle_unknown(attempt, "provider_error")
                    raise _RoutedRetryableError(
                        RouterErrorCode.PROVIDER_ERROR.value,
                        account=attempt.account,
                        attempt_id=attempt.attempt_id,
                        http_status=status,
                    )
                if status != 200:
                    await router.settle_unknown(attempt, "request_rejected")
                    raise _RoutedNoRetryError(
                        RouterErrorCode.REQUEST_REJECTED.value,
                        account=attempt.account,
                        attempt_id=attempt.attempt_id,
                        http_status=status,
                    )

                if response_mode == "bytes":
                    body = await response.read()
                    await router.settle_unknown(attempt, "usage_incomplete")
                    return redact_value(body, attempt.key)

                try:
                    body = await response.json()
                except Exception:
                    await router.settle_unknown(attempt, "usage_incomplete")
                    raise _RoutedNoRetryError(
                        RouterErrorCode.PROVIDER_ERROR.value,
                        account=attempt.account,
                        attempt_id=attempt.attempt_id,
                        http_status=status,
                    ) from None

                if isinstance(payload, dict) and "messages" in payload and isinstance(body, dict):
                    if body.get("error") and "choices" not in body and "object" not in body:
                        await router.settle_unknown(attempt, "usage_incomplete")
                        raise _RoutedNoRetryError(
                            RouterErrorCode.PROVIDER_ERROR.value,
                            account=attempt.account,
                            attempt_id=attempt.attempt_id,
                            http_status=status,
                        )
                await router.settle_response(attempt, body)
                return redact_value(body, attempt.key)
        except get_cancelled_exc_class():
            if not request_entered:
                await router.settle_not_sent(attempt)
            raise
        except AccountRoutingError as exc:
            raise _RoutedNoRetryError(
                exc.code.value,
                account=exc.account,
                attempt_id=exc.attempt_id,
                http_status=exc.http_status,
            ) from None
        except (_RoutedNoRetryError, _RoutedRetryableError):
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, ConnectionError):
            if request_entered:
                await router.settle_unknown(attempt, "transport_error")
                raise _RoutedRetryableError(
                    RouterErrorCode.TRANSPORT_ERROR.value,
                    account=attempt.account,
                    attempt_id=attempt.attempt_id,
                ) from None
            await router.settle_not_sent(attempt)
            raise _RoutedNoRetryError(
                RouterErrorCode.TRANSPORT_ERROR.value,
                account=attempt.account,
                attempt_id=attempt.attempt_id,
            ) from None
        except Exception:
            if request_entered:
                await router.settle_unknown(attempt, "transport_error")
            else:
                await router.settle_not_sent(attempt)
            raise _RoutedNoRetryError(
                RouterErrorCode.TRANSPORT_ERROR.value,
                account=attempt.account,
                attempt_id=attempt.attempt_id,
            ) from None
        finally:
            if response is not None and not response.closed:
                response.release()

    async def _stream_aiohttp(self, payload: dict, headers: dict, *, _routing_scope=None, **kwargs):
        if not self._account_routing_enabled():
            async for chunk in super()._stream_aiohttp(
                payload,
                headers,
                _routing_scope=_routing_scope,
                **kwargs,
            ):
                yield chunk
            return

        from lionagi.service.accounts._types import AccountRoutingError, RouterErrorCode
        from lionagi.service.connections.endpoint import (
            _RoutedNoRetryError,
            _RoutedRetryableError,
        )
        from lionagi.service.resilience import retry_with_backoff

        if self.retry_config is None:
            retry_options = {
                "max_retries": max(0, self.config.max_retries - 1),
                "base_delay": 1.0,
                "max_delay": 60.0,
                "backoff_factor": 2.0,
                "jitter": True,
                "jitter_factor": 0.5,
            }
        else:
            retry_options = self.retry_config.as_kwargs()
            retry_options.pop("retry_exceptions", None)
            retry_options.pop("exclude_exceptions", None)

        async def first_chunk():
            stream = self._routed_stream_attempt(
                payload,
                headers,
                _routing_scope=_routing_scope,
                **kwargs,
            )
            try:
                chunk = await anext(stream)
            except StopAsyncIteration:
                return stream, None
            return stream, chunk

        try:
            stream, first = await retry_with_backoff(
                first_chunk,
                retry_exceptions=(_RoutedRetryableError,),
                exclude_exceptions=(_RoutedNoRetryError,),
                **retry_options,
            )
        except (_RoutedNoRetryError, _RoutedRetryableError) as exc:
            raise AccountRoutingError(
                RouterErrorCode(exc.code),
                account=exc.account,
                attempt_id=exc.attempt_id,
                http_status=exc.http_status,
            ) from None

        try:
            if first is not None:
                yield first
            async for chunk in stream:
                yield chunk
        except (_RoutedNoRetryError, _RoutedRetryableError) as exc:
            raise AccountRoutingError(
                RouterErrorCode(exc.code),
                account=exc.account,
                attempt_id=exc.attempt_id,
                http_status=exc.http_status,
            ) from None
        finally:
            # Closed early by its consumer, this generator closes the attempt too, so the
            # attempt records how the stream ended now rather than when collected.
            await stream.aclose()

    async def _routed_stream_attempt(
        self,
        payload: dict,
        headers: dict,
        *,
        _routing_scope=None,
        **kwargs,
    ):
        import asyncio
        import codecs
        import json

        import aiohttp

        from lionagi.ln.concurrency import get_cancelled_exc_class
        from lionagi.service.accounts._pricing import price_usage
        from lionagi.service.accounts._router import (
            StreamingSecretRedactor,
            StreamingTextRedactor,
            get_router,
            redact_value,
            validate_routed_request,
        )
        from lionagi.service.accounts._types import (
            AccountRoutingError,
            RouterErrorCode,
        )
        from lionagi.service.connections.endpoint import (
            _RoutedNoRetryError,
            _RoutedRetryableError,
        )

        self._assert_ssrf_safe_url()
        payload["stream"] = True
        validate_routed_request(
            self,
            headers,
            request_kwargs=kwargs,
            payload=payload,
        )
        router = get_router()
        scope = self._account_scope(_routing_scope)
        body_kwargs = {"json": payload}
        attempt = await router.begin(
            scope,
            payload,
            headers,
            endpoint=self,
            request_kwargs=kwargs,
            body_kwargs=body_kwargs,
        )
        request_headers = {
            name: value for name, value in headers.items() if name.strip().lower() != "content-type"
        }
        request_headers["Content-Type"] = "application/json"
        request_headers["x-api-key"] = attempt.key.get_secret_value()

        response = None
        request_entered = False
        redactor = StreamingSecretRedactor(attempt.key)
        # The byte redactor sees the JSON framing between two deltas, so a key split
        # across them reaches a consumer that joins their text. Each block's delta text
        # runs through its own redactor before its chunk is built, so the chunk and its
        # raw event agree; the held rest is sent as one more delta when the block ends.
        delta_redactors: dict[tuple[Any, str], StreamingTextRedactor] = {}
        decoder = codecs.getincrementaldecoder("utf-8")()
        pending = ""
        event_data: list[str] = []
        usage: dict[str, Any] = {}
        served_model = None
        request_id = None
        settled = False
        saw_error_event = False

        def finish_line(line: str) -> list[str]:
            text = line.rstrip("\r")
            stripped = text.strip()
            if not stripped:
                if not event_data:
                    return []
                data = "\n".join(event_data)
                event_data.clear()
                return [data]
            if stripped.startswith(":") or stripped.startswith(("event:", "id:", "retry:")):
                return []
            if stripped.startswith("data:"):
                data = stripped.removeprefix("data:")
                if data.startswith(" "):
                    data = data[1:]
                event_data.append(data)
                return []
            if event_data:
                data = "\n".join(event_data)
                event_data.clear()
                return [data, stripped]
            return [stripped]

        def block_of(event: dict) -> Any:
            index = event.get("index")
            return index if isinstance(index, int) else None

        def release_held(keys: list[tuple[Any, str]]) -> list:
            released = []
            for block, delta_type in keys:
                rest = delta_redactors.pop((block, delta_type)).feed("", final=True)
                if not rest:
                    continue
                field = _JOINED_DELTA_FIELDS[delta_type]
                event = {
                    "type": "content_block_delta",
                    "index": block,
                    "delta": {"type": delta_type, field: rest},
                }
                released.append(self._line_to_stream_chunk(json.dumps(event)))
            return released

        async def emit_data(data: str):
            nonlocal served_model, request_id, settled, saw_error_event
            event = None
            try:
                event = json.loads(data)
            except (TypeError, ValueError):
                pass
            if isinstance(event, dict):
                if event.get("type") == "message_start":
                    message = event.get("message")
                    if isinstance(message, dict):
                        served_model = message.get("model")
                        request_id = message.get("id")
                        initial_usage = message.get("usage")
                        if isinstance(initial_usage, dict):
                            usage.update(initial_usage)
                elif event.get("type") == "message_delta":
                    delta_usage = event.get("usage")
                    if isinstance(delta_usage, dict):
                        usage.update(delta_usage)
                elif event.get("type") == "content_block_delta":
                    delta = event.get("delta")
                    delta_type = delta.get("type") if isinstance(delta, dict) else None
                    field = (
                        _JOINED_DELTA_FIELDS.get(delta_type)
                        if isinstance(delta_type, str)
                        else None
                    )
                    if field is not None and isinstance(delta.get(field), str):
                        delta_redactor = delta_redactors.setdefault(
                            (block_of(event), delta_type), StreamingTextRedactor(attempt.key)
                        )
                        delta[field] = delta_redactor.feed(delta[field])
                        data = json.dumps(event)
                elif event.get("type") == "content_block_stop":
                    block = block_of(event)
                    for held in release_held([key for key in delta_redactors if key[0] == block]):
                        yield held
                elif event.get("type") == "message_stop":
                    priced = price_usage(
                        router.revision,
                        attempt.ttls,
                        served_model,
                        usage,
                        request_id if isinstance(request_id, str) else None,
                    )
                    await router.settle_stream_end(attempt, priced)
                    settled = True
                elif event.get("type") == "error":
                    saw_error_event = True
            chunk = self._line_to_stream_chunk(data)
            if chunk is not None:
                chunk.content = redact_value(chunk.content, attempt.key)
                chunk.tool_name = redact_value(chunk.tool_name, attempt.key)
                chunk.tool_id = redact_value(chunk.tool_id, attempt.key)
                chunk.tool_input = redact_value(chunk.tool_input, attempt.key)
                chunk.tool_output = redact_value(chunk.tool_output, attempt.key)
                chunk.metadata = redact_value(chunk.metadata, attempt.key)
                yield chunk

        try:
            async with self._create_http_session() as session:
                request_entered = True
                response = await session.request(
                    method=self.config.method,
                    url=self.config.full_url,
                    headers=request_headers,
                    data=attempt.body,
                    allow_redirects=False,
                    **kwargs,
                )
                status = response.status
                if status in (401, 403):
                    await router.settle_unknown(attempt, "auth_required")
                    await router.block(attempt, "auth_required")
                    raise AccountRoutingError(
                        RouterErrorCode.AUTH_FAILED,
                        account=attempt.account,
                        attempt_id=attempt.attempt_id,
                        http_status=status,
                    ) from None
                if status == 429:
                    await router.settle_unknown(attempt, "rate_limited")
                    raise _RoutedRetryableError(
                        RouterErrorCode.RATE_LIMITED.value,
                        account=attempt.account,
                        attempt_id=attempt.attempt_id,
                        http_status=status,
                    )
                if status >= 500:
                    await router.settle_unknown(attempt, "provider_error")
                    raise _RoutedRetryableError(
                        RouterErrorCode.PROVIDER_ERROR.value,
                        account=attempt.account,
                        attempt_id=attempt.attempt_id,
                        http_status=status,
                    )
                if status != 200:
                    await router.settle_unknown(attempt, "request_rejected")
                    raise AccountRoutingError(
                        RouterErrorCode.REQUEST_REJECTED,
                        account=attempt.account,
                        attempt_id=attempt.attempt_id,
                        http_status=status,
                    ) from None

                async for raw in response.content:
                    safe = redactor.feed(raw)
                    pending += decoder.decode(safe)
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        for data in finish_line(line):
                            async for chunk in emit_data(data):
                                yield chunk
                pending += decoder.decode(redactor.feed(b"", final=True), final=True)
                if pending:
                    for data in finish_line(pending):
                        async for chunk in emit_data(data):
                            yield chunk
                if event_data:
                    data = "\n".join(event_data)
                    event_data.clear()
                    async for chunk in emit_data(data):
                        yield chunk
                for held in release_held(list(delta_redactors)):
                    yield held
                if not settled:
                    # The stream ended before message_stop, so its usage is incomplete.
                    reason = "stream_error" if saw_error_event else "stream_truncated"
                    await router.settle_stream_end(attempt, None, reason=reason)
                    settled = True
        except get_cancelled_exc_class():
            if not request_entered:
                await router.settle_not_sent(attempt)
            elif not settled:
                await router.settle_stream_end(attempt, None, reason="cancelled")
            raise
        except GeneratorExit:
            # The consumer closed the stream before message_stop.
            if not settled:
                await router.settle_stream_end(attempt, None, reason="stream_closed")
            raise
        except AccountRoutingError:
            raise
        except (_RoutedNoRetryError, _RoutedRetryableError):
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError, ConnectionError):
            if request_entered:
                await router.settle_unknown(attempt, "transport_error")
                raise _RoutedRetryableError(
                    RouterErrorCode.TRANSPORT_ERROR.value,
                    account=attempt.account,
                    attempt_id=attempt.attempt_id,
                ) from None
            await router.settle_not_sent(attempt)
            raise _RoutedNoRetryError(
                RouterErrorCode.TRANSPORT_ERROR.value,
                account=attempt.account,
                attempt_id=attempt.attempt_id,
            ) from None
        except Exception:
            if request_entered:
                await router.settle_unknown(attempt, "transport_error")
                raise _RoutedRetryableError(
                    RouterErrorCode.TRANSPORT_ERROR.value,
                    account=attempt.account,
                    attempt_id=attempt.attempt_id,
                ) from None
            else:
                await router.settle_not_sent(attempt)
            raise _RoutedNoRetryError(
                RouterErrorCode.TRANSPORT_ERROR.value,
                account=attempt.account,
                attempt_id=attempt.attempt_id,
            ) from None
        finally:
            if response is not None and not response.closed:
                response.release()

    def create_payload(
        self,
        request: dict | BaseModel,
        extra_headers: dict | None = None,
        **kwargs,
    ):
        request_dict = request if isinstance(request, dict) else request.model_dump()
        prompt_cache_control = request_dict.get("cache_control")
        if self._account_routing_enabled() and request_dict.get("service_tier") not in (
            None,
            "standard_only",
        ):
            from lionagi.service.accounts._types import AccountRoutingError, RouterErrorCode

            # The request model has no tier field, so the tier would be dropped and the call
            # sent as standard_only. Routed calls are priced for standard_only alone.
            raise AccountRoutingError(RouterErrorCode.UNSUPPORTED_PRICING)
        system = None

        if "messages" in request_dict and request_dict["messages"]:
            first_message = request_dict["messages"][0]
            if first_message.get("role") == "system":
                system = first_message["content"]
                request_dict["messages"] = request_dict["messages"][1:]
                request = request_dict

        payload, headers = super().create_payload(request, extra_headers=extra_headers, **kwargs)

        if (
            self._account_routing_enabled()
            and prompt_cache_control is not None
            and "cache_control" not in payload
        ):
            payload["cache_control"] = prompt_cache_control

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

        if system:
            system = [{"type": "text", "text": system}]
            payload["system"] = system

        return (payload, headers)
