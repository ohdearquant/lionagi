# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import Field, field_validator

from lionagi.ln._utils import now_utc

from .base import SenderRecipient
from .message import Message, MessageContent, MessageRole


@dataclass(slots=True)
class SystemContent(MessageContent):
    """Content for system messages with optional datetime prefix."""

    system_message: str = "You are a helpful AI assistant. Let's think step by step."
    system_datetime: str | None = None

    @property
    def rendered(self) -> str:
        """Render system message, prepending datetime if set."""
        parts = []
        if self.system_datetime:
            parts.append(f"System Time: {self.system_datetime}")
        parts.append(self.system_message)
        return "\n\n".join(parts)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SystemContent:
        """Construct SystemContent from a dict; system_datetime=True generates current UTC time."""
        system_message = data.get(
            "system_message",
            cls.__dataclass_fields__["system_message"].default,
        )
        system_datetime = data.get("system_datetime")

        # Handle datetime generation
        if system_datetime is True:
            system_datetime = now_utc().isoformat(timespec="minutes")
        elif system_datetime is False or system_datetime is None:
            system_datetime = None

        return cls(system_message=system_message, system_datetime=system_datetime)


class System(Message):
    """System-level message setting context or policy for the conversation."""

    _role: ClassVar[MessageRole] = MessageRole.SYSTEM
    content: SystemContent = Field(default_factory=SystemContent)
    sender: SenderRecipient | None = MessageRole.SYSTEM
    recipient: SenderRecipient | None = MessageRole.ASSISTANT

    @field_validator("content", mode="before")
    def _validate_content(cls, v):
        if v is None:
            return SystemContent()
        if isinstance(v, dict):
            return SystemContent.from_dict(v)
        if isinstance(v, SystemContent):
            return v
        raise TypeError("content must be dict or SystemContent instance")
