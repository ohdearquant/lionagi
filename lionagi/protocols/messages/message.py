# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import Field, PrivateAttr, field_serializer, field_validator

from lionagi.ln.types import DataClass, ModelConfig

from .._concepts import Sendable
from ..graph.node import Node
from .base import (
    MessageRole,
    SenderRecipient,
    serialize_sender_recipient,
    validate_sender_recipient,
)


class _TrackedList(list):
    """List that bumps its owning content revision after mutation."""

    __slots__ = ("_touch",)

    def __init__(self, values=(), touch: Callable[[], None] | None = None):
        self._touch = touch
        super().__init__(_track_mutable(value, touch) for value in values)

    def _changed(self) -> None:
        if self._touch is not None:
            self._touch()

    def __reduce__(self):
        # Copy/pickle as a plain list: the revision callback must not
        # survive reconstruction (the owning content re-wraps on restore).
        return (list, (list(self),))

    def append(self, value) -> None:
        super().append(_track_mutable(value, self._touch))
        self._changed()

    def extend(self, values) -> None:
        super().extend(_track_mutable(value, self._touch) for value in values)
        self._changed()

    def insert(self, index, value) -> None:
        super().insert(index, _track_mutable(value, self._touch))
        self._changed()

    def __setitem__(self, index, value) -> None:
        if isinstance(index, slice):
            value = [_track_mutable(item, self._touch) for item in value]
        else:
            value = _track_mutable(value, self._touch)
        super().__setitem__(index, value)
        self._changed()

    def __delitem__(self, index) -> None:
        super().__delitem__(index)
        self._changed()

    def clear(self) -> None:
        super().clear()
        self._changed()

    def pop(self, index=-1):
        value = super().pop(index)
        self._changed()
        return value

    def remove(self, value) -> None:
        super().remove(value)
        self._changed()

    def reverse(self) -> None:
        super().reverse()
        self._changed()

    def sort(self, *args, **kwargs) -> None:
        super().sort(*args, **kwargs)
        self._changed()

    def __iadd__(self, values):
        self.extend(values)
        return self

    def __imul__(self, value):
        super().__imul__(value)
        self._changed()
        return self


class _TrackedDict(dict):
    """Dict that bumps its owning content revision after mutation."""

    __slots__ = ("_touch",)

    def __init__(self, values=(), touch: Callable[[], None] | None = None, **kwargs):
        self._touch = touch
        super().__init__()
        self.update(values, **kwargs)

    def _changed(self) -> None:
        if self._touch is not None:
            self._touch()

    def __reduce__(self):
        # Copy/pickle as a plain dict; see _TrackedList.__reduce__.
        return (dict, (dict(self),))

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, _track_mutable(value, self._touch))
        self._changed()

    def __delitem__(self, key) -> None:
        super().__delitem__(key)
        self._changed()

    def clear(self) -> None:
        super().clear()
        self._changed()

    def pop(self, key, *args):
        value = super().pop(key, *args)
        self._changed()
        return value

    def popitem(self):
        value = super().popitem()
        self._changed()
        return value

    def setdefault(self, key, default=None):
        if key in self:
            return super().setdefault(key, default)
        value = _track_mutable(default, self._touch)
        super().__setitem__(key, value)
        self._changed()
        return value

    def update(self, *args, **kwargs) -> None:
        values = dict(*args, **kwargs)
        for key, value in values.items():
            dict.__setitem__(self, key, _track_mutable(value, self._touch))
        if values:
            self._changed()

    def __ior__(self, values):
        self.update(values)
        return self


def _track_mutable(value: Any, touch: Callable[[], None] | None) -> Any:
    """Copy mutable render inputs into revision-aware containers."""
    if isinstance(value, _TrackedList):
        value = list(value)
    elif isinstance(value, _TrackedDict):
        value = dict(value)

    if isinstance(value, list):
        return _TrackedList(value, touch)
    if isinstance(value, dict):
        return _TrackedDict(value, touch)
    return value


@dataclass(slots=True)
class MessageContent(DataClass):
    """A base class for message content structures."""

    _config: ClassVar[ModelConfig] = ModelConfig(none_as_sentinel=True)
    _revision: int = field(default=0, init=False, repr=False, compare=False)

    def __setattr__(self, name: str, value: Any) -> None:
        track_mutation = not name.startswith("_") and hasattr(self, "_revision")
        if not name.startswith("_"):
            value = _track_mutable(value, self._touch_revision)
        object.__setattr__(self, name, value)
        if track_mutation:
            self._touch_revision()

    def __post_init__(self) -> None:
        object.__setattr__(self, "_revision", 0)
        DataClass.__post_init__(self)
        self._track_render_inputs()
        object.__setattr__(self, "_revision", 0)

    def _track_render_inputs(self) -> None:
        for name in self.allowed():
            object.__setattr__(
                self,
                name,
                _track_mutable(getattr(self, name), self._touch_revision),
            )

    def _touch_revision(self) -> None:
        object.__setattr__(self, "_revision", self._revision + 1)

    @property
    def _render_revision(self) -> int:
        """Internal revision used to invalidate rendered-message cache entries."""
        return self._revision

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
    _render_cache: dict[str, tuple[Any, int, Any]] = PrivateAttr(default_factory=dict)

    def __getstate__(self) -> dict[str, Any]:
        # A clone must start uncached: the copied entry would hold the source
        # content/revision pair and never be servable.
        state = super().__getstate__()
        private = state.get("__pydantic_private__")
        if private and private.get("_render_cache"):
            private = dict(private)
            private["_render_cache"] = {}
            state = {**state, "__pydantic_private__": private}
        return state

    def __deepcopy__(self, memo: dict | None = None) -> "Message":
        clone = super().__deepcopy__(memo)
        private = getattr(clone, "__pydantic_private__", None)
        if private is not None and private.get("_render_cache"):
            private["_render_cache"] = {}
        return clone

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
        return self._chat_msg()

    def _chat_msg(self, *, use_render_cache: bool = True) -> dict[str, Any] | None:
        """Build a provider chat message, reusing the stable content rendering when safe."""
        try:
            role_str = self.role.value if isinstance(self.role, MessageRole) else str(self.role)
            return {
                "role": role_str,
                "content": self._render_cached("chat", lambda: self.rendered)
                if use_render_cache
                else self.rendered,
            }
        except Exception:
            return None

    def _render_cached(self, variant: str, render: Callable[[], Any]) -> Any:
        """Return a rendering cached by content identity and revision (not
        id(), which could cross-wire non-overlapping objects reusing an address)."""
        content = self.content
        revision = getattr(content, "_render_revision", 0)
        cached = self._render_cache.get(variant)
        if cached is not None and cached[0] is content and cached[1] == revision:
            return _copy_rendered(cached[2])

        rendered = render()
        self._render_cache[variant] = (content, revision, _copy_rendered(rendered))
        return rendered

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


def _copy_rendered(rendered: Any) -> Any:
    """Keep cached structured content isolated from provider-side mutation."""
    if isinstance(rendered, list | dict):
        return deepcopy(rendered)
    return rendered


RoledMessage = Message


# File: lionagi/protocols/messages/message.py
