# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Internal types for ``lionagi.testing`` — response entries, matchers, recorded calls.

These are the parsed shape behind the YAML/dict fixture format documented in
``ScriptModel``. End users should only need ``lionagi.testing.TestBranch`` /
``ScriptModel``; everything here is an implementation detail.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Union

from pydantic import BaseModel, Field

ResponseType = Literal["text", "tool_call", "structured", "stream", "error"]

ErrorKind = Literal["rate_limit", "timeout", "server_error", "bad_request", "value_error"]


class WhenMatcher(BaseModel):
    """Declarative match condition.

    All conditions are AND-ed. An empty matcher (no fields set) matches nothing —
    callers should treat that as "use positional cursor."
    """

    model_config = {"extra": "forbid"}

    prompt_contains: str | None = None
    prompt_regex: str | None = None
    has_tool: str | None = None  # request includes a tool with this name
    after_calls: int | None = None  # only match after N prior calls
    call_index: int | None = None  # only match on the Nth call (0-indexed)

    def is_empty(self) -> bool:
        return all(getattr(self, f) is None for f in type(self).model_fields)


class _BaseResponse(BaseModel):
    """Base for response entries. The ``type`` discriminator lives on subclasses."""

    model_config = {"extra": "forbid"}

    when: WhenMatcher | None = None


class TextResponse(_BaseResponse):
    type: Literal["text"] = "text"
    content: str


class ToolCallResponse(_BaseResponse):
    """Model requests a tool call. ``arguments`` is serialized to JSON in the payload."""

    type: Literal["tool_call"] = "tool_call"
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    id: str | None = None  # tool_call id (auto-generated if not provided)


class StructuredResponse(_BaseResponse):
    """A structured/JSON response. ``data`` is serialized as the message content
    so ``branch.parse()`` can validate it against the request's schema."""

    type: Literal["structured"] = "structured"
    data: dict[str, Any]


class StreamChunkSpec(BaseModel):
    """One chunk inside a StreamResponse. Maps 1:1 to ``StreamChunk``."""

    model_config = {"extra": "forbid"}

    type: Literal["system", "thinking", "text", "tool_use", "tool_result", "result", "error"]
    content: str | None = None
    tool_name: str | None = None
    tool_id: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: Any = None
    is_error: bool = False
    is_delta: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class StreamResponse(_BaseResponse):
    """Multi-chunk streaming response for agentic/CLI endpoints."""

    type: Literal["stream"] = "stream"
    chunks: list[StreamChunkSpec]


class ErrorResponse(_BaseResponse):
    """Inject an exception at this call site."""

    type: Literal["error"] = "error"
    kind: ErrorKind = "value_error"
    message: str = "scripted error"
    delay_ms: int = 0


ResponseEntry = Union[
    TextResponse,
    ToolCallResponse,
    StructuredResponse,
    StreamResponse,
    ErrorResponse,
]


@dataclass(slots=True)
class RecordedCall:
    """One observed call against a ScriptedEndpoint.

    Tests inspect ``endpoint.calls`` to assert on what the agent actually sent —
    messages, tool definitions, model, system content, etc.
    """

    index: int
    payload: dict[str, Any]
    headers: dict[str, Any]
    response_type: str
    response: Any
    was_streamed: bool = False
    matched_by: str = "positional"
    when_index: int | None = None  # if matched via when:, the response entry's index

    @property
    def last_user_message(self) -> str | None:
        msgs = self.payload.get("messages") or []
        for msg in reversed(msgs):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts = [p.get("text", "") for p in content if isinstance(p, dict)]
                    return "".join(parts) or None
        return None

    @property
    def system_message(self) -> str | None:
        msgs = self.payload.get("messages") or []
        for msg in msgs:
            if isinstance(msg, dict) and msg.get("role") in {"system", "developer"}:
                content = msg.get("content")
                if isinstance(content, str):
                    return content
        return None

    @property
    def tool_names(self) -> list[str]:
        tools = self.payload.get("tools") or []
        names: list[str] = []
        for t in tools:
            if isinstance(t, dict):
                fn = t.get("function") or {}
                if name := fn.get("name") or t.get("name"):
                    names.append(name)
        return names


__all__ = (
    "ErrorKind",
    "ErrorResponse",
    "RecordedCall",
    "ResponseEntry",
    "ResponseType",
    "StreamChunkSpec",
    "StreamResponse",
    "StructuredResponse",
    "TextResponse",
    "ToolCallResponse",
    "WhenMatcher",
)
