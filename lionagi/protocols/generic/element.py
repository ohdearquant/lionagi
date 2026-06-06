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
    """Basic identifiable, timestamped element.

    This Pydantic model provides a unique identifier (`id`), an automatically
    generated creation timestamp (`created_at`), and an optional metadata
    dictionary.

    Attributes:
        id (UUID):
            A unique ID based on UUIDv4 (defaults to a newly generated one).
        created_at (float):
            The creation timestamp as a float (Unix epoch). Defaults to
            the current time.
        metadata (dict):
            A dictionary for storing additional information about this Element.
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
        """Validates that `metadata` is a dictionary and checks class naming.

        If a `lion_class` field is present in `metadata`, it must match the
        fully qualified name of this class. Converts `metadata` to a dict
        if needed.
        """
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
        """Coerces `created_at` to a float-based timestamp."""
        if val is None:
            return now_utc().timestamp()
        if isinstance(val, float):
            return val
        if isinstance(val, dt.datetime):
            return val.timestamp()
        if isinstance(val, str):
            # Parse datetime string from database
            try:
                # Handle datetime strings like "2025-08-30 10:54:59.310329"
                # Convert space to T for ISO format, but handle timezone properly
                iso_string = val.replace(" ", "T")
                parsed_dt = dt.datetime.fromisoformat(iso_string)

                # If parsed as naive datetime (no timezone), treat as UTC to avoid local timezone issues
                if parsed_dt.tzinfo is None:
                    parsed_dt = parsed_dt.replace(tzinfo=dt.timezone.utc)

                return parsed_dt.timestamp()
            except ValueError:
                # Try parsing as float string as fallback
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
        """Ensures `id` is validated as an UUID."""
        if isinstance(val, UUID):
            return val
        return UUID(str(val))

    @field_serializer("id")
    def _serialize_id_type(self, val: UUID) -> str:
        """Serializes the `id` field to a string."""
        return str(val)

    @property
    def created_datetime(self) -> dt.datetime:
        """Returns the creation time as a datetime object."""
        return dt.datetime.fromtimestamp(self.created_at, tz=dt.timezone.utc)

    def __eq__(self, other: Any) -> bool:
        """Compares two Element instances by their ID."""
        if not isinstance(other, Element):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        """Returns a hash of this element's ID."""
        return hash(self.id)

    def __bool__(self) -> bool:
        """Elements are always considered truthy."""
        return True

    @classmethod
    def class_name(cls, full: bool = False) -> str:
        """Returns this class's name.

        full (bool): If True, returns the fully qualified class name; otherwise,
            returns only the class name.
        """
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
        """Converts this Element to a dictionary."""
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
        """Deserializes a dictionary into an Element or subclass of Element.

        If `lion_class` in `metadata` refers to a subclass, this method
        is polymorphic, it will attempt to create an instance of that subclass.
        """
        # Shallow copy to avoid mutating the caller's dict. The nested metadata
        # dict is also copied because we pop from it (lion_class extraction).
        data = dict(data)

        # Preprocess database format if needed
        metadata = {}

        if "node_metadata" in data:
            metadata = dict(data.pop("node_metadata") or {})
        elif "metadata" in data:
            metadata = dict(data.pop("metadata") or {})
        if "lion_class" in metadata:
            subcls: str = metadata.pop("lion_class")
            if subcls != Element.class_name(full=True):
                try:
                    # Try full qualified name first (LION_CLASS_REGISTRY is
                    # populated with full-qualified names at import time).
                    # Fall back to short name for classes registered before the
                    # full-name convention was adopted.
                    try:
                        subcls_type: type[Element] = get_class(subcls)
                    except (KeyError, ValueError):
                        subcls_type = get_class(subcls.split(".")[-1])
                    # Delegate when there is a custom from_dict OR when the
                    # concrete type differs from cls (so model_validate uses the
                    # right schema). Restore metadata before the recursive call
                    # so the delegate sees a self-consistent dict.
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
        """Converts this Element to a JSON string."""
        kw.pop("mode", None)
        dict_ = self._to_dict(**kw)
        return json_dumps(dict_, default=DEFAULT_ELEMENT_SERIALIZER, decode=decode)

    @classmethod
    def from_json(cls, json_str: str) -> Element:
        """Deserializes a JSON string into an Element or subclass of Element."""
        return cls.from_dict(orjson.loads(json_str))


DEFAULT_ELEMENT_SERIALIZER = get_orjson_default(
    order=[Element, BaseModel],
    additional={
        Element: lambda o: o.to_dict(),
        BaseModel: lambda o: o.model_dump(mode="json"),
    },
)


def validate_order(order: Any) -> list[UUID]:
    """Validates and flattens an ordering into a list of UUID objects.

    This function accepts a variety of possible representations for ordering
    (e.g., a single Element, a list of Elements, a dictionary with ID keys,
    or a nested structure) and returns a flat list of UUID objects.

    Returns:
        list[UUID]: A flat list of validated UUID objects.

    Raises:
        ValueError: If an invalid item is encountered or if there's a mixture
            of types not all convertible to UUID.
    """
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
        elif isinstance(cur, (list, tuple, set)):
            stack.extend(reversed(cur))
        else:
            raise ValueError("Invalid item in order.")

    return [] if not out else out


E = TypeVar("E", bound=Element)


class ID(Generic[E]):
    """Utility class for working with UUID objects and Elements.

    This class provides helper methods to extract IDs from Elements, strings,
    or UUIDs, and to test whether a given object can be interpreted as
    an ID.
    """

    ID: TypeAlias = UUID
    Item: TypeAlias = E | Element  # type: ignore
    Ref: TypeAlias = UUID | E | str  # type: ignore
    IDSeq: TypeAlias = Sequence[UUID] | Ordering[E]  # type: ignore
    ItemSeq: TypeAlias = Sequence[E] | Collective[E]  # type: ignore
    RefSeq: TypeAlias = ItemSeq | Sequence[Ref] | Ordering[E]  # type: ignore

    @staticmethod
    def get_id(item: E) -> UUID:
        """Retrieves an UUID from multiple possible item forms.

        Acceptable item types include:
        - Element: Uses its `id` attribute.
        - UUID: Returns it directly.
        - UUID: Validates and wraps it.
        - str: Interpreted as a UUID if possible.

        Returns:
            UUID: The validated ID.

        Raises:
            ValueError: If the item cannot be converted to an UUID.
        """
        if isinstance(item, UUID):
            return item
        if isinstance(item, Element):
            return item.id
        if isinstance(item, str):
            return UUID(item)
        raise ValueError("Cannot get ID from item.")

    @staticmethod
    def is_id(item: Any) -> bool:
        """Checks if an item can be validated as an UUID.

        Returns:
            bool: True if `item` is or can be validated as an UUID;
                otherwise, False.
        """
        try:
            ID.get_id(item)  # type: ignore
            return True
        except ValueError:
            return False


# File: lionagi/protocols/generic/element.py
