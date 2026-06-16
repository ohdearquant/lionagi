# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ChunkType = Literal[
    "system",  # session init (model, session_id, tools)
    "thinking",  # reasoning trace
    "text",  # assistant text content
    "tool_use",  # model requests a tool call
    "tool_result",  # tool execution output
    "result",  # final aggregated result
    "error",  # error
]


@dataclass(slots=True)
class StreamChunk:
    """Provider-agnostic streaming chunk; endpoints normalize to this, operations build Messages from it."""

    type: ChunkType
    content: str | None = None
    tool_name: str | None = None
    tool_id: str | None = None
    tool_input: dict[str, Any] | None = None
    tool_output: Any | None = None
    is_error: bool = False
    is_delta: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
