# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any, Generic, Literal, TypeAlias, TypeVar
from uuid import UUID, uuid4

import orjson
from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from lionagi._class_registry import get_class
from lionagi.ln._json_dump import get_orjson_default, json_dumps
from lionagi.ln._utils import now_utc
from lionagi.ln.types import not_sentinel
from lionagi.utils import import_module, to_dict

from .._concepts import Collective, Observable, Ordering

__all__ = (
    "Element",
    "validate_order",
)


class Element(BaseModel, Observable):
    """Pydantic base with UUID id, creation timestamp, and metadata dict."""

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
        if "lion_class" in val and val["lion_class"] != cls.class_name(full=True):
            raise ValueError("Metadata class mismatch.")
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
            return orjson.loads(self.to_json(decode=False, **kw))
        if mode == "db":
            dict_ = orjson.loads(self.to_json(decode=False, **kw))
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
                try:
                    # get_class resolves both fully-qualified names and legacy
                    # short names (data persisted before full-name adoption).
                    subcls_type: type[Element] = get_class(subcls)
                    # Delegate to a custom from_dict, or when the concrete
                    # type differs so model_validate uses the right schema.
                    if hasattr(subcls_type, "from_dict") and (
                        subcls_type.from_dict.__func__ != cls.from_dict.__func__
                        or subcls_type is not cls
                    ):
                        data["metadata"] = metadata
                        return subcls_type.from_dict(data)

                except (KeyError, ValueError, ImportError, AttributeError, TypeError):
                    mod, imp = subcls.rsplit(".", 1)
                    subcls_type = import_module(mod, import_name=imp)
                    data["metadata"] = metadata
                    if hasattr(subcls_type, "from_dict") and (subcls_type is not cls):
                        return subcls_type.from_dict(data)
        data["metadata"] = metadata
        return cls.model_validate(data)

    def to_json(self, decode: bool = True, **kw) -> str:
        """Serialize to JSON string."""
        kw.pop("mode", None)
        dict_ = self._to_dict(**kw)
        return json_dumps(dict_, default=DEFAULT_ELEMENT_SERIALIZER, decode=decode)

    @classmethod
    def from_json(cls, json_str: str) -> Element:
        """Deserialize JSON string into Element or subclass."""
        return cls.from_dict(orjson.loads(json_str))


DEFAULT_ELEMENT_SERIALIZER = get_orjson_default(
    order=[Element, BaseModel],
    additional={
        Element: lambda o: o.to_dict(),
        BaseModel: lambda o: o.model_dump(mode="json"),
    },
)


def validate_order(order: Any) -> list[UUID]:
    """Flatten an ordering (Element, UUID, str, nested list, or dict) into a list of UUIDs."""
    if isinstance(order, Element):
        return [order.id]
    if isinstance(order, Mapping):
        order = list(order.keys())

    stack = [order]
    out: list[UUID] = []
    while stack:
        cur = stack.pop()
        if cur is None:
            continue
        if isinstance(cur, Element):
            out.append(cur.id)
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
        """Return UUID from an Element, UUID, or str; raises ValueError otherwise."""
        if isinstance(item, UUID):
            return item
        if isinstance(item, Element):
            return item.id
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
