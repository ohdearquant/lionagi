# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""``ScriptedEndpoint`` — registered ``provider="scripted"`` that serves canned responses instead of HTTP."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any, ClassVar

import aiohttp
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from lionagi.service.connections.agentic_endpoint import AgenticEndpoint
from lionagi.service.connections.endpoint import Endpoint
from lionagi.service.connections.registry import EndpointType, register_endpoint
from lionagi.service.types.stream_chunk import StreamChunk

from ._script import ScriptModel
from ._types import (
    ErrorResponse,
    RecordedCall,
    ResponseEntry,
    StreamChunkSpec,
    StreamResponse,
    StructuredResponse,
    TextResponse,
    ToolCallResponse,
)

logger = logging.getLogger(__name__)

__all__ = ("ScriptedEndpoint",)

# Env-var names. Kept in one place so tests and CLI agree.
ENV_SCRIPT_PATH = "LIONAGI_TEST_SCRIPT"


@register_endpoint(
    provider="scripted",
    endpoint="chat/completions",
    aliases=["chat", "query_cli", "cli"],
    endpoint_type=EndpointType.AGENTIC,
    base_url="internal",
    auth_type="bearer",
)
class ScriptedEndpoint(AgenticEndpoint):
    """Endpoint that serves responses from a ``ScriptModel`` instead of HTTP (calls via ``endpoint.calls``)."""

    # Inherits is_cli=True, DEFAULT_QUEUE_CAPACITY=10, DEFAULT_CONCURRENCY_LIMIT=3
    # from AgenticEndpoint. Overriding here only to make them explicit.
    is_cli: ClassVar[bool] = True
    DEFAULT_QUEUE_CAPACITY: ClassVar[int] = 10
    DEFAULT_CONCURRENCY_LIMIT: ClassVar[int] = 3

    def __init__(self, config: Any = None, **kwargs: Any) -> None:
        # ── pop our test-only kwargs BEFORE super so EndpointConfig doesn't
        # see them. EndpointConfig drops unknown keys into ``config.kwargs``
        # which then leak into request payloads.
        script = kwargs.pop("script", None)
        script_was_passed = script is not None
        if script is None:
            script = self._resolve_script_from_env()

        # Default api_key so HeaderFactory doesn't complain; this is never used.
        kwargs.setdefault("api_key", "scripted-test-key")
        # Don't compute tokens against a dummy model.
        kwargs.setdefault("requires_tokens", False)

        super().__init__(config, **kwargs)

        if script:
            self._script: ScriptModel = ScriptModel.coerce(script)
        else:
            self._script = ScriptModel()
            if not script_was_passed:
                # No script kwarg AND no env var — the first call will raise
                # a confusing "exhausted" error several layers downstream.
                # Surface it now so tests fail with a clear message.
                logger.warning(
                    "ScriptedEndpoint constructed with no script and no "
                    "%s env var. The first call will raise "
                    "ScriptExhaustedError. Pass script= or set the env var.",
                    ENV_SCRIPT_PATH,
                )
        self.calls: list[RecordedCall] = []

    # ─────────────────────────────── public api ───────────────────────────

    @property
    def script(self) -> ScriptModel:
        return self._script

    def attach_script(self, source: Any) -> None:
        """Replace the script in place. Useful when constructing via
        ``match_endpoint`` and configuring later."""
        self._script = ScriptModel.coerce(source)
        self._script.reset()

    def clear_calls(self) -> None:
        self.calls.clear()

    # ─────────────────────────────── overrides ────────────────────────────

    async def _call(
        self, payload: dict[str, Any], headers: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        """Serve from the script instead of making an HTTP request."""
        entry, matched_by = self._script.next(payload, len(self.calls))

        if isinstance(entry, ErrorResponse):
            if entry.delay_ms:
                await asyncio.sleep(entry.delay_ms / 1000)
            self._record(payload, headers, entry, response=None, matched_by=matched_by)
            raise _build_exception(entry)

        if isinstance(entry, StreamResponse):
            # Caller is using _call (non-streaming) but script has a stream
            # entry. Concatenate text chunks into a single response.
            text = "".join(c.content or "" for c in entry.chunks if c.type == "text")
            response = _openai_text_response(text, payload)
            self._record(payload, headers, entry, response=response, matched_by=matched_by)
            return response

        response = _entry_to_openai(entry, payload)
        self._record(payload, headers, entry, response=response, matched_by=matched_by)
        return response

    async def stream(
        self,
        request: Any,
        extra_headers: dict | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[StreamChunk]:
        """Yield StreamChunk objects from the script (no HTTP, no SSE parsing)."""
        payload, headers = self.create_payload(request, extra_headers=extra_headers, **kwargs)
        entry, matched_by = self._script.next(payload, len(self.calls))

        if isinstance(entry, ErrorResponse):
            if entry.delay_ms:
                await asyncio.sleep(entry.delay_ms / 1000)
            self._record(
                payload, headers, entry, response=None, matched_by=matched_by, streamed=True
            )
            yield StreamChunk(type="error", content=entry.message, is_error=True)
            raise _build_exception(entry)

        if isinstance(entry, StreamResponse):
            chunks_out: list[StreamChunk] = []
            for spec in entry.chunks:
                chunk = _spec_to_chunk(spec)
                chunks_out.append(chunk)
                yield chunk
            self._record(
                payload,
                headers,
                entry,
                response=chunks_out,
                matched_by=matched_by,
                streamed=True,
            )
            return

        # Non-stream entry served via stream(): wrap as a single text chunk.
        if isinstance(entry, TextResponse):
            yield StreamChunk(type="text", content=entry.content, is_delta=True)
            yield StreamChunk(type="result", metadata={"done": True})
            self._record(
                payload,
                headers,
                entry,
                response=entry.content,
                matched_by=matched_by,
                streamed=True,
            )
            return

        # tool_call / structured streamed as a single message-level chunk.
        response = _entry_to_openai(entry, payload)
        self._record(
            payload, headers, entry, response=response, matched_by=matched_by, streamed=True
        )
        yield StreamChunk(type="result", content=json.dumps(response), metadata={"raw": response})

    # ─────────────────────────────── internals ────────────────────────────

    def _record(
        self,
        payload: dict[str, Any],
        headers: dict[str, Any],
        entry: ResponseEntry,
        *,
        response: Any,
        matched_by: str,
        streamed: bool = False,
    ) -> None:
        self.calls.append(
            RecordedCall(
                index=len(self.calls),
                payload=dict(payload),
                headers=dict(headers),
                response_type=entry.type,
                response=response,
                was_streamed=streamed,
                matched_by=matched_by,
            )
        )

    @staticmethod
    def _resolve_script_from_env() -> Any | None:
        import os

        path = os.environ.get(ENV_SCRIPT_PATH)
        if path:
            return path
        return None

    def copy_runtime_state_to(self, other: Endpoint) -> None:
        """Carry script + recorded calls across an ``iModel.copy()`` clone.

        The script is **deep-copied** so the clone gets an independent cursor
        — otherwise positional matching would cross-contaminate (clone A
        consuming entry 0, clone B getting entry 1 instead of 0). Recorded
        calls are shallow-copied for inspection but each clone records its
        own future calls.
        """
        super().copy_runtime_state_to(other)
        if isinstance(other, ScriptedEndpoint):
            other._script = copy.deepcopy(self._script)
            other._script.reset()
            other.calls = list(self.calls)


# ─────────────────────────── response formatting ─────────────────────────


def _openai_text_response(text: str, payload: dict[str, Any]) -> dict[str, Any]:
    model = payload.get("model") or "scripted"
    return {
        "id": f"chatcmpl-scripted-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _entry_to_openai(entry: ResponseEntry, payload: dict[str, Any]) -> dict[str, Any]:
    """Convert a ResponseEntry to an OpenAI-chat-completions response dict."""

    if isinstance(entry, TextResponse):
        return _openai_text_response(entry.content, payload)

    if isinstance(entry, ToolCallResponse):
        tool_call_id = entry.id or f"call_{uuid.uuid4().hex[:10]}"
        return {
            "id": f"chatcmpl-scripted-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": 0,
            "model": payload.get("model") or "scripted",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": entry.name,
                                    "arguments": json.dumps(entry.arguments),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    if isinstance(entry, StructuredResponse):
        # Serialize the structured payload as a JSON string in the message
        # content so `branch.parse()` validates it through its normal path.
        return _openai_text_response(json.dumps(entry.data), payload)

    raise TypeError(f"unhandled response entry: {type(entry).__name__}")


def _spec_to_chunk(spec: StreamChunkSpec) -> StreamChunk:
    return StreamChunk(
        type=spec.type,
        content=spec.content,
        tool_name=spec.tool_name,
        tool_id=spec.tool_id,
        tool_input=spec.tool_input,
        tool_output=spec.tool_output,
        is_error=spec.is_error,
        is_delta=spec.is_delta,
        metadata=dict(spec.metadata),
    )


_STATUS_FOR_KIND = {"rate_limit": 429, "server_error": 500, "bad_request": 400}


def _build_exception(entry: ErrorResponse) -> Exception:
    """Map an ErrorResponse to a realistic exception type."""
    if entry.kind in _STATUS_FOR_KIND:
        request_info = aiohttp.RequestInfo(
            URL("https://scripted.invalid"),
            "POST",
            CIMultiDictProxy(CIMultiDict()),
        )
        return aiohttp.ClientResponseError(
            request_info=request_info,
            history=(),
            status=_STATUS_FOR_KIND[entry.kind],
            message=entry.message,
        )
    if entry.kind == "timeout":
        return asyncio.TimeoutError(entry.message)
    return ValueError(entry.message)
