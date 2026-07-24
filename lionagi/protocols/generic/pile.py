# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import threading
from collections import deque
from collections.abc import AsyncIterator, Callable, Generator, Iterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, ClassVar, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import Field, PrivateAttr, field_serializer
from typing_extensions import Self, override

from lionagi._errors import ItemExistsError, ItemNotFoundError, ValidationError
from lionagi.adapters._base import Adaptable, AsyncAdaptable
from lionagi.ln import is_same_dtype, is_union_type, union_members
from lionagi.ln._utils import async_synchronized, synchronized
from lionagi.ln.concurrency import Lock as ConcurrencyLock
from lionagi.ln.concurrency import sleep as _concurrency_sleep
from lionagi.utils import UNDEFINED, to_list

from .._concepts import Observable
from .element import ID, Collective, E, Element, validate_order
from .progression import Progression

D = TypeVar("D")
T = TypeVar("T", bound=E)

_ADAPTER_REGISTERED = False


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
    # Don't drop falsy Observables (e.g. empty Progression/Pile with len()==0).
    if not value and not isinstance(value, Observable):
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
    """Ordered collection of Observable elements with a two-lock concurrency contract.

    Concurrency contract:

    - The sync API (``@synchronized`` methods, subscripting, iteration
      snapshots) is thread-safe under ``_lock``.
    - The async API (``a``-prefixed ``@async_synchronized`` methods) is
      task-safe under ``_async_lock`` AND excludes sync callers in other
      threads: the async wrapper holds both locks (async lock first, then a
      non-blocking spin on the threading lock) for the duration of the call.
    - Iteration (``__iter__`` / ``__aiter__``) captures a point-in-time
      snapshot of the *order* under the lock; item lookup stays live, so
      removing a not-yet-visited item raises ``KeyError`` at that step
      (fail-loud) instead of silently yielding a stale object. ``keys`` /
      ``values`` / ``items`` return fully materialized snapshots.
    - The exclusion boundary is CROSS-THREAD, not cross-task. On the event
      loop's own thread, a sync call made by a different task while an async
      operation is mid-await re-enters the RLock (thread-owned) and proceeds.
      Same-thread callers are cooperative by design; enforcing task-level
      exclusion for sync calls on the loop thread would deadlock the loop.
      Async-side critical regions (``async with pile``, ``adump``,
      ``adapt_to_async``, ``__aiter__``) all use the ordered both-lock
      protocol, so they exclude sync callers running in other threads.
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

    # Two locks, one ordered protocol: sync methods hold _lock; async methods
    # hold _async_lock THEN _lock (via @async_synchronized), so the two API
    # families mutually exclude. _lock is an RLock because sync methods
    # reenter each other (update -> include, exclude -> pop) and async bodies
    # may call @synchronized siblings while the wrapper already holds it.
    _lock: threading.RLock = PrivateAttr(default_factory=threading.RLock)
    _async_lock: ConcurrencyLock = PrivateAttr(default_factory=ConcurrencyLock)
    _next_cursor: Iterator[T] | None = PrivateAttr(default=None)

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
        return cls(**data)

    @synchronized
    def __setitem__(
        self,
        key: ID.Ref | ID.RefSeq | int | slice,
        item: ID.ItemSeq | ID.Item,
    ) -> None:
        self._setitem(key, item)

    @synchronized
    def pop(
        self,
        key: ID.Ref | ID.RefSeq | int | slice,
        default: D = UNDEFINED,
        /,
    ) -> T | Pile | D:
        return self._pop(key, default)

    def remove(
        self,
        item: T,
        /,
    ) -> None:
        if isinstance(item, int | slice):
            raise TypeError("Invalid item type for remove, should be ID or Item(s)")
        if item in self:
            self.pop(item)
            return
        raise ItemNotFoundError(f"{item}")

    @synchronized
    def include(self, item: ID.ItemSeq | ID.Item, /) -> None:
        item_dict = _validate_collections(item, self.item_type, self.strict_type)
        self.progression.include(list(item_dict.keys()))
        self.collections.update(item_dict)

    @synchronized
    def exclude(
        self,
        item: ID.ItemSeq | ID.Item,
        /,
    ) -> None:
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
        others = _validate_collections(other, self.item_type, self.strict_type)
        for i in others.keys():
            if i in self.collections:
                self.collections[i] = others[i]
            else:
                self.include(others[i])

    @synchronized
    def insert(self, index: int, item: T, /) -> None:
        self._insert(index, item)

    @synchronized
    def append(self, item: T, /) -> None:
        self.update(item)

    @synchronized
    def get(
        self,
        key: ID.Ref | ID.RefSeq | int | slice,
        default: D = UNDEFINED,
        /,
    ) -> T | Pile | D:
        return self._get(key, default)

    @synchronized
    def keys(self) -> Sequence[str]:
        return list(self.progression)

    @synchronized
    def values(self) -> Sequence[T]:
        return [self.collections[key] for key in self.progression]

    @synchronized
    def items(self) -> Sequence[tuple[UUID, T]]:
        return [(key, self.collections[key]) for key in self.progression]

    @synchronized
    def is_empty(self) -> bool:
        return len(self.progression) == 0

    @synchronized
    def size(self) -> int:
        return len(self.progression)

    def __iter__(self) -> Iterator[T]:
        with self._lock:
            order = list(self.progression)

        # Order is a point-in-time snapshot, but item lookup stays live:
        # removing a not-yet-visited item makes the traversal raise KeyError
        # (fail-loud) rather than silently yielding a stale object.
        for key in order:
            yield self.collections[key]

    def __next__(self) -> T:
        if self._next_cursor is None:
            self._next_cursor = iter(self)
        try:
            return next(self._next_cursor)
        except StopIteration:
            self._next_cursor = None
            raise StopIteration("End of pile") from None

    @synchronized
    def __getitem__(self, key: ID.Ref | ID.RefSeq | int | slice) -> Any | list | T:
        return self._getitem(key)

    @synchronized
    def __contains__(self, item: ID.RefSeq | ID.Ref) -> bool:
        return item in self.progression

    @synchronized
    def __len__(self) -> int:
        return len(self.collections)

    @override
    @synchronized
    def __bool__(self) -> bool:
        return not self.is_empty()

    def __list__(self) -> list[T]:
        return self.values()

    def __ior__(self, other: Pile) -> Self:
        if not isinstance(other, Pile):
            raise TypeError(f"Invalid type for Pile operation. expected <Pile>, got {type(other)}")
        other = _validate_collections(list(other), self.item_type, self.strict_type)
        self.include(other)
        return self

    def __or__(self, other: Pile) -> Pile:
        if not isinstance(other, Pile):
            raise TypeError(f"Invalid type for Pile operation. expected <Pile>, got {type(other)}")

        result = self.__class__(
            collections=self.values(),
            item_type=self.item_type,
            strict_type=self.strict_type,
            order=list(self.progression),
        )
        result.include(list(other))
        return result

    def __ixor__(self, other: Pile) -> Self:
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
            collections=values,
            item_type=self.item_type,
            strict_type=self.strict_type,
        )
        return result

    def __iand__(self, other: Pile) -> Self:
        if not isinstance(other, Pile):
            raise TypeError(f"Invalid type for Pile operation. expected <Pile>, got {type(other)}")

        to_exclude = []
        for i in self.values():
            if i not in other:
                to_exclude.append(i)
        self.exclude(to_exclude)
        return self

    def __and__(self, other: Pile) -> Pile:
        if not isinstance(other, Pile):
            raise TypeError(f"Invalid type for Pile operation. expected <Pile>, got {type(other)}")

        values = [i for i in self if i in other]
        return self.__class__(
            collections=values,
            item_type=self.item_type,
            strict_type=self.strict_type,
        )

    @override
    def __str__(self) -> str:
        return f"Pile({len(self)})"

    @override
    def __repr__(self) -> str:
        length = len(self)
        if length == 0:
            return "Pile()"
        elif length == 1:
            return f"Pile({next(iter(self.collections.values())).__repr__()})"
        else:
            return f"Pile({length})"

    def __getstate__(self):
        state = self.__dict__.copy()
        state.pop("_lock", None)
        state.pop("_async_lock", None)
        state.pop("_next_cursor", None)
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        try:
            priv = object.__getattribute__(self, "__pydantic_private__")
        except AttributeError:
            priv = {}
            object.__setattr__(self, "__pydantic_private__", priv)
        priv["_lock"] = threading.RLock()
        priv["_async_lock"] = ConcurrencyLock()
        priv["_next_cursor"] = None

    def __deepcopy__(self, memo):
        import copy

        cls = self.__class__
        result = cls.__new__(cls)
        memo[id(self)] = result
        for k, v in self.__dict__.items():
            object.__setattr__(result, k, copy.deepcopy(v, memo))
        priv = {}
        for k, v in (self.__pydantic_private__ or {}).items():
            if k in ("_lock", "_async_lock", "_next_cursor"):
                continue
            priv[k] = copy.deepcopy(v, memo)
        priv["_lock"] = threading.RLock()
        priv["_async_lock"] = ConcurrencyLock()
        priv["_next_cursor"] = None
        object.__setattr__(result, "__pydantic_private__", priv)
        return result

    @property
    def lock(self):
        return self._lock

    @property
    def async_lock(self):
        # Task-level serialization only: holding this lock alone does NOT
        # exclude sync callers in other threads. Use `async with pile:` for
        # the full cross-thread boundary.
        return self._async_lock

    async def _spin_acquire_sync_lock(self) -> None:
        # Non-blocking spin so the event loop keeps running while a sync
        # thread holds _lock. Callers must already hold _async_lock.
        while not self._lock.acquire(blocking=False):
            await _concurrency_sleep(0.0005)

    @asynccontextmanager
    async def _both_locks(self) -> AsyncIterator[None]:
        # Ordered both-lock protocol shared by every async critical region:
        # _async_lock serializes tasks, then the spin excludes sync threads.
        async with self._async_lock:
            await self._spin_acquire_sync_lock()
            try:
                yield
            finally:
                self._lock.release()

    @async_synchronized
    async def asetitem(
        self,
        key: ID.Ref | ID.RefSeq | int | slice,
        item: ID.Item | ID.ItemSeq,
        /,
    ) -> None:
        self._setitem(key, item)

    @async_synchronized
    async def apop(
        self,
        key: ID.Ref | ID.RefSeq | int | slice,
        default: Any = UNDEFINED,
        /,
    ):
        return self._pop(key, default)

    @async_synchronized
    async def aremove(
        self,
        item: ID.Ref | ID.RefSeq,
        /,
    ) -> None:
        self.remove(item)

    @async_synchronized
    async def ainclude(
        self,
        item: ID.ItemSeq | ID.Item,
        /,
    ) -> None:
        self.include(item)
        if item not in self:
            raise TypeError(f"Item {item} is not of allowed types")

    @async_synchronized
    async def aexclude(
        self,
        item: ID.Ref | ID.RefSeq,
        /,
    ) -> None:
        self.exclude(item)

    @async_synchronized
    async def aclear(self) -> None:
        self._clear()

    @async_synchronized
    async def aupdate(
        self,
        other: ID.ItemSeq | ID.Item,
        /,
    ) -> None:
        self.update(other)

    @async_synchronized
    async def aget(
        self,
        key: Any,
        default=UNDEFINED,
        /,
    ) -> list | Any | T:
        return self._get(key, default)

    async def __aiter__(self) -> AsyncIterator[T]:
        async with self._both_locks():
            order = list(self.progression)

        # Same contract as __iter__: snapshotted order, live item lookup.
        for key in order:
            yield self.collections[key]

    async def __anext__(self) -> T:
        try:
            return await anext(self.AsyncPileIterator(self))
        except StopAsyncIteration:
            raise StopAsyncIteration("End of pile") from None

    @synchronized
    def filter(self, predicate: Callable[[T], bool]) -> Pile[T]:
        return self._filter_by_function(predicate)

    def _filter_by_function(self, func: Callable[[T], bool]) -> Pile[T]:
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

    def _getitem(self, key: Any) -> Any | list | T:
        if key is None:
            raise ValueError("getitem key not provided.")

        if isinstance(key, type):
            from lionagi.ln.types import TypeFilter

            return self._filter_by_function(TypeFilter(key))

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
                    self.__class__(
                        collections=result,
                        item_type=self.item_type,
                        strict_type=self.strict_type,
                    )
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
            self._agen: AsyncIterator[T] | None = None

        def __aiter__(self) -> AsyncIterator[T]:
            return self

        async def __anext__(self) -> T:
            # Delegate to Pile.__aiter__ so this legacy iterator shares the
            # both-lock snapshot contract and never takes the blocking sync
            # subscript path on the event loop.
            if self._agen is None:
                self._agen = self.pile.__aiter__()
            item = await anext(self._agen)
            # Cooperative checkpoint between elements so bulk iteration over a
            # large pile cannot monopolize the event loop.
            await _concurrency_sleep(0)
            return item

    async def __aenter__(self) -> Self:
        # Ordered both-lock acquisition held for the whole `async with pile:`
        # block, so sync callers in other threads are excluded until exit.
        await self.async_lock.__aenter__()
        try:
            await self._spin_acquire_sync_lock()
        except BaseException:
            await self.async_lock.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        self._lock.release()
        await self.async_lock.__aexit__(exc_type, exc_val, exc_tb)

    def is_homogenous(self) -> bool:
        return len(self.collections) < 2 or is_same_dtype(list(self.collections.values()))

    @classmethod
    def list_adapters(cls) -> list[str]:
        syn_ = cls._registry()._reg.keys()
        asy_ = cls._areg()._reg.keys()
        return list(set(syn_) | set(asy_))

    def adapt_to(self, obj_key: str, many=False, **kw: Any) -> Any:
        kw["adapt_meth"] = "to_dict"
        return super().adapt_to(obj_key=obj_key, many=many, **kw)

    @classmethod
    def adapt_from(cls, obj: Any, obj_key: str, many=False, **kw: Any):
        kw["adapt_meth"] = "from_dict"
        return super().adapt_from(obj, obj_key, many=many, **kw)

    async def adapt_to_async(self, obj_key: str, many=False, **kw: Any) -> Any:
        kw["adapt_meth"] = "to_dict"
        # Serialize under the both-lock protocol so a sync-thread mutation
        # cannot race the adapter's snapshot of the pile.
        async with self._both_locks():
            return await super().adapt_to_async(obj_key=obj_key, many=many, **kw)

    @classmethod
    async def adapt_from_async(cls, obj: Any, obj_key: str, many=False, **kw: Any):
        kw["adapt_meth"] = "from_dict"
        return await super().adapt_from_async(obj, obj_key, many=many, **kw)

    def to_df(self, columns: list[str] | None = None, **kw: Any):
        try:
            from lionagi.adapters.pandas_ import DataFrameAdapter
        except ImportError as e:
            raise ImportError(
                "pandas is required for to_df(). "
                "Please install it via: pip install pandas  or  uv add pandas"
            ) from e

        # Serialize in progression (logical) order, not dict insertion order,
        # so the frame matches iteration and every ordered dump/adump path.
        ordered = [self.collections[key] for key in self.progression]
        df = DataFrameAdapter.to_obj(ordered, adapt_meth="to_dict", **kw)
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
    ) -> str | None:
        df = self.to_df()
        out = None
        match obj_key:
            case "parquet":
                df.to_parquet(fp, engine="pyarrow", index=False, **kw)
            case "json":
                out = df.to_json(fp, orient="records", lines=True, mode=mode, **kw)
            case "csv":
                out = df.to_csv(fp, index=False, mode=mode, **kw)
            case _:
                raise ValueError(
                    f"Unsupported obj_key: {obj_key}. Supported keys are 'json', 'csv', 'parquet'."
                )

        # Clear only after a successful write, and for every format: the json/csv
        # branches previously returned early and skipped this.
        if clear:
            self.clear()
        return out

    async def adump(
        self,
        fp: str | Path,
        *,
        obj_key: Literal["json", "csv", "parquet"] = "json",
        mode: Literal["w", "a"] = "w",
        clear=False,
        **kw,
    ) -> None:
        from lionagi.ln.concurrency import run_sync

        async with self._both_locks():
            snapshot_ids = set(self.collections.keys())
            df = self.to_df()

        def _write() -> None:
            match obj_key:
                case "parquet":
                    df.to_parquet(fp, engine="pyarrow", index=False, **kw)
                case "json":
                    df.to_json(fp, orient="records", lines=True, mode=mode, **kw)
                case "csv":
                    df.to_csv(fp, index=False, mode=mode, **kw)
                case _:
                    raise ValueError(
                        f"Unsupported obj_key: {obj_key}. "
                        "Supported keys are 'json', 'csv', 'parquet'."
                    )

        await run_sync(_write)

        if clear:
            async with self._both_locks():
                self.progression.exclude(list(snapshot_ids))
                for uid in snapshot_ids:
                    self.collections.pop(uid, None)

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
    if value is None:
        return []
    if isinstance(value, UUID):
        return [value]
    if isinstance(value, str):
        return [ID.get_id(value)] if ID.is_id(value) else []
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
