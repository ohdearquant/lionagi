# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import Field, field_serializer, field_validator

from lionagi.ln.types import DataClass, ModelConfig

from .._concepts import Sendable
from ..graph.node import Node
from .base import (
    MessageRole,
    SenderRecipient,
    serialize_sender_recipient,
    validate_sender_recipient,
)


@dataclass(slots=True)
class MessageContent(DataClass):
    """A base class for message content structures."""

    _config: ClassVar[ModelConfig] = ModelConfig(none_as_sentinel=True)

    @property
    def rendered(self) -> str:
        """Render the content as a string."""
        raise NotImplementedError("Subclasses must implement rendered property.")

    def render(self, *_args: Any, **_kwargs: Any) -> str:
        return self.rendered

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MessageContent":
        """Create an instance from a dictionary."""
        raise NotImplementedError("Subclasses must implement from_dict method.")


class Message(Node, Sendable):
    """Base class for all messages; subclasses fix their role via _role ClassVar."""

    _role: ClassVar[MessageRole] = MessageRole.UNSET
    _content_type: ClassVar[type] = MessageContent

    content: Any = None

    @field_validator("content", mode="before")
    @classmethod
    def _validate_content(cls, v: Any) -> Any:
        t = cls._content_type
        # base Message is a generic envelope (content: Any); only roled subclasses coerce
        if t is MessageContent:
            return v
        if v is None:
            return t()
        if isinstance(v, dict):
            return t.from_dict(v)
        if isinstance(v, t):
            return v
        raise TypeError(f"content must be dict or {t.__name__} instance")

    def __init__(self, **kwargs):
        kwargs.pop("role", None)
        super().__init__(**kwargs)

    sender: SenderRecipient | None = MessageRole.UNSET
    recipient: SenderRecipient | None = MessageRole.UNSET
    channel: str | None = Field(
        None, description="Optional namespace for message grouping/filtering"
    )

    @property
    def role(self) -> MessageRole:
        return self.__class__._role

    @field_serializer("sender", "recipient")
    def _serialize_sender_recipient(self, value: SenderRecipient) -> str:
        return serialize_sender_recipient(value)

    @field_validator("sender", "recipient")
    def _validate_sender_recipient(cls, v):
        if v is None:
            return None
        return validate_sender_recipient(v)

    def to_dict(self, mode="python", **kw):
        d = super().to_dict(mode=mode, **kw)
        d["role"] = self.role.value if isinstance(self.role, MessageRole) else str(self.role)
        return d

    def model_dump(self, **kw):
        d = super().model_dump(**kw)
        d["role"] = self.role.value if isinstance(self.role, MessageRole) else str(self.role)
        return d

    @property
    def is_broadcast(self) -> bool:
        """True if no specific recipient (broadcast to all)."""
        return self.recipient is None

    @property
    def is_direct(self) -> bool:
        """True if has specific recipient (point-to-point)."""
        return self.recipient is not None

    @property
    def chat_msg(self) -> dict[str, Any] | None:
        """A dictionary representation typically used in chat-based contexts."""
        try:
            role_str = self.role.value if isinstance(self.role, MessageRole) else str(self.role)
            return {"role": role_str, "content": self.rendered}
        except Exception:
            return None

    @property
    def rendered(self) -> str:
        """Render the message content as a string, delegating to content.rendered if available."""
        if hasattr(self.content, "rendered"):
            return self.content.rendered
        return str(self.content) if self.content is not None else ""

    def update(self, sender=None, recipient=None, **kw):
        """Update sender, recipient, and/or content fields in place."""
        if sender:
            self.sender = validate_sender_recipient(sender)
        if recipient:
            self.recipient = validate_sender_recipient(recipient)
        if kw and hasattr(self.content, "to_dict"):
            _dict = self.content.to_dict()
            _dict.update(kw)
            self.content = type(self.content).from_dict(_dict)

    def clone(self) -> "Message":
        """Create a clone with a new ID but reference to original."""
        data = self.to_dict()
        original_id = data.pop("id")
        data.pop("created_at")
        data.pop("role", None)

        cloned = type(self).from_dict(data)
        cloned.metadata["clone_from"] = str(original_id)
        return cloned

    @property
    def image_content(self) -> list[dict[str, Any]] | None:
        """Extract structured image data from the message content."""
        msg_ = self.chat_msg
        if isinstance(msg_, dict) and isinstance(msg_["content"], list):
            return [i for i in msg_["content"] if i["type"] == "image_url"]
        return None


RoledMessage = Message


# File: lionagi/protocols/messages/message.py
