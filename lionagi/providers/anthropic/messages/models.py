# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Anthropic API models for request/response validation."""

from __future__ import annotations

from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
        """Convert string content to proper format."""
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
