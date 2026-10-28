# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Anthropic messages API request/response models."""

from __future__ import annotations

from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

from ._config import AnthropicConfigs

__all__ = ("ANTHROPIC_DEFAULT_MAX_TOKENS", "CreateMessageRequest", "AnthropicMessagesEndpoint")

# The output limit a request is sent with when nothing names one: the messages API refuses a
# request without `max_tokens`.
ANTHROPIC_DEFAULT_MAX_TOKENS = 8192

# Delta fields a consumer joins, each kind across the whole message. A citations_delta is not
# here because its text is the cited_text of the citation it carries; the quoted texts of a
# message's citations are joined like any other kind.
_JOINED_DELTA_FIELDS = {
    "text_delta": "text",
    "thinking_delta": "thinking",
    "input_json_delta": "partial_json",
    "signature_delta": "signature",
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


def _rename_masked_names(value: dict, names: list, key: Any) -> dict:
    """Rename each of ``names`` in ``value`` whose text holds a run of the key to its
    masked text, every entry keeping its place, and return ``{name: new name}``.

    Names are taken in order, and a masked name that is already a name of ``value``, or
    one an earlier name took, is numbered, so every entry stays reachable. Only a string
    name is matched. The object is rebuilt once, however many names change.
    """
    from lionagi.service.accounts._router import mask_key_runs

    present = set(value)
    renames: dict = {}
    for name in names:
        if not isinstance(name, str):
            continue
        masked = mask_key_runs(name, key)
        if masked == name:
            continue
        taken = masked
        suffix = 2
        while taken in present:
            taken = f"{masked} ({suffix})"
            suffix += 1
        present.discard(name)
        present.add(taken)
        renames[name] = taken
    if renames:
        items = [(renames.get(name, name), item) for name, item in value.items()]
        value.clear()
        value.update(items)
    return renames


def _mask_key_runs_in_strings(value: Any, key: Any, skip: set[tuple[int, Any]]) -> bool:
    """Mask long runs of the key in every string of ``value`` not in ``skip``, in
    place; return whether any string changed.

    A string is matched on its own, and a name and a number are matched as their text.
    A name whose text holds a run is replaced by its masked text and keeps its place in
    the object, and where the masked name is already taken it is numbered too, so both
    values stay reachable; a number whose text holds a run is replaced by its masked
    text. A bool and None are never matched.
    """
    from lionagi.service.accounts._router import mask_key_runs

    changed = False
    if isinstance(value, dict):
        entries = list(value.items())
        renames = _rename_masked_names(
            value, [name for name, _ in entries if (id(value), name) not in skip], key
        )
        changed = bool(renames)
        # A place is exempt by the name it had before its rename, so a renamed name that
        # reads as an exempt one does not make its own value exempt.
        items = [(renames.get(name, name), item, name) for name, item in entries]
    elif isinstance(value, list):
        items = [(name, item, name) for name, item in enumerate(value)]
    else:
        return False
    for name, item, recorded in items:
        if (id(value), recorded) in skip:
            continue
        if isinstance(item, str):
            masked = mask_key_runs(item, key)
            if masked != item:
                value[name] = masked
                changed = True
        elif isinstance(item, bool) or item is None:
            # A bool is an int to `isinstance`, and neither it nor None is text.
            continue
        elif isinstance(item, (int, float)):
            masked = mask_key_runs(str(item), key)
            if masked != str(item):
                value[name] = masked
                changed = True
        elif isinstance(item, (dict, list)):
            changed = _mask_key_runs_in_strings(item, key, skip) or changed
    return changed


def _settlement_reading(event: Any) -> dict | None:
    """What an event says about the attempt, read before any masking rewrites its text.

    The counts a settlement prices are the provider's own, so they are copied out of the
    event before a count whose text holds a run of the key is replaced by its masked text
    in place; the event itself is masked as usual. A ``message_start`` yields the model,
    the id and the usage of its message, a ``message_delta`` its usage; None is yielded
    for anything else, a ``message_start`` whose message is not an object included.
    """
    from lionagi.ln import copy

    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    if event_type == "message_start":
        message = event.get("message")
        if not isinstance(message, dict):
            return None
        return {
            "model": message.get("model"),
            "id": message.get("id"),
            "usage": copy(message.get("usage")),
        }
    if event_type == "message_delta":
        return {"usage": copy(event.get("usage"))}
    return None


def _mask_content_runs(body: Any, key: Any) -> Any:
    """``body`` with the key masked in every string of a response this request parsed.

    The text, thinking and quoted text of its content are each matched as a join of their
    kind, and all of them in order, a bare string item counting as text; a key split
    across those is masked in each piece that holds part of it. Every other string, such
    as a tool input value, a redacted block, a citation title or a signature, is matched
    on its own, so a key split between one of the joined strings and one of those is not
    masked (see ``StreamTextRedactor``). A dict name and a number are matched as their
    text; see ``_mask_key_runs_in_strings``. The body is changed in place.
    """
    from lionagi.service.accounts._router import mask_joined_key_runs

    raw = body.get("content") if isinstance(body, dict) else None
    content = raw if isinstance(raw, list) else [raw] if raw is not None else []
    pieces: list[tuple[str, str]] = []
    places: list[tuple[Any, Any]] = []
    for index, block in enumerate(content):
        if isinstance(block, str):
            # A bare string item is text: a list item in place, or the body's content.
            holder, field = (raw, index) if isinstance(raw, list) else (body, "content")
            pieces.append(("text", block))
            places.append((holder, field))
            continue
        if not isinstance(block, dict):
            continue
        for field in ("text", "thinking"):
            if block.get("type") == field and isinstance(block.get(field), str):
                pieces.append((field, block[field]))
                places.append((block, field))
        citations = block.get("citations")
        for citation in citations if isinstance(citations, list) else ():
            if isinstance(citation, dict) and isinstance(citation.get("cited_text"), str):
                pieces.append(("cited_text", citation["cited_text"]))
                places.append((citation, "cited_text"))
    for (holder, field), text in zip(places, mask_joined_key_runs(pieces, key), strict=True):
        holder[field] = text
    _mask_key_runs_in_strings(body, key, {(id(holder), field) for holder, field in places})
    return body


def _system_blocks(content: Any) -> list[dict]:
    """System content as a list of block copies: a string, a block, or a list of either.

    A text block with no visible text is dropped, since the API refuses one.
    """
    if isinstance(content, str | dict):
        content = [content]
    if not isinstance(content, list | tuple):
        return []
    blocks = []
    for item in content:
        block = {"type": "text", "text": item} if isinstance(item, str) else item
        if not isinstance(block, dict):
            continue
        if _is_blank_text(block):
            continue
        blocks.append(dict(block))
    return blocks


def _is_blank_text(block: dict) -> bool:
    return block.get("type") == "text" and not (
        isinstance(block.get("text"), str) and block["text"].strip()
    )


def _place_prompt_cache(payload: dict, stable_blocks: list[dict]) -> None:
    """Write the stable blocks as the native system field and place the cache marks.

    The marks follow ADR-0127 Amendment 3: a read of the deepest entry earlier requests of the
    conversation wrote, the end of the turn when more of the conversation follows, one hour on
    the end of the stable segment, then probes and the compiler's anchors, at most four in all
    with the caller's own marks counted. Lists and blocks are copied before a mark is written,
    so the caller's request is left as it was handed in. The placement is kept on the current
    call, when there is one, for the completed request to update the conversation's entries.
    """
    from lionagi.service._prompt_cache import current_call

    from ._prompt_cache import place

    if stable_blocks:
        payload["system"] = stable_blocks
    call = current_call()
    placement = place(payload, call)
    if call is not None:
        call.placement = placement


@AnthropicConfigs.MESSAGES.register
class AnthropicMessagesEndpoint(Endpoint):
    _account_routing_provider = "anthropic"
    caches_system_prefix = True

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

        import httpx

        from lionagi.ln.concurrency import get_cancelled_exc_class
        from lionagi.service.accounts._router import (
            get_router,
            header_pairs,
            redact_value,
            routed_client_kwargs,
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
            # Read once: the URL, method, headers and client options checked are the ones sent,
            # whatever changes them while the account is reserved.
            url = self.config.full_url
            method = self.config.method
            timeout = self.config.timeout
            request_pairs = header_pairs(headers)
            session_kwargs = routed_client_kwargs(self.config.client_kwargs)
            validate_routed_request(
                self,
                request_pairs,
                request_kwargs=request_kwargs,
                body_kwargs=body_kwargs,
                payload=payload,
                client_kwargs=session_kwargs,
                url=url,
                method=method,
            )
            if response_mode != "json":
                # A routed call is settled from the usage in its JSON body and its key is
                # masked in that body as parsed; a mode that returns anything else could do
                # neither.
                raise AccountRoutingError(RouterErrorCode.UNSUPPORTED_PRICING)
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
            request_pairs,
            endpoint=self,
            request_kwargs=request_kwargs,
            body_kwargs=body_kwargs,
            client_kwargs=session_kwargs,
            url=url,
            method=method,
        )
        request_headers = {
            name: value for name, value in request_pairs if name.strip().lower() != "content-type"
        }
        request_headers["Content-Type"] = "application/json"
        request_headers["x-api-key"] = attempt.key.get_secret_value()

        request_entered = False
        failure: Exception | None = None
        try:
            async with self._create_http_session(session_kwargs, timeout=timeout) as client:
                request_entered = True
                response = await client.request(
                    method=method,
                    url=url,
                    headers=request_headers,
                    content=attempt.body,
                    follow_redirects=False,
                    **request_kwargs,
                )
                status = response.status_code
                if status in (401, 403):
                    # A cancellation during the first of these writes lets it commit but stops
                    # the second from starting, so the block goes first: losing the attempt's
                    # reason keeps its ceiling held, while losing the block would leave the
                    # refused key free to be reserved again.
                    await router.block(attempt, "auth_required")
                    await router.settle_unknown(attempt, "auth_required")
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

                # The decode error is dropped, not chained: it can quote the body.
                try:
                    body = response.json()
                    unreadable = False
                except Exception:
                    unreadable = True
                if unreadable:
                    await router.settle_unknown(attempt, "usage_incomplete")
                    raise _RoutedNoRetryError(
                        RouterErrorCode.PROVIDER_ERROR.value,
                        account=attempt.account,
                        attempt_id=attempt.attempt_id,
                        http_status=status,
                    )

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
                return redact_value(_mask_content_runs(body, attempt.key), attempt.key)
        except get_cancelled_exc_class():
            # The request may have reached the provider, so the attempt stays open with its
            # ceiling held and records why, as a cancelled stream does. The write runs in a
            # worker thread that the cancellation does not stop: under an anyio cancel scope
            # the caller waits for it, while a native task.cancel() returns to the caller at
            # once and the write commits after. An attempt that already settled is left as it
            # is. A write that fails records nothing, and the cancellation still reaches the
            # caller.
            try:
                if not request_entered:
                    await router.settle_not_sent(attempt)
                else:
                    await router.settle_stream_end(attempt, None, reason="cancelled")
            except Exception:  # noqa: S110 - the caller must see the cancellation
                pass
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
        except (httpx.RequestError, asyncio.TimeoutError, TimeoutError, ConnectionError):
            failure = (_RoutedRetryableError if request_entered else _RoutedNoRetryError)(
                RouterErrorCode.TRANSPORT_ERROR.value,
                account=attempt.account,
                attempt_id=attempt.attempt_id,
            )
        except Exception:
            failure = _RoutedNoRetryError(
                RouterErrorCode.TRANSPORT_ERROR.value,
                account=attempt.account,
                attempt_id=attempt.attempt_id,
            )
        # Settled and raised here, outside the handler, so neither a ledger error nor
        # this error carries context: the exception it stands for can quote the request
        # or the response.
        if failure is not None:
            if request_entered:
                await router.settle_unknown(attempt, "transport_error")
            else:
                await router.settle_not_sent(attempt)
            raise failure

    async def _stream_http(self, payload: dict, headers: dict, *, _routing_scope=None, **kwargs):
        if not self._account_routing_enabled():
            async for chunk in super()._stream_http(
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
        import codecs
        import json

        from lionagi.ln.concurrency import get_cancelled_exc_class
        from lionagi.service.accounts._pricing import usage_outcome
        from lionagi.service.accounts._router import (
            StreamingSecretRedactor,
            StreamTextRedactor,
            get_router,
            header_pairs,
            provider_request_id,
            redact_value,
            routed_client_kwargs,
            validate_routed_request,
        )
        from lionagi.service.accounts._types import (
            AccountRoutingError,
            RouterErrorCode,
        )
        from lionagi.service.connections.endpoint import (
            _RoutedNoRetryError,
            _RoutedRetryableError,
            _SSEFrame,
        )

        self._assert_ssrf_safe_url()
        payload["stream"] = True
        # Read once: the URL, method, headers and client options checked are the ones sent,
        # whatever changes them while the account is reserved.
        url = self.config.full_url
        method = self.config.method
        timeout = self.config.timeout
        request_pairs = header_pairs(headers)
        session_kwargs = routed_client_kwargs(self.config.client_kwargs)
        validate_routed_request(
            self,
            request_pairs,
            request_kwargs=kwargs,
            payload=payload,
            client_kwargs=session_kwargs,
            url=url,
            method=method,
        )
        router = get_router()
        scope = self._account_scope(_routing_scope)
        body_kwargs = {"json": payload}
        attempt = await router.begin(
            scope,
            payload,
            request_pairs,
            endpoint=self,
            request_kwargs=kwargs,
            body_kwargs=body_kwargs,
            client_kwargs=session_kwargs,
            url=url,
            method=method,
        )
        request_headers = {
            name: value for name, value in request_pairs if name.strip().lower() != "content-type"
        }
        request_headers["Content-Type"] = "application/json"
        request_headers["x-api-key"] = attempt.key.get_secret_value()

        request_entered = False
        failure: Exception | None = None
        redactor = StreamingSecretRedactor(attempt.key)
        # The byte redactor sees the JSON framing between two deltas, so a key split
        # across them reaches a consumer that joins their text. The text redactor holds
        # an event while its text ends in a run of the key shorter than MIN_RUN that the
        # next event may continue, and events leave in the order they came.
        text_redactor = StreamTextRedactor(attempt.key)
        decoder = codecs.getincrementaldecoder("utf-8")()
        pending = ""
        # The same SSE framing as the unrouted stream, so an event keeps its ``event:``
        # name and an ``event: error`` fails the call here as it does there.
        frame = _SSEFrame()
        usage: dict[str, Any] = {}
        served_model = None
        request_id = None
        settled = False
        saw_error_event = False
        saw_final_usage = False
        start_models: set[str] = set()

        def queue_data(data: str, sse_event: str | None = None) -> list:
            """Hand one event to the text redactor; return the events now ready."""
            event = None
            try:
                event = json.loads(data)
            except (TypeError, ValueError):
                pass
            pieces: list[tuple[str, str]] = []
            places: list[tuple[Any, Any]] = []
            # The chunk a consumer receives for this event, read before any masking.
            shown = self._line_to_stream_chunk(data, sse_event=sse_event)
            # A consumer joins chunk text by type (``Branch.run`` joins text chunks, and
            # thinking chunks) and may join every chunk's text in order.
            if shown is None or not isinstance(shown.content, str) or not shown.content:
                shown = None
            if not isinstance(event, dict):
                # Not a JSON object: the whole data is matched, joined with the data of
                # other such events, and the text its chunk shows is joined with the text
                # of the other chunks of that type.
                pieces.append((StreamTextRedactor.RAW, data))
                places.append((None, None))
                return push(event, data, places, False, pieces, shown, None, sse_event)
            # What the event says about the attempt, read before the walk masks its text:
            # a count whose text holds a run is masked in place, and the attempt is priced
            # from the provider's own count.
            settlement = _settlement_reading(event)
            delta = event.get("delta")
            if event.get("type") == "content_block_delta" and isinstance(delta, dict):
                delta_type = delta.get("type")
                field = (
                    _JOINED_DELTA_FIELDS.get(delta_type) if isinstance(delta_type, str) else None
                )
                citation = delta.get("citation") if delta_type == "citations_delta" else None
                if field is not None and isinstance(delta.get(field), str):
                    pieces.append((delta_type, delta[field]))
                    places.append((delta, field))
                elif isinstance(citation, dict) and isinstance(citation.get("cited_text"), str):
                    pieces.append((delta_type, citation["cited_text"]))
                    places.append((citation, "cited_text"))
                elif field is None and delta_type != "citations_delta":
                    # A delta type this module does not know: each of its text fields is
                    # joined like a known one, under the delta's type. A name holding a
                    # run is renamed before its place is recorded, and the write-back
                    # uses that name, since a place in ``skip`` is never rematched.
                    kind = delta_type if isinstance(delta_type, str) and delta_type else None
                    text_names = [
                        name
                        for name, value in delta.items()
                        if name != "type" and isinstance(value, str)
                    ]
                    renames = _rename_masked_names(delta, text_names, attempt.key)
                    for name in text_names:
                        name = renames.get(name, name)
                        pieces.append((kind, delta[name]))
                        places.append((delta, name))
            # Every other string value in the event is matched on its own: no consumer joins
            # it, except as the text of the event's chunk, which is joined with other chunks'
            # text. An object's name and a number are matched as their text too, and the
            # chunk masks a whole key wherever one lies in the text it shows.
            rewrite = _mask_key_runs_in_strings(
                event, attempt.key, {(id(holder), name) for holder, name in places}
            )
            return push(event, data, places, rewrite, pieces, shown, settlement, sse_event)

        def push(
            event, data, places, rewrite, pieces, shown, settlement=None, sse_event=None
        ) -> list:
            if shown is None:
                return text_redactor.push(
                    (event, data, places, rewrite, None, settlement, sse_event), pieces
                )
            # A piece whose text the chunk shows takes the chunk's join too; other text
            # the chunk shows is a piece of its own.
            source = next(
                (
                    index
                    for index, (kind, text) in enumerate(pieces)
                    if kind != StreamTextRedactor.RAW and text == shown.content
                ),
                shown.content,
            )
            return text_redactor.push(
                (event, data, places, rewrite, shown.type, settlement, sse_event),
                pieces,
                (shown.type, source),
            )

        async def emit_ready(ready: list):
            nonlocal served_model, request_id, settled, saw_error_event, saw_final_usage
            for (event, data, places, rewrite, shown_type, settlement, sse_event), texts in ready:
                shown_text = texts.pop() if shown_type is not None else None
                raw = None
                for (holder, field), text in zip(places, texts, strict=True):
                    if holder is None:
                        raw = text
                    else:
                        holder[field] = text
                if raw is not None:
                    data = raw
                    try:
                        event = json.loads(data)
                    except (TypeError, ValueError):
                        event = None
                elif places or rewrite:
                    data = json.dumps(event)
                event_type = event.get("type") if isinstance(event, dict) else None
                if event_type == "message_start":
                    # A message_start whose message is not an object says nothing about the
                    # attempt, and one that leaves a field out says nothing about that field,
                    # so each leaves what an earlier event said in place. Its counts are
                    # placeholders, so a start after a message_delta's final count leaves that
                    # count alone, and its model too. A stream has one message_start; when
                    # starts name two models, the next count cannot be tied to one of them
                    # (see message_delta).
                    if settlement is not None:
                        started_model = settlement.get("model")
                        if isinstance(started_model, str) and started_model:
                            start_models.add(started_model)
                        else:
                            started_model = None
                        if not saw_final_usage:
                            served_model = started_model or served_model
                        request_id = (
                            provider_request_id(settlement.get("id"), attempt.key) or request_id
                        )
                        initial_usage = settlement.get("usage")
                        if isinstance(initial_usage, dict) and not saw_final_usage:
                            usage.update(initial_usage)
                elif event_type == "message_delta":
                    delta_usage = settlement.get("usage") if settlement else None
                    if isinstance(delta_usage, dict):
                        usage.update(delta_usage)
                        # Usage that arrives after starts naming two models, an output count
                        # or any other count, has no one model to price it at, so the attempt
                        # is left unpriced, open with its ceiling held. Usage that arrived
                        # before the second start keeps the first model's price.
                        if len(start_models) > 1:
                            served_model = None
                        output_tokens = delta_usage.get("output_tokens")
                        if isinstance(output_tokens, int) and not isinstance(output_tokens, bool):
                            # A later delta whose count is not an integer replaces the earlier
                            # count, which also leaves the attempt unpriced.
                            saw_final_usage = True
                elif event_type == "error":
                    saw_error_event = True
                elif event_type == "message_stop" and not settled:
                    # message_start's output count is a placeholder; only a
                    # message_delta's output count completes it. The attempt settles once,
                    # so a later message_stop settles nothing.
                    outcome = (
                        usage_outcome(
                            router.revision,
                            attempt.ttls,
                            served_model,
                            usage,
                            request_id if isinstance(request_id, str) else None,
                        )
                        if saw_final_usage
                        else None
                    )
                    await router.settle_stream_end(attempt, outcome)
                    settled = True
                chunk = self._line_to_stream_chunk(data, sse_event=sse_event)
                if chunk is not None:
                    if chunk.metadata.get("call_failed"):
                        # A failure the event's SSE name or its data reports, whatever
                        # ``type`` the data carries.
                        saw_error_event = True
                    if shown_text is not None and chunk.type == shown_type:
                        chunk.content = shown_text
                    chunk.content = redact_value(chunk.content, attempt.key)
                    chunk.tool_name = redact_value(chunk.tool_name, attempt.key)
                    chunk.tool_id = redact_value(chunk.tool_id, attempt.key)
                    chunk.tool_input = redact_value(chunk.tool_input, attempt.key)
                    chunk.tool_output = redact_value(chunk.tool_output, attempt.key)
                    chunk.metadata = redact_value(chunk.metadata, attempt.key)
                    yield chunk

        try:
            async with self._create_http_session(session_kwargs, timeout=timeout) as client:
                request_entered = True
                async with client.stream(
                    method=method,
                    url=url,
                    headers=request_headers,
                    content=attempt.body,
                    follow_redirects=False,
                    **kwargs,
                ) as response:
                    status = response.status_code
                    if status in (401, 403):
                        # A cancellation during the first of these writes lets it commit but stops
                        # the second from starting, so the block goes first: losing the attempt's
                        # reason keeps its ceiling held, while losing the block would leave the
                        # refused key free to be reserved again.
                        await router.block(attempt, "auth_required")
                        await router.settle_unknown(attempt, "auth_required")
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

                    async for raw in response.aiter_bytes():
                        safe = redactor.feed(raw)
                        pending += decoder.decode(safe)
                        while "\n" in pending:
                            line, pending = pending.split("\n", 1)
                            for data, name in self._frame_stream_line(line, frame):
                                async for chunk in emit_ready(queue_data(data, name)):
                                    yield chunk
                    # The byte redactor's held tail can hold several lines, the last events
                    # of the stream among them.
                    pending += decoder.decode(redactor.feed(b"", final=True), final=True)
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        for data, name in self._frame_stream_line(line, frame):
                            async for chunk in emit_ready(queue_data(data, name)):
                                yield chunk
                    if pending:
                        for data, name in self._frame_stream_line(pending, frame):
                            async for chunk in emit_ready(queue_data(data, name)):
                                yield chunk
                    if frame.data:
                        data, name = self._take_sse_event(frame)
                        async for chunk in emit_ready(queue_data(data, name)):
                            yield chunk
                    async for chunk in emit_ready(text_redactor.finish()):
                        yield chunk
                    if not settled:
                        # The stream ended before message_stop, so its usage is incomplete.
                        reason = "stream_error" if saw_error_event else "stream_truncated"
                        await router.settle_stream_end(attempt, None, reason=reason)
                        settled = True
        except get_cancelled_exc_class():
            # A write that fails records nothing; the cancellation still reaches the caller.
            # As on the call path, a native task.cancel() returns before the write commits.
            try:
                if not request_entered:
                    await router.settle_not_sent(attempt)
                elif not settled:
                    await router.settle_stream_end(attempt, None, reason="cancelled")
            except Exception:  # noqa: S110 - the caller must see the cancellation
                pass
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
        except Exception:
            failure = (_RoutedRetryableError if request_entered else _RoutedNoRetryError)(
                RouterErrorCode.TRANSPORT_ERROR.value,
                account=attempt.account,
                attempt_id=attempt.attempt_id,
            )
        # Settled and raised here, outside the handler, so neither a ledger error nor
        # this error carries context: the exception it stands for can quote the request
        # or the response.
        if failure is not None:
            if request_entered:
                await router.settle_unknown(attempt, "transport_error")
            else:
                await router.settle_not_sent(attempt)
            raise failure

    def create_payload(
        self,
        request: dict | BaseModel,
        extra_headers: dict | None = None,
        **kwargs,
    ):
        if isinstance(request, dict):
            # A copy: the system message and the system blocks are taken off it below, and the
            # caller's request is left as it was handed in.
            request = request_dict = dict(request)
        else:
            request_dict = request.model_dump()
        system_blocks = request_dict.pop("system_blocks", None)
        if "system_blocks" in kwargs:
            system_blocks = kwargs.pop("system_blocks")
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

        # The layers merge in this order, a later one replacing an earlier. The call sends the
        # limit named last, a None naming none, so a None in the request leaves the model's limit
        # in place; when no layer names one, the default is sent.
        max_tokens = None
        for layer in (self.config.kwargs, request_dict, kwargs):
            if layer.get("max_tokens") is not None:
                max_tokens = layer["max_tokens"]
        kwargs["max_tokens"] = ANTHROPIC_DEFAULT_MAX_TOKENS if max_tokens is None else max_tokens

        payload, headers = super().create_payload(request, extra_headers=extra_headers, **kwargs)

        if (
            self._account_routing_enabled()
            and prompt_cache_control is not None
            and "cache_control" not in payload
        ):
            payload["cache_control"] = prompt_cache_control

        payload.pop("api_key", None)

        if self.places_prompt_cache:
            # ADR-0127 D5: this placement replaces the last-message mark `cache_control` would add.
            # The flag keeps its other meaning, the response memo in Endpoint.call.
            payload.pop("cache_control", None)
            # As below, a system message takes the place of a native `system` field; one with no
            # visible text does not, and a native field with none is not sent.
            stable = _system_blocks(system) or _system_blocks(payload.get("system"))
            stable += _system_blocks(system_blocks)
            if not stable:
                payload.pop("system", None)
            _place_prompt_cache(payload, stable)
            return (payload, headers)

        if "cache_control" in payload:
            cache_control = payload.pop("cache_control")
            if cache_control:
                cache_control = {"type": "ephemeral"}
                # The last message and its last block are copied before the mark is written, so
                # the caller's messages are left as they were handed in.
                last_message = payload["messages"][-1]["content"]
                if isinstance(last_message, str):
                    last_message = {
                        "type": "text",
                        "text": last_message,
                        "cache_control": cache_control,
                    }
                elif isinstance(last_message, list) and isinstance(last_message[-1], dict):
                    last_message = [
                        *last_message[:-1],
                        {**last_message[-1], "cache_control": cache_control},
                    ]
                payload["messages"] = [
                    *payload["messages"][:-1],
                    {
                        **payload["messages"][-1],
                        "content": (
                            [last_message] if not isinstance(last_message, list) else last_message
                        ),
                    },
                ]

        if system:
            system = [{"type": "text", "text": system}]
            payload["system"] = system

        return (payload, headers)
