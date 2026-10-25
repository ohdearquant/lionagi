# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any, Generic, Literal, TypeAlias, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from lionagi._class_registry import _cut, _quoted, get_class
from lionagi._moved import resolve as _resolve_moved
from lionagi.ln._json_dump import get_orjson_default, json_dumps
from lionagi.ln._json_load import json_loads
from lionagi.ln._utils import now_utc
from lionagi.ln.types import not_sentinel
from lionagi.utils import to_dict

from .._concepts import Collective, Observable, Ordering

__all__ = (
    "Element",
    "validate_order",
)


class Element(BaseModel):
    """Pydantic base with UUID id, creation timestamp, and metadata dict.

    Structurally satisfies the ``Observable`` protocol through its ``id`` field;
    it does not inherit ``Observable`` (a runtime-checkable Protocol cannot be a
    pydantic base), and does not need to -- Pile admission is by capability.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        use_enum_values=True,
        populate_by_name=True,
        extra="forbid",
    )

    id: UUID = Field(
        default_factory=uuid4,
        title="ID",
        description="Unique identifier for this element.",
        frozen=True,
    )
    created_at: float = Field(
        default_factory=lambda: now_utc().timestamp(),
        title="Creation Timestamp",
        description="Timestamp of element creation.",
        frozen=True,
    )
    metadata: dict = Field(
        default_factory=dict,
        title="Metadata",
        description="Additional data for this element.",
    )

    @field_validator("metadata", mode="before")
    def _validate_meta_integrity(cls, val: dict) -> dict:
        """Coerce to dict; reject lion_class mismatch."""
        if not val:
            return {}
        if not isinstance(val, dict):
            val = to_dict(val, recursive=True, suppress=True)
        if "lion_class" in val:
            current = _current_path(val["lion_class"])
            if current != cls.class_name(full=True):
                raise ValueError("Metadata class mismatch.")
            if current != val["lion_class"]:
                # Metadata written before a move names the old path. An element nested in
                # another is saved with this metadata, so it is saved under the current path.
                val = {**val, "lion_class": current}
        if not isinstance(val, dict):
            raise ValueError("Invalid metadata.")
        return val

    @field_validator("created_at", mode="before")
    def _coerce_created_at(cls, val: float | dt.datetime | str | None) -> float:
        """Coerce created_at to a UTC float timestamp."""
        if val is None:
            return now_utc().timestamp()
        if isinstance(val, float):
            return val
        if isinstance(val, dt.datetime):
            return val.timestamp()
        if isinstance(val, str):
            try:
                # e.g. "2025-08-30 10:54:59.310329" -> ISO by swapping space for T
                iso_string = val.replace(" ", "T")
                parsed_dt = dt.datetime.fromisoformat(iso_string)

                # Naive datetime (no timezone) is treated as UTC.
                if parsed_dt.tzinfo is None:
                    parsed_dt = parsed_dt.replace(tzinfo=dt.timezone.utc)

                return parsed_dt.timestamp()
            except ValueError:
                try:
                    return float(val)
                except ValueError:
                    raise ValueError(f"Invalid datetime string: {val}") from None
        try:
            return float(val)  # type: ignore
        except Exception:
            raise ValueError(f"Invalid created_at: {val}") from None

    @field_validator("id", mode="before")
    def _ensure_uuid(cls, val: UUID | str) -> UUID:
        """Coerce id to UUID."""
        if isinstance(val, UUID):
            return val
        return UUID(str(val))

    @field_serializer("id")
    def _serialize_id_type(self, val: UUID) -> str:
        """Serialize id to string."""
        return str(val)

    @property
    def created_datetime(self) -> dt.datetime:
        """Creation time as a UTC-aware datetime."""
        return dt.datetime.fromtimestamp(self.created_at, tz=dt.timezone.utc)

    def __eq__(self, other: Any) -> bool:
        """Compare by id."""
        if not isinstance(other, Element):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        """Hash by id."""
        return hash(self.id)

    def __bool__(self) -> bool:
        """Always True."""
        return True

    @classmethod
    def class_name(cls, full: bool = False) -> str:
        """Return class name; full=True returns fully qualified name."""
        if full:
            return f"{cls.__module__}.{cls.__qualname__}"
        return cls.__name__

    def _to_dict(self, **kw) -> dict:
        """kw for model_dump."""
        dict_ = self.model_dump(**kw)
        dict_["metadata"].update({"lion_class": self.class_name(full=True)})
        return {k: v for k, v in dict_.items() if not_sentinel(v)}

    def to_dict(
        self,
        mode: Literal["python", "json", "db"] = "python",
        db_meta_key: str | None = None,
        **kw,
    ) -> dict:
        """Serialize to dict; mode='db' renames metadata to db_meta_key."""
        if mode == "python":
            return self._to_dict(**kw)
        if mode == "json":
            return json_loads(self.to_json(decode=False, **kw))
        if mode == "db":
            dict_ = json_loads(self.to_json(decode=False, **kw))
            dict_[db_meta_key or "node_metadata"] = dict_.pop("metadata", {})
            return dict_
        raise ValueError(f"Unsupported mode: {mode}")

    @classmethod
    def from_dict(cls, data: dict) -> Element:
        """Deserialize dict into Element or the subclass named by lion_class in metadata."""
        # Shallow copy so we don't mutate the caller's dict; metadata is popped from below.
        data = dict(data)

        metadata = {}

        if "node_metadata" in data:
            metadata = dict(data.pop("node_metadata") or {})
        elif "metadata" in data:
            metadata = dict(data.pop("metadata") or {})
        if "lion_class" in metadata:
            subcls: str = metadata.pop("lion_class")
            if subcls != Element.class_name(full=True):
                # get_class resolves both fully-qualified names and legacy
                # short names (data persisted before full-name adoption), and
                # imports a module only under an allowed prefix, with its parents.
                subcls_type = _lion_class_type(subcls)
                # Another class restores through its own from_dict, a staticmethod included, or
                # through this one again with itself as cls, so that model_validate applies its
                # schema. The class already restoring stops here: a from_dict of its own that calls
                # super() into this one would otherwise run a second time for the same data.
                if subcls_type is not cls:
                    data["metadata"] = metadata
                    return subcls_type.from_dict(data)
        data["metadata"] = metadata
        return cls.model_validate(data)

    def to_json(self, decode: bool = True, **kw) -> str:
        """Serialize to JSON string.

        Raises ValueError if the element holds inf, -inf or nan: JSON cannot
        represent them and they would be written as `null`, which no reader can
        tell apart from a genuine null once the element has been stored.
        """
        kw.pop("mode", None)
        dict_ = self._to_dict(**kw)
        return json_dumps(
            dict_,
            default=DEFAULT_ELEMENT_SERIALIZER,
            decode=decode,
            check_non_finite=True,
        )

    @classmethod
    def from_json(cls, json_str: str) -> Element:
        """Deserialize JSON string into Element or subclass."""
        return cls.from_dict(json_loads(json_str))


def _current_path(lion_class: Any) -> Any:
    """The dotted path a ``lion_class`` names today: one written under a module that has
    since moved (``lionagi._moved.MOVED``) reads as the class's path at its new module."""
    if isinstance(lion_class, str):
        return _resolve_moved(lion_class) or lion_class
    return lion_class


def _lion_class_type(lion_class: Any) -> type[Element]:
    """Resolve a ``lion_class`` from serialized metadata to an Element subclass.

    Raises ValueError for a name that is not a string, that resolves to nothing
    lionagi may load, or that names a type outside the Element hierarchy.
    """
    if not isinstance(lion_class, str):
        shown = _cut(type(lion_class).__name__)
        raise ValueError(f"metadata lion_class must be a string, got {shown}")
    try:
        resolved = get_class(lion_class)
    except ValueError as exc:
        # get_class's message names the class, cut short, and carries the remedy
        # and, when loading failed, the reason; the name is not repeated here.
        raise ValueError(f"metadata lion_class cannot be loaded: {exc}") from exc
    if not (isinstance(resolved, type) and issubclass(resolved, Element)):
        raise ValueError(f"metadata lion_class {_quoted(lion_class)} is not an Element subclass")
    return resolved


DEFAULT_ELEMENT_SERIALIZER = get_orjson_default(
    order=[Element, BaseModel],
    additional={
        Element: lambda o: o.to_dict(),
        BaseModel: lambda o: o.model_dump(mode="json"),
    },
)


def _observable_uuid(obj: Observable) -> UUID:
    """Extract an Observable's ``id`` as a UUID (coercing a string id).

    Shared by ``validate_order`` and ``ID.get_id`` so every id-resolution path
    accepts the same structural contract Pile admits by -- any object exposing an
    ``id``, not only nominal Element subclasses.
    """
    oid = obj.id
    return oid if isinstance(oid, UUID) else UUID(str(oid))


def validate_order(order: Any) -> list[UUID]:
    """Flatten an ordering (Observable, UUID, str, nested list, or dict) into a list of UUIDs."""
    if isinstance(order, Observable):
        return [_observable_uuid(order)]
    if isinstance(order, Mapping):
        order = list(order.keys())

    stack = [order]
    out: list[UUID] = []
    while stack:
        cur = stack.pop()
        if cur is None:
            continue
        if isinstance(cur, Observable):
            out.append(_observable_uuid(cur))
        elif isinstance(cur, UUID):
            out.append(cur)
        elif isinstance(cur, str):
            out.append(UUID(cur))
        elif isinstance(cur, list | tuple | set):
            stack.extend(reversed(cur))
        else:
            raise ValueError("Invalid item in order.")

    return [] if not out else out


E = TypeVar("E", bound=Element)


class ID(Generic[E]):
    """Type aliases and helpers for extracting UUIDs from Elements, strings, or UUIDs."""

    ID: TypeAlias = UUID
    Item: TypeAlias = E | Element  # type: ignore
    Ref: TypeAlias = UUID | E | str  # type: ignore
    IDSeq: TypeAlias = Sequence[UUID] | Ordering[E]  # type: ignore
    ItemSeq: TypeAlias = Sequence[E] | Collective[E]  # type: ignore
    RefSeq: TypeAlias = ItemSeq | Sequence[Ref] | Ordering[E]  # type: ignore

    @staticmethod
    def get_id(item: E) -> UUID:
        """Return UUID from an Observable, UUID, or str; raises ValueError otherwise.

        Resolution is structural, matching Pile's admission contract: any object
        exposing an ``id`` (an Element or a duck-typed item) resolves, not just
        nominal Element subclasses -- otherwise an object Pile admits could not
        be looked up or removed by identity.
        """
        if isinstance(item, UUID):
            return item
        if isinstance(item, Observable):
            return _observable_uuid(item)
        if isinstance(item, str):
            return UUID(item)
        raise ValueError("Cannot get ID from item.")

    @staticmethod
    def is_id(item: Any) -> bool:
        """Return True if item can be converted to a UUID."""
        try:
            ID.get_id(item)  # type: ignore
            return True
        except ValueError:
            return False
