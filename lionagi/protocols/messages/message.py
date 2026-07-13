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
    # Memoized "fully JSON-safe" verdict, keyed by (content identity, tracked
    # revision) — see `_content_is_render_safe`. Only ever holds a *safe*
    # verdict; an unsafe (untracked-mutable) verdict is never cached.
    _untracked_mutable_safe: tuple[Any, int] | None = PrivateAttr(default=None)

    def __getstate__(self) -> dict[str, Any]:
        # A clone must start uncached: the copied entry would hold the source
        # content/revision pair and never be servable.
        state = super().__getstate__()
        private = state.get("__pydantic_private__")
        if private and (private.get("_render_cache") or private.get("_untracked_mutable_safe")):
            private = dict(private)
            private["_render_cache"] = {}
            private["_untracked_mutable_safe"] = None
            state = {**state, "__pydantic_private__": private}
        return state

    def __deepcopy__(self, memo: dict | None = None) -> "Message":
        clone = super().__deepcopy__(memo)
        private = getattr(clone, "__pydantic_private__", None)
        if private is not None:
            if private.get("_render_cache"):
                private["_render_cache"] = {}
            if private.get("_untracked_mutable_safe"):
                private["_untracked_mutable_safe"] = None
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
        id(), which could cross-wire non-overlapping objects reusing an address).
        Bypasses the cache entirely when content holds a value the revision
        tracker cannot observe in-place mutation of."""
        content = self.content
        if not self._content_is_render_safe(content):
            return render()

        revision = getattr(content, "_render_revision", 0)
        cached = self._render_cache.get(variant)
        if cached is not None and cached[0] is content and cached[1] == revision:
            return _copy_rendered(cached[2])

        rendered = render()
        self._render_cache[variant] = (content, revision, _copy_rendered(rendered))
        return rendered

    def _content_is_render_safe(self, content: Any) -> bool:
        """True if `content` is fully JSON-safe (no untracked-mutable value
        reachable at any depth, including dict keys) — memoized per
        (content identity, tracked revision) so a warm JSON-safe content is
        walked at most once per revision instead of on every render.

        Only the *safe* verdict is ever cached. An untracked-mutable object
        can mutate without bumping the tracked revision (that is the whole
        reason the render cache must bypass for it), so a cached *unsafe*
        verdict has no revision to reliably invalidate on; recomputing it
        every call is the only way to keep it honest. Content that never
        clears the walk is presumably rare, so re-walking it is cheap in
        practice.
        """
        revision = getattr(content, "_render_revision", 0)
        verdict = self._untracked_mutable_safe
        if verdict is not None and verdict[0] is content and verdict[1] == revision:
            return True

        safe = not _content_has_untracked_mutable(content)
        if safe:
            self._untracked_mutable_safe = (content, revision)
        return safe

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


_JSON_SAFE_LEAF_TYPES = (type(None), bool, int, float, str, bytes)
_UNTRACKED_MUTABLE_MAX_DEPTH = 1000


class _ExitFrame:
    """Stack marker: pops `obj_id` off `on_path` once all of its children
    have been visited, so a later *sibling* reference to the same container
    is not mistaken for a cycle back through an ancestor."""

    __slots__ = ("obj_id",)

    def __init__(self, obj_id: int) -> None:
        self.obj_id = obj_id


def _has_untracked_mutable(root: Any) -> bool:
    """True if `root` holds — at any depth, including dict keys — a mutable
    object whose in-place mutation `_TrackedList`/`_TrackedDict` cannot
    observe: anything besides JSON-safe primitives and list/dict/tuple/
    frozenset nesting of them. `type` objects are exempt — content only
    ever reads their class-level schema, never live instance state (see
    `_build_structure` in instruction.py).

    Iterative (explicit stack, not recursion) so deeply nested-but-safe
    input cannot raise `RecursionError`. Fails safe — returns True without
    raising — for a self-referential (cyclic) container or once traversal
    exceeds a bounded depth, since neither can be proven safe to cache.
    """
    stack: list[tuple[Any, int]] = [(root, 0)]
    on_path: set[int] = set()

    while stack:
        value, depth = stack.pop()

        if isinstance(value, _ExitFrame):
            on_path.discard(value.obj_id)
            continue

        if isinstance(value, _JSON_SAFE_LEAF_TYPES) or isinstance(value, type):
            continue

        if isinstance(value, (list, tuple, frozenset, dict)):
            if depth > _UNTRACKED_MUTABLE_MAX_DEPTH:
                return True

            obj_id = id(value)
            if obj_id in on_path:
                return True  # cyclic reference: cannot prove safe

            on_path.add(obj_id)
            stack.append((_ExitFrame(obj_id), depth))
            if isinstance(value, dict):
                for key, item in value.items():
                    stack.append((key, depth + 1))
                    stack.append((item, depth + 1))
            else:
                for item in value:
                    stack.append((item, depth + 1))
            continue

        return True

    return False


def _content_has_untracked_mutable(content: Any) -> bool:
    """True if any render-input field on `content` carries a value the
    revision tracker cannot observe in-place mutation of — the cache must
    not trust its revision counter and should re-render on every call."""
    allowed = getattr(content, "allowed", None)
    if not callable(allowed):
        return False
    return any(_has_untracked_mutable(getattr(content, name, None)) for name in allowed())


def _copy_rendered(rendered: Any) -> Any:
    """Keep cached structured content isolated from provider-side mutation."""
    if isinstance(rendered, list | dict):
        return deepcopy(rendered)
    return rendered


RoledMessage = Message


# File: lionagi/protocols/messages/message.py
