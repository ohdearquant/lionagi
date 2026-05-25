# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import threading
from collections import deque
from collections.abc import AsyncIterator, Callable, Generator, Iterator, Sequence
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import Field, field_serializer
from typing_extensions import Self, override

from lionagi._errors import ItemExistsError, ItemNotFoundError, ValidationError
from lionagi.adapters._base import Adaptable, AsyncAdaptable
from lionagi.ln.concurrency import Lock as ConcurrencyLock
from lionagi.ln.concurrency import sleep as _concurrency_sleep
from lionagi.utils import (
    UNDEFINED,
    is_same_dtype,
    is_union_type,
    to_list,
    union_members,
)

from .._concepts import Observable
from .element import ID, Collective, E, Element, validate_order
from .progression import Progression

if TYPE_CHECKING:
    from pydantic.fields import FieldInfo


D = TypeVar("D")
T = TypeVar("T", bound=E)

_ADAPTER_REGISTERED = False


def synchronized(func: Callable):
    @wraps(func)
    def wrapper(self: Pile, *args, **kwargs):
        with self.lock:
            return func(self, *args, **kwargs)

    return wrapper


def async_synchronized(func: Callable):
    @wraps(func)
    async def wrapper(self: Pile, *args, **kwargs):
        async with self.async_lock:
            return await func(self, *args, **kwargs)

    return wrapper


def _validate_item_type(value, /) -> set[type[T]] | None:
    if value is None:
        return None

    value = to_list_type(value)
    out = set()

    from lionagi.utils import import_module

    for i in value:
        subcls = i
        if isinstance(i, str):
            try:
                mod, imp = i.rsplit(".", 1)
                subcls = import_module(mod, import_name=imp)
            except Exception as e:
                raise ValidationError.from_value(
                    i,
                    expected="A subclass of Observable.",
                    cause=e,
                ) from e
        if isinstance(subcls, type):
            if is_union_type(subcls):
                members = union_members(subcls)
                for m in members:
                    if not issubclass(m, Observable):
                        raise ValidationError.from_value(m, expected="A subclass of Observable.")
                    out.add(m)
            elif not issubclass(subcls, Observable):
                raise ValidationError.from_value(subcls, expected="A subclass of Observable.")
            else:
                out.add(subcls)
        else:
            raise ValidationError.from_value(i, expected="A subclass of Observable.")

    if len(value) != len(set(value)):
        raise ValidationError("Detected duplicated item types in item_type.")

    if len(value) > 0:
        return out


def _validate_progression(value: Any, collections: dict[UUID, T], /) -> Progression:
    if not value:
        return Progression(order=list(collections.keys()))

    prog = None
    if isinstance(value, dict):
        try:
            prog = Progression.from_dict(value)
            value = list(prog)
        except Exception:
            # If we can't create Progression from dict, try to extract order field
            value = to_list_type(value.get("order", []))
    elif isinstance(value, Progression):
        prog = value
        value = list(prog)
    else:
        value = to_list_type(value)

    value_set = set(value)
    if len(value_set) != len(value):
        raise ValueError("There are duplicate elements in the order")
    if len(value_set) != len(collections.keys()):
        raise ValueError("The length of the order does not match the length of the pile")

    for i in value_set:
        if ID.get_id(i) not in collections.keys():
            raise ValueError(f"The order does not match the pile. {i} not found")
    return prog or Progression(order=value)


def _validate_collections(value: Any, item_type: set | None, strict_type: bool, /) -> dict[str, T]:
    if not value:
        return {}

    value = to_list_type(value)

    result = {}
    for i in value:
        if isinstance(i, dict):
            i = Element.from_dict(i)

        if item_type:
            if strict_type:
                if type(i) not in item_type:
                    raise ValidationError.from_value(
                        i,
                        expected=f"One of {item_type}, no subclasses allowed.",
                    )
            else:
                if not any(issubclass(type(i), t) for t in item_type):
                    raise ValidationError.from_value(
                        i,
                        expected=f"One of {item_type} or the subclasses",
                    )
        else:
            if not isinstance(i, Observable):
                raise ValueError(f"Invalid pile item {i}")

        result[i.id] = i

    return result


class Pile(Element, Collective[T], Generic[T], Adaptable, AsyncAdaptable):
    """Thread-safe async-compatible, ordered collection of elements.

    The Pile class provides a thread-safe, async-compatible collection with:
    - Type validation and enforcement
    - Order preservation
    - Format adapters (JSON, CSV, Excel)
    - Memory efficient storage

    Thread Safety:
        Mutation methods decorated with ``@synchronized`` /
        ``@async_synchronized`` acquire ``_lock`` (a ``threading.RLock``).
        The RLock is reentrant so that :class:`Flow` can hold its own lock
        and then call Pile methods without deadlocking.

        ``include()``, ``exclude()``, and ``update()`` are also decorated
        with ``@synchronized`` as of v0.20.2.

    Attributes:
        pile_ (dict[str, T]): Internal storage mapping IDs to elements
        item_type (set[type[T]] | None): Allowed element types
        progress (Progression): Order tracking
        strict_type (bool): Whether to enforce strict type checking
    """

    collections: dict[UUID, T] = Field(default_factory=dict)
    item_type: set | None = Field(
        default=None,
        description="Set of allowed types for items in the pile.",
        exclude=True,
    )
    progression: Progression = Field(
        default_factory=Progression,
        description="Progression specifying the order of items in the pile.",
    )
    strict_type: bool = Field(
        default=False,
        description="Specify if enforce a strict type check",
        frozen=True,
    )

    _EXTRA_FIELDS: ClassVar[set[str]] = {
        "collections",
        "item_type",
        "progression",
        "strict_type",
    }

    def __pydantic_extra__(self) -> dict[str, FieldInfo]:
        return {
            "_lock": Field(default_factory=threading.RLock),
            "_async": Field(default_factory=ConcurrencyLock),
        }

    def __pydantic_private__(self) -> dict[str, FieldInfo]:
        return self.__pydantic_extra__()

    @classmethod
    def _validate_before(cls, data: dict[str, Any]) -> dict[str, Any]:
        item_type = _validate_item_type(data.get("item_type"))
        strict_type = data.get("strict_type", False)
        collections = _validate_collections(data.get("collections"), item_type, strict_type)
        progression = None
        if "order" in data:
            progression = _validate_progression(data["order"], collections)
        else:
            progression = _validate_progression(data.get("progression"), collections)

        return {
            "collections": collections,
            "item_type": item_type,
            "progression": progression,
            "strict_type": strict_type,
            **{k: v for k, v in data.items() if k not in cls._EXTRA_FIELDS},
        }

    @override
    def __init__(
        self,
        collections: ID.ItemSeq = None,
        item_type: set[type[T]] = None,
        order: ID.RefSeq = None,
        strict_type: bool = False,
        **kwargs,
    ) -> None:
        """Initialize a Pile instance.

        Args:
            items: Initial items for the pile.
            item_type: Allowed types for items in the pile.
            order: Initial order of items (as Progression).
            strict_type: If True, enforce strict type checking.
        """
        data = Pile._validate_before(
            {
                "collections": collections,
                "item_type": item_type,
                "progression": order,
                "strict_type": strict_type,
                **kwargs,
            }
        )
        super().__init__(**data)

    @field_serializer("collections")
    def _serialize_collections(self, v: dict[UUID, T]) -> list[dict[str, Any]]:
        return [i.to_dict() for i in v.values()]

    @field_serializer("progression")
    def _serialize_progression(self, v: Progression) -> dict[str, Any]:
        return v.to_dict()

    @field_serializer("item_type")
    def _serialize_item_type(self, v: set[type[T]] | None) -> list[str] | None:
        """Serialize item_type to a list of class names."""
        if v is None:
            return None
        return [c.class_name(full=True) for c in v]

    # Sync Interface methods
    @override
    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        /,
    ) -> Pile:
        """Create a Pile instance from a dictionary.

        Args:
            data: A dictionary containing Pile data.

        Returns:
            A new Pile instance created from the provided data.

        Raises:
            ValidationError: If the dictionary format is invalid.
        """
        return cls(**data)

    def __setitem__(
        self,
        key: ID.Ref | ID.RefSeq | int | slice,
        item: ID.ItemSeq | ID.Item,
    ) -> None:
        """Set an item or items in the Pile.

        Args:
            key: The key to set (index, ID, or slice).
            item: The item(s) to set.

        Raises:
            TypeError: If item type not allowed.
            KeyError: If key invalid.
        """
        self._setitem(key, item)

    @synchronized
    def pop(
        self,
        key: ID.Ref | ID.RefSeq | int | slice,
        default: D = UNDEFINED,
        /,
    ) -> T | Pile | D:
        """Remove and return item(s) from the Pile.

        Args:
            key: Key of item(s) to remove.
            default: Value if key not found.

        Returns:
            Removed item(s) or default.

        Raises:
            KeyError: If key not found and no default.
        """
        return self._pop(key, default)

    def remove(
        self,
        item: T,
        /,
    ) -> None:
        """Remove a specific item from the Pile.

        Args:
            item: Item to remove.

        Raises:
            ValueError: If item not found.
        """
        if isinstance(item, int | slice):
            raise TypeError("Invalid item type for remove, should be ID or Item(s)")
        if item in self:
            self.pop(item)
            return
        raise ItemNotFoundError(f"{item}")

    @synchronized
    def include(self, item: ID.ItemSeq | ID.Item, /) -> None:
        """Include item(s) if not present.

        Args:
            item: Item(s) to include.
        """
        item_dict = _validate_collections(item, self.item_type, self.strict_type)

        item_order = []
        for i in item_dict.keys():
            if i not in self.progression:
                item_order.append(i)

        self.progression.append(item_order)
        self.collections.update(item_dict)

    @synchronized
    def exclude(
        self,
        item: ID.ItemSeq | ID.Item,
        /,
    ) -> None:
        """Exclude item(s) if present.

        Args:
            item: Item(s) to exclude.
        """
        item = to_list_type(item)
        exclude_list = []
        for i in item:
            if i in self:
                exclude_list.append(i)
        if exclude_list:
            self.pop(exclude_list)

    @synchronized
    def clear(self) -> None:
        """Remove all items."""
        self._clear()

    @synchronized
    def update(
        self,
        other: ID.Item | ID.ItemSeq,
        /,
    ) -> None:
        """Update with items from another source.

        Args:
            other: Items to update from.

        Raises:
            TypeError: If item types not allowed.
        """
        others = _validate_collections(other, self.item_type, self.strict_type)
        for i in others.keys():
            if i in self.collections:
                self.collections[i] = others[i]
            else:
                self.include(others[i])

    @synchronized
    def insert(self, index: int, item: T, /) -> None:
        """Insert item at position.

        Args:
            index: Position to insert at.
            item: Item to insert.

        Raises:
            IndexError: If index out of range.
            TypeError: If item type not allowed.
        """
        self._insert(index, item)

    @synchronized
    def append(self, item: T, /) -> None:
        """Append item to end (alias for include).

        Args:
            item: Item to append.

        Raises:
            TypeError: If item type not allowed.
        """
        self.update(item)

    @synchronized
    def get(
        self,
        key: ID.Ref | ID.RefSeq | int | slice,
        default: D = UNDEFINED,
        /,
    ) -> T | Pile | D:
        """Get item(s) by key with default.

        Args:
            key: Key to get items by.
            default: Value if not found.

        Returns:
            Item(s) or default.
        """
        return self._get(key, default)

    def keys(self) -> Sequence[str]:
        """Get all Lion IDs in order."""
        return list(self.progression)

    def values(self) -> Sequence[T]:
        """Get all items in order."""
        return [self.collections[key] for key in self.progression]

    def items(self) -> Sequence[tuple[UUID, T]]:
        """Get all (ID, item) pairs in order."""
        return [(key, self.collections[key]) for key in self.progression]

    def is_empty(self) -> bool:
        """Check if empty."""
        return len(self.progression) == 0

    def size(self) -> int:
        """Get number of items."""
        return len(self.progression)

    def __iter__(self) -> Iterator[T]:
        """Iterate over items safely."""
        current_order = list(self.progression)

        for key in current_order:
            yield self.collections[key]

    def __next__(self) -> T:
        """Get next item."""
        try:
            return next(iter(self))
        except StopIteration:
            raise StopIteration("End of pile") from None

    def __getitem__(self, key: ID.Ref | ID.RefSeq | int | slice) -> Any | list | T:
        """Get item(s) by key.

        Args:
            key: Key to get items by.

        Returns:
            Item(s) or sliced Pile.

        Raises:
            KeyError: If key not found.
        """
        return self._getitem(key)

    def __contains__(self, item: ID.RefSeq | ID.Ref) -> bool:
        """Check if item exists."""
        return item in self.progression

    def __len__(self) -> int:
        """Get number of items."""
        return len(self.collections)

    @override
    def __bool__(self) -> bool:
        """Check if not empty."""
        return not self.is_empty()

    def __list__(self) -> list[T]:
        """Convert to list."""
        return self.values()

    def __ior__(self, other: Pile) -> Self:
        """In-place union."""
        if not isinstance(other, Pile):
            raise TypeError(f"Invalid type for Pile operation. expected <Pile>, got {type(other)}")
        other = _validate_collections(list(other), self.item_type, self.strict_type)
        self.include(other)
        return self

    def __or__(self, other: Pile) -> Pile:
        """Union."""
        if not isinstance(other, Pile):
            raise TypeError(f"Invalid type for Pile operation. expected <Pile>, got {type(other)}")

        result = self.__class__(
            items=self.values(),
            item_type=self.item_type,
            order=self.progression,
        )
        result.include(list(other))
        return result

    def __ixor__(self, other: Pile) -> Self:
        """In-place symmetric difference."""
        if not isinstance(other, Pile):
            raise TypeError(f"Invalid type for Pile operation. expected <Pile>, got {type(other)}")

        to_exclude = []
        for i in other:
            if i in self:
                to_exclude.append(i)

        other = [i for i in other if i not in to_exclude]
        self.exclude(to_exclude)
        self.include(other)
        return self

    def __xor__(self, other: Pile) -> Pile:
        """Symmetric difference."""
        if not isinstance(other, Pile):
            raise TypeError(f"Invalid type for Pile operation. expected <Pile>, got {type(other)}")

        to_exclude = []
        for i in other:
            if i in self:
                to_exclude.append(i)

        values = [i for i in self if i not in to_exclude] + [
            i for i in other if i not in to_exclude
        ]

        result = self.__class__(
            items=values,
            item_type=self.item_type,
        )
        return result

    def __iand__(self, other: Pile) -> Self:
        """In-place intersection."""
        if not isinstance(other, Pile):
            raise TypeError(f"Invalid type for Pile operation. expected <Pile>, got {type(other)}")

        to_exclude = []
        for i in self.values():
            if i not in other:
                to_exclude.append(i)
        self.exclude(to_exclude)
        return self

    def __and__(self, other: Pile) -> Pile:
        """Intersection."""
        if not isinstance(other, Pile):
            raise TypeError(f"Invalid type for Pile operation. expected <Pile>, got {type(other)}")

        values = [i for i in self if i in other]
        return self.__class__(
            items=values,
            item_type=self.item_type,
        )

    @override
    def __str__(self) -> str:
        """Simple string representation."""
        return f"Pile({len(self)})"

    @override
    def __repr__(self) -> str:
        """Detailed string representation."""
        length = len(self)
        if length == 0:
            return "Pile()"
        elif length == 1:
            return f"Pile({next(iter(self.collections.values())).__repr__()})"
        else:
            return f"Pile({length})"

    def __getstate__(self):
        """Prepare for pickling."""
        state = self.__dict__.copy()
        state["_lock"] = None
        state["_async_lock"] = None
        return state

    def __setstate__(self, state):
        """Restore after unpickling."""
        self.__dict__.update(state)
        self._lock = threading.RLock()
        self._async_lock = ConcurrencyLock()

    @property
    def lock(self):
        """Thread lock."""
        if not hasattr(self, "_lock") or self._lock is None:
            self._lock = threading.RLock()
        return self._lock

    @property
    def async_lock(self):
        """Async lock."""
        if not hasattr(self, "_async_lock") or self._async_lock is None:
            self._async_lock = ConcurrencyLock()
        return self._async_lock

    # Async Interface methods
    @async_synchronized
    async def asetitem(
        self,
        key: ID.Ref | ID.RefSeq | int | slice,
        item: ID.Item | ID.ItemSeq,
        /,
    ) -> None:
        """Async set item(s)."""
        self._setitem(key, item)

    @async_synchronized
    async def apop(
        self,
        key: ID.Ref | ID.RefSeq | int | slice,
        default: Any = UNDEFINED,
        /,
    ):
        """Async remove and return item(s)."""
        return self._pop(key, default)

    @async_synchronized
    async def aremove(
        self,
        item: ID.Ref | ID.RefSeq,
        /,
    ) -> None:
        """Async remove item."""
        self.remove(item)

    @async_synchronized
    async def ainclude(
        self,
        item: ID.ItemSeq | ID.Item,
        /,
    ) -> None:
        """Async include item(s)."""
        self.include(item)
        if item not in self:
            raise TypeError(f"Item {item} is not of allowed types")

    @async_synchronized
    async def aexclude(
        self,
        item: ID.Ref | ID.RefSeq,
        /,
    ) -> None:
        """Async exclude item(s)."""
        self.exclude(item)

    @async_synchronized
    async def aclear(self) -> None:
        """Async clear all items."""
        self._clear()

    @async_synchronized
    async def aupdate(
        self,
        other: ID.ItemSeq | ID.Item,
        /,
    ) -> None:
        """Async update with items."""
        self.update(other)

    @async_synchronized
    async def aget(
        self,
        key: Any,
        default=UNDEFINED,
        /,
    ) -> list | Any | T:
        """Async get item(s)."""
        return self._get(key, default)

    async def __aiter__(self) -> AsyncIterator[T]:
        """Async iterate over items."""
        async with self.async_lock:
            current_order = list(self.progression)

        for key in current_order:
            yield self.collections[key]

    async def __anext__(self) -> T:
        """Async get next item."""
        try:
            return await anext(self.AsyncPileIterator(self))
        except StopAsyncIteration:
            raise StopAsyncIteration("End of pile") from None

    def filter(self, predicate: Callable[[T], bool]) -> Pile[T]:
        """Return a new Pile containing items matching the predicate.

        Args:
            predicate: A callable that takes an item and returns True/False.

        Returns:
            Pile[T]: A new Pile containing only matching items.
        """
        return self._filter_by_function(predicate)

    def _filter_by_function(self, func: Callable[[T], bool]) -> Pile[T]:
        """Internal filter implementation.

        Args:
            func: Callable predicate over items.

        Returns:
            Pile[T] with matching items in original order.
        """
        matched = []
        for key in list(self.progression):
            item = self.collections[key]
            if func(item):
                matched.append(item)
        return self.__class__(
            collections=matched,
            item_type=self.item_type,
            strict_type=self.strict_type,
        )

    # private methods
    def _getitem(self, key: Any) -> Any | list | T:
        if key is None:
            raise ValueError("getitem key not provided.")

        if callable(key) and not isinstance(key, (UUID, Element, type)):  # noqa: UP038
            return self._filter_by_function(key)

        if isinstance(key, int | slice):
            try:
                result_ids = self.progression[key]
                if isinstance(result_ids, Progression):
                    result_ids = list(result_ids)
                elif not isinstance(result_ids, list):
                    result_ids = [result_ids]
                result = []
                for i in result_ids:
                    result.append(self.collections[i])
                return result[0] if len(result) == 1 else result
            except (IndexError, KeyError, ItemNotFoundError) as e:
                raise ItemNotFoundError(f"index {key}. Error: {e}") from e

        elif isinstance(key, UUID):
            try:
                return self.collections[key]
            except KeyError as e:
                raise ItemNotFoundError(f"key {key}. Error: {e}") from e

        else:
            key = to_list_type(key)
            result = []
            try:
                for k in key:
                    result_id = ID.get_id(k)
                    result.append(self.collections[result_id])

                if len(result) == 0:
                    raise ItemNotFoundError(f"key {key} item not found")
                if len(result) == 1:
                    return result[0]
                return result
            except (KeyError, ValueError, ItemNotFoundError) as e:
                raise ItemNotFoundError(f"Key {key}. Error:{e}") from e

    def _setitem(
        self,
        key: ID.Ref | ID.RefSeq | int | slice,
        item: ID.Item | ID.ItemSeq,
    ) -> None:
        item_dict = _validate_collections(item, self.item_type, self.strict_type)

        item_order = []
        for i in item_dict.keys():
            if i in self.progression:
                raise ItemExistsError(f"item {i} already exists in the pile")
            item_order.append(i)
        if isinstance(key, int | slice):
            try:
                delete_order = (
                    list(self.progression[key])
                    if isinstance(self.progression[key], Progression)
                    else [self.progression[key]]
                )
                self.progression[key] = item_order
                for i in to_list(delete_order, flatten=True):
                    self.collections.pop(i)
                self.collections.update(item_dict)
            except Exception as e:
                raise ValueError(f"Failed to set pile. Error: {e}") from e
        else:
            key = to_list_type(key)
            if isinstance(key[0], list):
                key = to_list(key, flatten=True, dropna=True)
            if len(key) != len(item_order):
                raise KeyError(
                    f"Invalid key {key}. Key and item does not match.",
                )
            for k in key:
                id_ = ID.get_id(k)
                if id_ not in item_order:
                    raise KeyError(
                        f"Invalid key {id_}. Key and item does not match.",
                    )
            self.progression += key
            self.collections.update(item_dict)

    def _get(self, key: Any, default: D = UNDEFINED) -> T | Pile | D:
        if isinstance(key, int | slice):
            try:
                return self[key]
            except Exception as e:
                if default is UNDEFINED:
                    raise ItemNotFoundError(f"Item not found. Error: {e}") from e
                return default
        else:
            check = None
            if isinstance(key, list):
                check = True
                for i in key:
                    if type(i) is not int:
                        check = False
                        break
            try:
                if not check:
                    key = validate_order(key)
                result = []
                for k in key:
                    result.append(self[k])
                if len(result) == 0:
                    raise ItemNotFoundError(f"key {key} item not found")
                if len(result) == 1:
                    return result[0]
                return result

            except Exception as e:
                if default is UNDEFINED:
                    raise ItemNotFoundError(f"Item not found. Error: {e}") from e
                return default

    def _pop(
        self,
        key: ID.Ref | ID.RefSeq | int | slice,
        default: D = UNDEFINED,
    ) -> T | Pile | D:
        if isinstance(key, int | slice):
            try:
                pops = self.progression[key]
                pops = [pops] if isinstance(pops, UUID) else pops
                self.progression.exclude(pops)
                result = [self.collections.pop(i) for i in pops]
                result = (
                    self.__class__(items=result, item_type=self.item_type)
                    if len(result) > 1
                    else result[0]
                )
                return result
            except Exception as e:
                if default is UNDEFINED:
                    raise ItemNotFoundError(f"Item not found. Error: {e}") from e
                return default
        else:
            try:
                key = validate_order(key)
                self.progression.exclude(key)
                result = [self.collections.pop(k) for k in key]
                if len(result) == 0:
                    raise ItemNotFoundError(f"key {key} item not found")
                elif len(result) == 1:
                    return result[0]
                return result
            except Exception as e:
                if default is UNDEFINED:
                    raise ItemNotFoundError(f"Item not found. Error: {e}") from e
                return default

    def _clear(self) -> None:
        self.collections.clear()
        self.progression.clear()

    def _insert(self, index: int, item: ID.Item):
        item_dict = _validate_collections(item, self.item_type, self.strict_type)

        item_order = []
        for i in item_dict.keys():
            if i in self.progression:
                raise ItemExistsError(f"item {i} already exists in the pile")
            item_order.append(i)
        self.progression.insert(index, item_order)
        self.collections.update(item_dict)

    class AsyncPileIterator:
        def __init__(self, pile: Pile):
            self.pile = pile
            self.index = 0

        def __aiter__(self) -> AsyncIterator[T]:
            return self

        async def __anext__(self) -> T:
            if self.index >= len(self.pile):
                raise StopAsyncIteration
            item = self.pile[self.pile.progression[self.index]]
            self.index += 1
            await _concurrency_sleep(0)  # Yield control to the event loop
            return item

    async def __aenter__(self) -> Self:
        """Enter async context."""
        await self.async_lock.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Exit async context."""
        await self.async_lock.__aexit__(exc_type, exc_val, exc_tb)

    def is_homogenous(self) -> bool:
        """Check if all items are same type."""
        return len(self.collections) < 2 or all(is_same_dtype(self.collections.values()))

    @classmethod
    def list_adapters(cls) -> list[str]:
        syn_ = cls._adapter_registry._reg.keys()
        asy_ = cls._async_registry._reg.keys()
        return list(set(syn_) | set(asy_))

    def adapt_to(self, obj_key: str, many=False, **kw: Any) -> Any:
        """Adapt to another format.

        Args:
            obj_key: Key indicating the format (e.g., 'json', 'csv').
            many: If True, interpret to receive list of items in the collection.
            **kw: Additional keyword arguments for adaptation.

        Example:
            >>> str_ = pile.adapt_to('json')
            >>> df = pile.adapt_to('pd.DataFrame', many=True)
            >>> csv_str = pile.adapt_to('csv', many=True)

        Pile built-in with `json`, `csv`, `pd.DataFrame` adapters. You can add more
        from pydapter, such as `qdrant`, `neo4j`, `postgres`, etc.
        please visit https://khive-ai.github.io/pydapter/ for more details.
        """
        kw["adapt_meth"] = "to_dict"
        return super().adapt_to(obj_key=obj_key, many=many, **kw)

    @classmethod
    def adapt_from(cls, obj: Any, obj_key: str, many=False, **kw: Any):
        """Create from another format.

        Args:
            obj: Object to adapt from.
            obj_key: Key indicating the format (e.g., 'json', 'csv').
            many: If True, interpret to receive list of items in the collection.
            **kw: Additional keyword arguments for adaptation.

        Example:
            >>> pile = Pile.adapt_from(str_, 'json')
            >>> pile = Pile.adapt_from(df, 'pd.DataFrame', many=True)
        Pile built-in with `json`, `csv`, `pd.DataFrame` adapters. You can add more
        from pydapter, such as `qdrant`, `neo4j`, `postgres`, etc.
        please visit https://khive-ai.github.io/pydapter/ for more details.
        """
        kw["adapt_meth"] = "from_dict"
        return super().adapt_from(obj, obj_key, many=many, **kw)

    async def adapt_to_async(self, obj_key: str, many=False, **kw: Any) -> Any:
        """Asynchronously adapt to another format."""
        kw["adapt_meth"] = "to_dict"
        return await super().adapt_to_async(obj_key=obj_key, many=many, **kw)

    @classmethod
    async def adapt_from_async(cls, obj: Any, obj_key: str, many=False, **kw: Any):
        """Asynchronously create from another format."""
        kw["adapt_meth"] = "from_dict"
        return await super().adapt_from_async(obj, obj_key, many=many, **kw)

    def to_df(self, columns: list[str] | None = None, **kw: Any):
        """Convert to DataFrame."""
        try:
            from lionagi.adapters.pandas_ import DataFrameAdapter
        except ImportError as e:
            raise ImportError(
                "pandas is required for to_df(). "
                "Please install it via: pip install pandas  or  uv add pandas"
            ) from e

        df = DataFrameAdapter.to_obj(list(self.collections.values()), adapt_meth="to_dict", **kw)
        if columns:
            return df[columns]
        return df

    def dump(
        self,
        fp: str | Path | None,
        obj_key: Literal["json", "csv", "parquet"] = "json",
        *,
        mode: Literal["w", "a"] = "w",
        clear=False,
        **kw,
    ) -> None:
        """Export collection to file in specified format.

        Args:
            fp: File path or buffer to write to. If None, returns string.
                Cannot be None if obj_key is 'parquet'.
            obj_key: Format to export ('json', 'csv', 'parquet').
            mode: File mode ('w' for write, 'a' for append).
            clear: If True, clear the collection after export.
            **kw: Additional arguments for the export method, pandas kwargs
        """
        df = self.to_df()
        match obj_key:
            case "parquet":
                df.to_parquet(fp, engine="pyarrow", index=False, **kw)
            case "json":
                out = df.to_json(fp, orient="records", lines=True, mode=mode, **kw)
                return out if out is not None else None
            case "csv":
                out = df.to_csv(fp, index=False, mode=mode, **kw)
                return out if out is not None else None
            case _:
                raise ValueError(
                    f"Unsupported obj_key: {obj_key}. Supported keys are 'json', 'csv', 'parquet'."
                )

        if clear:
            self.clear()

    @async_synchronized
    async def adump(
        self,
        fp: str | Path,
        *,
        obj_key: Literal["json", "csv", "parquet"] = "json",
        mode: Literal["w", "a"] = "w",
        clear=False,
        **kw,
    ) -> None:
        return self.dump(fp, obj_key=obj_key, mode=mode, clear=clear, **kw)

    def filter_by_type(
        self,
        item_type: type[T] | list | set,
        strict_type: bool = False,
        as_pile: bool = False,
        reverse: bool = False,
        num_items: int | None = None,
    ) -> list[T]:
        if isinstance(item_type, type):
            if is_union_type(item_type):
                item_type = set(union_members(item_type))
            else:
                item_type = {item_type}

        if isinstance(item_type, list | tuple):
            item_type = set(item_type)

        if not isinstance(item_type, set):
            raise TypeError("item_type must be a type or a list/set of types")

        meth = None

        if strict_type:
            meth = lambda item: type(item) in item_type  # noqa: E731
        else:
            meth = lambda item: any(isinstance(item, t) for t in item_type) is True  # noqa: E731

        out = []
        prog = list(self.progression) if not reverse else reversed(list(self.progression))
        for i in prog:
            item = self.collections[i]
            if meth(item):
                out.append(item)
            if num_items is not None and len(out) == num_items:
                break

        if as_pile:
            return self.__class__(collections=out, item_type=item_type, strict_type=strict_type)
        return out


def to_list_type(value: Any, /) -> list[Any]:
    """Convert input to a list format"""
    if value is None:
        return []
    if isinstance(value, UUID):
        return [value]
    if isinstance(value, str):
        return ID.get_id(value) if ID.is_id(value) else []
    if isinstance(value, Element):
        return [value]
    if hasattr(value, "values") and callable(value.values):
        return list(value.values())
    if isinstance(value, list | tuple | set | deque | Generator):
        return list(value)
    return [value]


if not _ADAPTER_REGISTERED:
    from lionagi.adapters.csv_ import CsvAdapter
    from lionagi.adapters.json_ import JsonAdapter

    Pile.register_adapter(CsvAdapter)
    Pile.register_adapter(JsonAdapter)

    _ADAPTER_REGISTERED = True

Pile = Pile

__all__ = ("Pile",)

# File: lionagi/protocols/generic/pile.py
