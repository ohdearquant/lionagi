# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Anthropic messages API request/response models."""

from __future__ import annotations

import json
import logging
from typing import Any, Literal, Union

import anyio.lowlevel
from pydantic import BaseModel, ConfigDict, Field, field_validator

from lionagi.ln.concurrency import CancelScope
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.endpoint_config import EndpointConfig

# CONTEXT_WINDOWS is defined in ._config and re-exported here.
from ._config import ANTHROPIC_CONTEXT_WINDOWS as CONTEXT_WINDOWS  # noqa: F401
from ._config import AnthropicConfigs

logger = logging.getLogger("lionagi.providers.anthropic.messages")

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
    output_config: dict | None = None

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


class _AccountingStreamParser:
    """Read billing fields from the stream's raw bytes, framed as the endpoint frames them.

    Events are delimited by the endpoint's own server-sent-event framer (``framer``'s
    ``_frame_stream_line`` and ``_take_sse_event``), whatever separators the stream uses,
    and an SSE name is read the way the endpoint's ``_line_to_stream_chunk`` reads it: it
    stands in for the type only when it says the call failed and the data carries no type.
    So billing reads an event as a ``message_start``, ``message_delta``, ``message_stop``
    or failure exactly when the consumer's chunk for it has that type. Data that is not a
    JSON object counts only under a failure name.
    """

    def __init__(self, key, framer) -> None:
        import codecs

        from lionagi.service.connections.endpoint import (
            _SSE_CALL_FAILURE_NAMES,
            _event_reports_call_failure,
            _SSEFrame,
        )

        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.pending = ""
        self.frame = _SSEFrame()
        self.framer = framer
        self.key = key
        self.model = None
        self.models: set[str] = set()
        self.request_id = None
        self.usage: dict[str, Any] = {}
        self.output_seen = False
        self.output_valid = False
        self.message_stop = False
        self.saw_error = False
        self._failure_names = _SSE_CALL_FAILURE_NAMES
        self._reports_failure = _event_reports_call_failure

    def feed(self, raw: bytes) -> list[dict]:
        """The events the complete lines of ``raw`` finish."""
        self.pending += self.decoder.decode(raw)
        events = []
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            events.extend(self._events(self.framer._frame_stream_line(line, self.frame)))
        return events

    def finish(self) -> list[dict]:
        """The events a clean end of stream completes: the last line and the held event.

        Called only when the byte source ended without an error; after a transport failure
        an unterminated line or event is a truncated event and contributes nothing.
        """
        self.pending += self.decoder.decode(b"", final=True)
        framed = self.framer._frame_stream_line(self.pending, self.frame) if self.pending else []
        self.pending = ""
        if self.frame.data:
            framed.append(self.framer._take_sse_event(self.frame))
        return self._events(framed)

    def _events(self, framed) -> list[dict]:
        events = []
        for payload, name in framed:
            try:
                event = json.loads(payload)
            except (TypeError, ValueError):
                event = None
            if isinstance(event, dict):
                # The endpoint's rule (``_line_to_stream_chunk``): a name stands in for the
                # type only when it says the call failed and the data carries no type.
                if event.get("type") is None and name in self._failure_names:
                    event = {**event, "type": name}
                events.append({**event, "_accounting_sse_event": name})
            elif name in self._failure_names:
                # The endpoint turns such data into an error chunk under a failure name and
                # into a text chunk under any other name, which billing does not read.
                events.append({"type": name, "_invalid": True, "_accounting_sse_event": name})
        return events

    def observe(self, event: dict) -> None:
        if event.get("_accounting_sse_event") in self._failure_names or self._reports_failure(
            event
        ):
            self.saw_error = True
        kind = event.get("type")
        reading = _settlement_reading(event)
        if kind == "message_start" and reading:
            model = reading.get("model")
            if isinstance(model, str) and model:
                self.models.add(model)
                self.model = model if len(self.models) == 1 else None
            from lionagi.service.accounts._router import provider_request_id

            self.request_id = provider_request_id(reading.get("id"), self.key) or self.request_id
            # A start's counts are placeholders: once a message_delta has counted the output,
            # a later start changes no count.
            if not self.output_seen:
                self._merge(reading.get("usage"), observed_output=False)
        elif kind == "message_delta" and reading:
            usage = reading.get("usage")
            self._merge(usage, observed_output=True)
        elif kind == "message_stop":
            self.message_stop = True
        elif kind == "error":
            self.saw_error = True

    def _merge(self, update, *, observed_output: bool) -> None:
        if not isinstance(update, dict):
            return
        for name, value in update.items():
            if name not in ("cache_creation",):
                self.usage[name] = value
        if "cache_creation_input_tokens" in update:
            self.usage["cache_creation_input_tokens"] = update["cache_creation_input_tokens"]
        if "cache_creation" in update:
            self.usage["cache_creation"] = update["cache_creation"]
        if observed_output and "output_tokens" in update:
            value = update["output_tokens"]
            self.output_seen = True
            self.output_valid = type(value) is int and value >= 0


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
    conversation wrote, the end of the turn when more of the conversation follows and no fold
    is expected before it (Amendment 4), one hour on
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


# The classes a rejected request is recorded under (ADR-0126 Amendments 7 and 8): the first row
# with a text the provider's error message contains, compared case-sensitively, names the class.
# A message that matches no row, or no message at all, is recorded as ``other``.
_REJECTION_CLASS_MESSAGES = (
    ("prompt_too_long", ("prompt is too long",)),
    (
        "tool_result_missing",
        (
            "tool_use ids were found without tool_result blocks immediately after",
            "was found without a corresponding",
        ),
    ),
    ("prefill_unsupported", ("does not support assistant message prefill",)),
    (
        "thinking_config_unsupported",
        (
            '"thinking.type.enabled" is not supported',
            '"thinking.type.disabled" is not supported',
            '"thinking.type.between_tools" is not supported',
            "adaptive thinking is not supported on this model",
            "is not supported when thinking is disabled on this model",
            "effort cannot change when thinking is disabled",
        ),
    ),
    (
        "thinking_block_invalid",
        (
            "blocks in the latest assistant message cannot be modified",
            "The block is bound to a different conversation",
        ),
    ),
    (
        "tool_choice_unsupported",
        ('tool_choice: type "tool" and "any" are not supported for this model',),
    ),
    ("tool_type_unsupported", ("does not support tool types:",)),
    ("image_too_large", ("exceed the maximum image size", "many-image requests")),
    # Amendment 8: rejections caused by the account's billing state, not by the request.
    ("credit_balance_low", ("Your credit balance is too low",)),
    (
        "spend_limit_reached",
        (
            "You have reached your specified API usage limits",
            "You have reached your specified workspace API usage limits",
        ),
    ),
)

# A rejection body longer than this many bytes is not decoded. The caller hands over at most one
# byte more than this, which is enough to tell that a body is too long.
_REJECTION_BODY_LIMIT = 65536

# A streamed rejection body is read for at most this many seconds. A body that has not arrived
# by then is classified as ``other``.
_REJECTION_READ_SECONDS = 5.0


def _classify_rejection(body: bytes) -> tuple[str, str]:
    """Name the provider error type and the rejection class of a rejected request's body.

    Returns ``(provider_error_type, rejection_class)``, each a member of the closed sets in
    ``lionagi.service.accounts._types``. The error type is the body's ``error.type`` when it is
    one of the documented types, else ``other``. The class is the first row of
    ``_REJECTION_CLASS_MESSAGES`` whose text ``error.message`` contains, else ``other``. A body
    longer than ``_REJECTION_BODY_LIMIT`` bytes is not decoded. Nothing of the body is logged,
    kept or raised: the two names are returned, and every failure returns ``("other", "other")``
    after one debug log that names only the type of the exception.
    """
    try:
        from lionagi.service.accounts._types import _PROVIDER_ERROR_TYPES, _REJECTION_CLASSES

        if len(body) > _REJECTION_BODY_LIMIT:
            return "other", "other"
        decoded = json.loads(body)
        error = decoded.get("error") if isinstance(decoded, dict) else None
        if not isinstance(error, dict):
            return "other", "other"
        error_type = error.get("type")
        if not (isinstance(error_type, str) and error_type in _PROVIDER_ERROR_TYPES):
            error_type = "other"
        rejection_class = "other"
        message = error.get("message")
        if isinstance(message, str):
            for name, texts in _REJECTION_CLASS_MESSAGES:
                if any(text in message for text in texts):
                    rejection_class = name
                    break
        if rejection_class not in _REJECTION_CLASSES:
            rejection_class = "other"
        return error_type, rejection_class
    except Exception as exc:
        logger.debug("rejection body could not be classified: %s", type(exc).__name__)
        return "other", "other"


async def _classify_streamed_rejection(response: Any) -> tuple[str, str]:
    """Name the provider error type and the rejection class of a streamed rejection.

    Reads the response's decoded byte stream (``aiter_bytes``) until it holds more than
    ``_REJECTION_BODY_LIMIT`` bytes or ends, reads no further piece after that, and returns what
    ``_classify_rejection`` names for the first ``_REJECTION_BODY_LIMIT + 1`` bytes. The read is
    shielded from cancellation, so a caller that cancelled still has its attempt recorded, and
    bounded by ``_REJECTION_READ_SECONDS``: a body that has not arrived by then is
    ``("other", "other")``. Nothing of the body is logged, kept or raised: it is bound only in
    this frame, only the two names leave this function, and every failure returns
    ``("other", "other")``.
    """
    try:
        body = b""
        deadline = anyio.current_time() + _REJECTION_READ_SECONDS
        with CancelScope(deadline=deadline, shield=True) as scope:
            async for piece in response.aiter_bytes():
                body += piece
                if len(body) > _REJECTION_BODY_LIMIT:
                    break
        if scope.cancelled_caught:
            return "other", "other"
        return _classify_rejection(body[: _REJECTION_BODY_LIMIT + 1])
    except Exception:
        return "other", "other"


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
            RejectionZero,
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
        status = None
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
                    # Both writes are shielded from cancellation. The block goes first because
                    # a ledger error on the first write stops the second from running: losing
                    # the attempt's reason only keeps its ceiling held, while losing the block
                    # would leave the refused key free to be reserved again.
                    await router.block(attempt, "auth_required")
                    await router.settle_unknown(attempt, "auth_required", http_status=status)
                    raise _RoutedNoRetryError(
                        RouterErrorCode.AUTH_FAILED.value,
                        account=attempt.account,
                        attempt_id=attempt.attempt_id,
                        http_status=status,
                    )
                if status == 429:
                    await router.settle_unknown(attempt, "rate_limited", http_status=status)
                    raise _RoutedRetryableError(
                        RouterErrorCode.RATE_LIMITED.value,
                        account=attempt.account,
                        attempt_id=attempt.attempt_id,
                        http_status=status,
                    )
                if status >= 500:
                    # A 529 whose body names ``overloaded_error`` settles at zero; every other
                    # 5xx, and a 529 body that does not name it, keeps the ceiling. The raised
                    # error, and so the retry, is the same either way.
                    overloaded = (
                        status == 529
                        and _classify_rejection(response.content[: _REJECTION_BODY_LIMIT + 1])[0]
                        == "overloaded_error"
                    )
                    if overloaded:
                        await router.settle_rejection(
                            attempt,
                            RejectionZero(529, "overloaded_error", None, "provider_error"),
                            http_status=status,
                        )
                    else:
                        await router.settle_unknown(attempt, "provider_error", http_status=status)
                    raise _RoutedRetryableError(
                        RouterErrorCode.PROVIDER_ERROR.value,
                        account=attempt.account,
                        attempt_id=attempt.attempt_id,
                        http_status=status,
                    )
                if status != 200:
                    # Only the two names leave the classifier: the bounded slice of the body is
                    # passed straight on and no local holds it.
                    provider_error_type, rejection_class = _classify_rejection(
                        response.content[: _REJECTION_BODY_LIMIT + 1]
                    )
                    # Only a 400 naming ``invalid_request_error`` settles at zero, whatever its
                    # class; every other rejection keeps the ceiling.
                    if status == 400 and provider_error_type == "invalid_request_error":
                        await router.settle_rejection(
                            attempt,
                            RejectionZero(
                                400, "invalid_request_error", rejection_class, "request_rejected"
                            ),
                            http_status=status,
                        )
                    else:
                        await router.settle_unknown(
                            attempt,
                            "request_rejected",
                            provider_error_type=provider_error_type,
                            rejection_class=rejection_class,
                            http_status=status,
                        )
                    raise _RoutedNoRetryError(
                        RouterErrorCode.REQUEST_REJECTED.value,
                        account=attempt.account,
                        attempt_id=attempt.attempt_id,
                        http_status=status,
                        provider_error_type=provider_error_type,
                        rejection_class=rejection_class,
                    )

                # The decode error is dropped, not chained: it can quote the body.
                try:
                    body = response.json()
                    unreadable = False
                except Exception:
                    unreadable = True
                if unreadable:
                    await router.settle_unknown(attempt, "usage_incomplete", http_status=status)
                    raise _RoutedNoRetryError(
                        RouterErrorCode.PROVIDER_ERROR.value,
                        account=attempt.account,
                        attempt_id=attempt.attempt_id,
                        http_status=status,
                    )

                if isinstance(payload, dict) and "messages" in payload and isinstance(body, dict):
                    if body.get("error") and "choices" not in body and "object" not in body:
                        await router.settle_unknown(attempt, "usage_incomplete", http_status=status)
                        raise _RoutedNoRetryError(
                            RouterErrorCode.PROVIDER_ERROR.value,
                            account=attempt.account,
                            attempt_id=attempt.attempt_id,
                            http_status=status,
                        )
                await router.settle_response(attempt, body, http_status=status)
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
                    await router.settle_not_sent(attempt, caller_cancelled=True)
                else:
                    await router.settle_stream_end(
                        attempt, None, reason="cancelled", http_status=status
                    )
            except Exception:  # noqa: S110 - the caller must see the cancellation
                pass
            raise
        except AccountRoutingError as exc:
            await anyio.lowlevel.checkpoint()
            raise _RoutedNoRetryError(
                exc.code.value,
                account=exc.account,
                attempt_id=exc.attempt_id,
                http_status=exc.http_status,
            ) from None
        except (_RoutedNoRetryError, _RoutedRetryableError):
            await anyio.lowlevel.checkpoint()
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
                await router.settle_unknown(attempt, "transport_error", http_status=status)
            else:
                await router.settle_not_sent(attempt)
            await anyio.lowlevel.checkpoint()
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
        from lionagi.service.accounts._pricing import stream_usage_estimate, usage_outcome
        from lionagi.service.accounts._router import (
            StreamingSecretRedactor,
            StreamTextRedactor,
            get_router,
            header_pairs,
            redact_value,
            routed_client_kwargs,
            validate_routed_request,
        )
        from lionagi.service.accounts._types import (
            AccountRoutingError,
            CompleteUsage,
            RejectionZero,
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
        status = None
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
        accounting = _AccountingStreamParser(attempt.key, type(self))
        settled = False  # set before each settlement write, so no handler writes a second
        settlement_write = False

        def accounting_usage():
            if not accounting.output_seen or not accounting.output_valid:
                return None
            if accounting.model is None:
                return None
            return usage_outcome(
                router.revision,
                attempt.ttls,
                accounting.model,
                accounting.usage,
                accounting.request_id,
            )

        def early_estimate(reason):
            try:
                inputs = attempt.reservation.quote.pricing_inputs
                if inputs is None or router.revision.id != attempt.reservation.quote.price_revision:
                    return None
                complete = accounting_usage()
                from lionagi.service.accounts._types import UsageCounts

                if not isinstance(complete, CompleteUsage):
                    return None
                counts = UsageCounts(
                    complete.input_tokens,
                    complete.cache_read_input_tokens,
                    complete.cache_write_5m_tokens,
                    complete.cache_write_1h_tokens,
                    complete.output_tokens,
                )
                return stream_usage_estimate(
                    router.revision,
                    inputs,
                    complete.served_model,
                    counts,
                    accounting.request_id,
                    reason,
                )
            except Exception as error:
                logger.debug("stream usage estimate unavailable: %s", type(error).__name__)
                return None

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
                return push(event, data, places, False, pieces, shown, sse_event)
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
            return push(event, data, places, rewrite, pieces, shown, sse_event)

        def push(event, data, places, rewrite, pieces, shown, sse_event=None) -> list:
            if shown is None:
                return text_redactor.push((event, data, places, rewrite, None, sse_event), pieces)
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
                (event, data, places, rewrite, shown.type, sse_event),
                pieces,
                (shown.type, source),
            )

        async def emit_ready(ready: list):
            for (event, data, places, rewrite, shown_type, sse_event), texts in ready:
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
                chunk = self._line_to_stream_chunk(data, sse_event=sse_event)
                if chunk is not None:
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
                        # Both writes are shielded from cancellation. The block goes first
                        # because a ledger error on the first write stops the second from
                        # running: losing the attempt's reason only keeps its ceiling held,
                        # while losing the block would leave the refused key free to be
                        # reserved again. The attempt counts as settled before the writes, so
                        # a cancellation arriving during them does not record a second outcome.
                        settled = True
                        await router.block(attempt, "auth_required")
                        await router.settle_unknown(attempt, "auth_required", http_status=status)
                        raise AccountRoutingError(
                            RouterErrorCode.AUTH_FAILED,
                            account=attempt.account,
                            attempt_id=attempt.attempt_id,
                            http_status=status,
                        ) from None
                    if status == 429:
                        settled = True
                        await router.settle_unknown(attempt, "rate_limited", http_status=status)
                        raise _RoutedRetryableError(
                            RouterErrorCode.RATE_LIMITED.value,
                            account=attempt.account,
                            attempt_id=attempt.attempt_id,
                            http_status=status,
                        )
                    if status >= 500:
                        settled = True
                        # A 529 whose body names ``overloaded_error`` settles at zero; every
                        # other 5xx, and a 529 body that does not name it, keeps the ceiling.
                        # The raised error, and so the retry, is the same either way.
                        overloaded = False
                        if status == 529:
                            # The body read is shielded and bounded by _REJECTION_READ_SECONDS,
                            # so a 529 whose body stalls raises after at most that long.
                            error_type, _ = await _classify_streamed_rejection(response)
                            overloaded = error_type == "overloaded_error"
                        if overloaded:
                            await router.settle_rejection(
                                attempt,
                                RejectionZero(529, "overloaded_error", None, "provider_error"),
                                http_status=status,
                            )
                        else:
                            await router.settle_unknown(
                                attempt, "provider_error", http_status=status
                            )
                        raise _RoutedRetryableError(
                            RouterErrorCode.PROVIDER_ERROR.value,
                            account=attempt.account,
                            attempt_id=attempt.attempt_id,
                            http_status=status,
                        )
                    if status != 200:
                        settled = True
                        # Only the two names leave the classifier: it reads the body in its own
                        # frame and no local here holds it.
                        (
                            provider_error_type,
                            rejection_class,
                        ) = await _classify_streamed_rejection(response)
                        # Only a 400 naming ``invalid_request_error`` settles at zero, whatever
                        # its class; every other rejection keeps the ceiling.
                        if status == 400 and provider_error_type == "invalid_request_error":
                            await router.settle_rejection(
                                attempt,
                                RejectionZero(
                                    400,
                                    "invalid_request_error",
                                    rejection_class,
                                    "request_rejected",
                                ),
                                http_status=status,
                            )
                        else:
                            await router.settle_unknown(
                                attempt,
                                "request_rejected",
                                provider_error_type=provider_error_type,
                                rejection_class=rejection_class,
                                http_status=status,
                            )
                        raise AccountRoutingError(
                            RouterErrorCode.REQUEST_REJECTED,
                            account=attempt.account,
                            attempt_id=attempt.attempt_id,
                            http_status=status,
                            provider_error_type=provider_error_type,
                            rejection_class=rejection_class,
                        ) from None

                    # Only a failure of the byte source itself is held back, so the events it
                    # completed are dispatched first; an error handling an event raises at once.
                    byte_stream = response.aiter_bytes().__aiter__()
                    transport_error: Exception | None = None
                    while True:
                        try:
                            raw = await byte_stream.__anext__()
                        except StopAsyncIteration:
                            break
                        except Exception as error:
                            transport_error = error
                            break
                        for accounting_event in accounting.feed(raw):
                            accounting.observe(accounting_event)
                            if accounting_event.get("type") == "message_stop" and not settled:
                                terminal = accounting_usage()
                                settled = True
                                await router.settle_stream_end(
                                    attempt, terminal, http_status=status
                                )
                        safe = redactor.feed(raw)
                        pending += decoder.decode(safe)
                        while "\n" in pending:
                            line, pending = pending.split("\n", 1)
                            for data, name in self._frame_stream_line(line, frame):
                                async for chunk in emit_ready(queue_data(data, name)):
                                    yield chunk
                    if transport_error is None:
                        # A clean end completes the last line and any held event, exactly as
                        # the endpoint framer does below; a message_stop among them settles
                        # before any of that output is yielded.
                        for accounting_event in accounting.finish():
                            accounting.observe(accounting_event)
                            if accounting_event.get("type") == "message_stop" and not settled:
                                terminal = accounting_usage()
                                settled = True
                                await router.settle_stream_end(
                                    attempt, terminal, http_status=status
                                )
                    if transport_error is not None and not settled:
                        # Settled before the first await after the failure, so a cancellation
                        # arriving now finds it settled and cannot record it as cancelled.
                        settled = True
                        estimate = early_estimate("transport_error")
                        settlement_write = True
                        await router.settle_stream_end(
                            attempt,
                            estimate,
                            reason="transport_error" if estimate is None else None,
                            http_status=status,
                        )
                        settlement_write = False
                    # The byte redactor's held tail can hold several lines, the last events
                    # of the stream among them. They are framed whether the stream ended or
                    # the connection failed; after a failure a trailing partial character is
                    # a truncated one and is dropped, not an error.
                    pending += decoder.decode(
                        redactor.feed(b"", final=True), final=transport_error is None
                    )
                    while "\n" in pending:
                        line, pending = pending.split("\n", 1)
                        for data, name in self._frame_stream_line(line, frame):
                            async for chunk in emit_ready(queue_data(data, name)):
                                yield chunk
                    if transport_error is None:
                        if pending:
                            for data, name in self._frame_stream_line(pending, frame):
                                async for chunk in emit_ready(queue_data(data, name)):
                                    yield chunk
                        if frame.data:
                            data, name = self._take_sse_event(frame)
                            async for chunk in emit_ready(queue_data(data, name)):
                                yield chunk
                    # After a failure an unterminated line or event is a truncated event and
                    # is not dispatched; the text already held from complete events is.
                    async for chunk in emit_ready(text_redactor.finish()):
                        yield chunk
                    if transport_error is not None:
                        raise transport_error
                    if not settled:
                        # The stream ended before message_stop, so its usage is incomplete.
                        reason = "stream_error" if accounting.saw_error else "stream_truncated"
                        settled = True
                        estimate = early_estimate(reason)
                        await router.settle_stream_end(
                            attempt,
                            estimate,
                            reason=reason if estimate is None else None,
                            http_status=status,
                        )
        except get_cancelled_exc_class():
            # A write that fails records nothing; the cancellation still reaches the caller.
            # As on the call path, a native task.cancel() returns before the write commits.
            try:
                if not request_entered:
                    await router.settle_not_sent(attempt, caller_cancelled=True)
                elif not settled:
                    reason = "cancelled"
                    estimate = early_estimate(reason)
                    settled = True
                    await router.settle_stream_end(
                        attempt,
                        estimate,
                        reason=reason if estimate is None else None,
                        http_status=status,
                    )
            except Exception:  # noqa: S110 - the caller must see the cancellation
                pass
            raise
        except GeneratorExit:
            # The consumer closed the stream before message_stop.
            if not settled:
                reason = "stream_error" if accounting.saw_error else "stream_closed"
                estimate = early_estimate(reason)
                settled = True
                await router.settle_stream_end(
                    attempt,
                    estimate,
                    reason=reason if estimate is None else None,
                    http_status=status,
                )
            raise
        except AccountRoutingError:
            await anyio.lowlevel.checkpoint()
            raise
        except (_RoutedNoRetryError, _RoutedRetryableError):
            if accounting.saw_error and not settled:
                reason = "stream_error"
                estimate = early_estimate(reason)
                settled = True
                await router.settle_stream_end(
                    attempt,
                    estimate,
                    reason=reason if estimate is None else None,
                    http_status=status,
                )
            await anyio.lowlevel.checkpoint()
            raise
        except Exception:
            if settlement_write:
                raise
            failure = (_RoutedRetryableError if request_entered else _RoutedNoRetryError)(
                RouterErrorCode.TRANSPORT_ERROR.value,
                account=attempt.account,
                attempt_id=attempt.attempt_id,
            )
        # Settled and raised here, outside the handler, so neither a ledger error nor
        # this error carries context: the exception it stands for can quote the request
        # or the response.
        if failure is not None:
            if not settled:
                if request_entered:
                    await router.settle_unknown(attempt, "transport_error", http_status=status)
                else:
                    await router.settle_not_sent(attempt)
            await anyio.lowlevel.checkpoint()
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
        baseline = self.config.kwargs.get("output_config")
        if isinstance(baseline, dict) and "effort" in baseline:
            merged = dict(baseline)
            for layer in (request_dict, kwargs):
                if "output_config" in layer:
                    value = layer["output_config"]
                    if value is None:
                        continue
                    if isinstance(value, dict):
                        merged.update(
                            {k: v for k, v in value.items() if k != "effort" or v is not None}
                        )
                    else:
                        raise ValueError(
                            "output_config must be a mapping when effort is configured"
                        )
            kwargs["output_config"] = merged
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
