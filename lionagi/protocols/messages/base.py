# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from enum import Enum
from typing import Any, TypeAlias
from uuid import UUID

from ..generic.element import ID, Element, Observable

__all__ = (
    "MessageRole",
    "MessageField",
    "MESSAGE_FIELDS",
    "validate_sender_recipient",
    "serialize_sender_recipient",
    "_coerce_id_field",
    "_unwrap_action_data",
)


class MessageRole(str, Enum):
    """Predefined roles for conversation participants or message semantics."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    UNSET = "unset"
    ACTION = "action"


SenderRecipient: TypeAlias = MessageRole | str | UUID
"""Sender/recipient: a UUID, string ID, or MessageRole enum value."""


class MessageField(str, Enum):
    """Common field names used in message objects."""

    CREATED_AT = "created_at"
    ROLE = "role"
    CONTENT = "content"
    ID = "id"
    SENDER = "sender"
    RECIPIENT = "recipient"
    METADATA = "metadata"


MESSAGE_FIELDS = [i.value for i in MessageField.__members__.values()]


def validate_sender_recipient(value: Any, /) -> SenderRecipient:
    """Normalize a sender/recipient to MessageRole, UUID, or string; raises ValueError if unrecognized."""
    if isinstance(value, MessageRole):
        return value

    if isinstance(value, UUID):
        return value

    if isinstance(value, Observable):
        return value.id

    if value is None:
        return MessageRole.UNSET

    if value in ["system", "user", "unset", "assistant", "action"]:
        return MessageRole(value)

    # Accept plain strings (user names, identifiers, etc)
    if isinstance(value, str):
        # Try to parse as ID first, but allow plain strings as fallback
        try:
            return ID.get_id(value)
        except Exception:
            return value

    raise ValueError("Invalid sender or recipient")


def serialize_sender_recipient(value: Any) -> str | None:
    if not value:
        return None
    # Check instance types first before enum membership
    if isinstance(value, Element):
        return str(value.id)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, MessageRole):
        return value.value
    if isinstance(value, str):
        return value
    return str(value)


def _coerce_id_field(data: dict[str, Any], key: str) -> str | None:
    """Return data[key] coerced to str, or None if absent/falsy."""
    val = data.get(key)
    if val:
        return str(val)
    return None


def _unwrap_action_data(data: dict[str, Any], nested_key: str) -> tuple[str, dict[str, Any]]:
    """Extract function and arguments from data, supporting a legacy nested-key wrapper."""
    if nested_key in data:
        inner = data[nested_key]
        function = inner.get("function", "")
        arguments = inner.get("arguments", {})
    else:
        function = data.get("function", "")
        arguments = data.get("arguments", {})
    return function, arguments


# File: lionagi/protocols/messages/base.py
