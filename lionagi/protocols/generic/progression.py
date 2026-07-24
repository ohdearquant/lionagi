# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections import deque
from typing import Any, Generic, TypeVar
from uuid import UUID

from pydantic import Field, PrivateAttr, field_serializer, field_validator
from typing_extensions import Self

from lionagi._errors import ItemNotFoundError

from .._concepts import Ordering
from .element import ID, Element, validate_order

T = TypeVar("T", bound=Element)


__all__ = (
    "Progression",
    "prog",
)


class _SyncedDeque(deque):
    """deque that keeps its owning Progression's `_members` cache in sync.

    `Progression.order` is a public field, so callers can mutate the deque
    object directly (`progression.order.append(x)`, `.clear()`, ...) instead
    of going through Progression's own methods. Every mutating method is
    overridden here so `_members`, which `__contains__` actually checks,
    never drifts from `order` regardless of how it was mutated.
    """

    def __init__(self, iterable=(), owner: Progression | None = None):
        super().__init__(iterable)
        self._owner = owner

    def append(self, item) -> None:
        super().append(item)
        if self._owner is not None:
            self._owner._members.add(item)

    def appendleft(self, item) -> None:
        super().appendleft(item)
        if self._owner is not None:
            self._owner._members.add(item)

    def extend(self, iterable) -> None:
        items = list(iterable)
        super().extend(items)
        if self._owner is not None:
            self._owner._members.update(items)

    def extendleft(self, iterable) -> None:
        items = list(iterable)
        super().extendleft(items)
        if self._owner is not None:
            self._owner._members.update(items)

    def insert(self, index, item) -> None:
        super().insert(index, item)
        if self._owner is not None:
            self._owner._members.add(item)

    def pop(self):
        item = super().pop()
        if self._owner is not None and item not in self:
            self._owner._members.discard(item)
        return item

    def popleft(self):
        item = super().popleft()
        if self._owner is not None and item not in self:
            self._owner._members.discard(item)
        return item

    def remove(self, value) -> None:
        super().remove(value)
        if self._owner is not None and value not in self:
            self._owner._members.discard(value)

    def clear(self) -> None:
        super().clear()
        if self._owner is not None:
            self._owner._members.clear()

    def __delitem__(self, index) -> None:
        # deque itself only supports integer indices here (slicing raises
        # TypeError natively); Progression's own slice handling never calls
        # this directly, it rebuilds the whole deque via `_replace_order`.
        item = self[index]
        super().__delitem__(index)
        if self._owner is not None and item not in self:
            self._owner._members.discard(item)

    def __setitem__(self, index, value) -> None:
        old = self[index]
        super().__setitem__(index, value)
        if self._owner is not None:
            if old not in self:
                self._owner._members.discard(old)
            self._owner._members.add(value)

    def __iadd__(self, other):
        self.extend(other)
        return self


class Progression(Element, Ordering[T], Generic[T]):
    """Ordered sequence of item UUIDs with set-backed O(1) membership checks."""

    order: deque[ID[T].ID] = Field(
        default_factory=deque,
        title="Order",
        description="A sequence of IDs representing the progression.",
    )
    name: str | None = Field(
        None,
        title="Name",
        description="A human-readable identifier for the progression.",
    )
    _members: set[UUID] = PrivateAttr(default_factory=set)
    _iterator: Any = PrivateAttr(default=None)

    def model_post_init(self, __context: Any) -> None:
        super().model_post_init(__context)
        self.order = _SyncedDeque(self.order, owner=self)
        self._members = set(self.order)

    def _rebuild_members(self) -> None:
        self._members = set(self.order)

    def _replace_order(self, items) -> None:
        """Reconstruct `order` from `items`, preserving the `_SyncedDeque` wrapper.

        A bare `self.order = deque(items)` would swap in a plain deque and
        silently drop future membership syncing for direct mutations.
        """
        self.order = _SyncedDeque(items, owner=self)
        self._rebuild_members()

    @field_validator("order", mode="before")
    def _validate_ordering(cls, value: Any) -> deque[UUID]:
        return deque(validate_order(value))

    @field_serializer("order")
    def _serialize_order(self, value: deque[UUID]) -> list[str]:
        return [str(x) for x in self.order]

    def __len__(self) -> int:
        return len(self.order)

    def __bool__(self) -> bool:
        return bool(self.order)

    def __contains__(self, item: Any) -> bool:
        try:
            refs = validate_order(item)
            return all(ref in self._members for ref in refs)
        except (ValueError, TypeError):
            return False

    def __getitem__(self, key: int | slice) -> UUID | list[UUID]:
        if not isinstance(key, (int, slice)):
            key_cls = key.__class__.__name__
            raise TypeError(f"indices must be integers or slices, not {key_cls}")
        try:
            if isinstance(key, slice):
                a = list(self.order)[key]
                if not a:
                    raise ItemNotFoundError(f"index {key} item not found")
                return self.__class__(order=a)
            else:
                a = self.order[key]
                return a
        except IndexError:
            raise ItemNotFoundError(f"index {key} item not found") from None

    def __setitem__(self, key: int | slice, value: Any) -> None:
        refs = validate_order(value)
        if isinstance(key, slice):
            as_list = list(self.order)
            as_list[key] = refs
            self._replace_order(as_list)
        else:
            try:
                old = self.order[key]
                self.order[key] = refs[0]
                if old not in self.order:
                    self._members.discard(old)
                self._members.add(refs[0])
            except IndexError:
                self.order.insert(key, refs[0])
                self._members.add(refs[0])

    def __delitem__(self, key: int | slice) -> None:
        if isinstance(key, slice):
            as_list = list(self.order)
            del as_list[key]
            self._replace_order(as_list)
        else:
            del self.order[key]
            self._rebuild_members()

    def __iter__(self):
        return iter(self.order)

    def __next__(self) -> UUID:
        # `iter(self.order)` must be the SAME iterator across calls - a fresh
        # `iter(...)` per call would always yield the first element instead of
        # advancing, so the live iterator is cached on the instance.
        if self._iterator is None:
            self._iterator = iter(self.order)
        try:
            return next(self._iterator)
        except StopIteration:
            self._iterator = None
            raise StopIteration("No more items in the progression") from None

    def __list__(self) -> list[UUID]:
        return list(self.order)

    def clear(self) -> None:
        self.order.clear()
        self._members.clear()

    def include(self, item: Any, /) -> bool:
        try:
            refs = validate_order(item)
        except ValueError:
            return False
        if not refs:
            return True

        appended = False
        for ref in refs:
            if ref not in self._members:
                self.order.append(ref)
                self._members.add(ref)
                appended = True
        return appended

    def exclude(self, item: Any, /) -> bool:
        try:
            refs = validate_order(item)
        except ValueError:
            return False
        if not refs:
            return True

        before = len(self.order)
        rset = set(refs)
        self._replace_order(x for x in self.order if x not in rset)
        return len(self.order) < before

    def append(self, item: Any, /) -> None:
        if isinstance(item, Element):
            self.order.append(item.id)
            self._members.add(item.id)
            return
        refs = validate_order(item)
        self.order.extend(refs)
        self._members.update(refs)

    def pop(self, index: int = -1) -> UUID:
        try:
            if index == -1 or index == len(self.order) - 1:
                uid = self.order.pop()
            elif index == 0:
                uid = self.order.popleft()
            else:
                uid = self.order[index]
                del self.order[index]
            if uid not in self.order:
                self._members.discard(uid)
            return uid
        except Exception as e:
            raise ItemNotFoundError(str(e)) from e

    def popleft(self) -> UUID:
        if not self.order:
            raise ItemNotFoundError("No items in progression.")
        uid = self.order.popleft()
        if uid not in self.order:
            self._members.discard(uid)
        return uid

    def remove(self, item: Any, /) -> None:
        try:
            refs = validate_order(item)
        except ValueError as e:
            raise ItemNotFoundError(str(item)) from e
        if not refs:
            return
        missing = [r for r in refs if r not in self._members]
        if missing:
            raise ItemNotFoundError(str(missing))
        rset = set(refs)
        self._replace_order(x for x in self.order if x not in rset)

    def count(self, item: Any, /) -> int:
        ref = ID.get_id(item)
        return self.order.count(ref)

    def index(self, item: Any, start: int = 0, end: int | None = None) -> int:
        ref = ID.get_id(item)
        if end is not None:
            return self.order.index(ref, start, end)
        return self.order.index(ref, start)

    def extend(self, other: Progression) -> None:
        if not isinstance(other, Progression):
            raise ValueError("Can only extend with another Progression.")
        self.order.extend(other.order)
        self._members.update(other.order)

    def __add__(self, other: Any) -> Progression[T]:
        new_refs = validate_order(other)
        return Progression(order=list(self.order) + new_refs)

    def __radd__(self, other: Any) -> Progression[T]:
        new_refs = validate_order(other)
        return Progression(order=new_refs + list(self.order))

    def __iadd__(self, other: Any) -> Self:
        self.append(other)
        return self

    def __sub__(self, other: Any) -> Progression[T]:
        refs = validate_order(other)
        remove_set = set(refs)
        return Progression(order=[x for x in self.order if x not in remove_set])

    def __isub__(self, other: Any) -> Self:
        self.remove(other)
        return self

    def insert(self, index: int, item: ID.RefSeq, /) -> None:
        item_ = validate_order(item)
        for i in reversed(item_):
            uid = ID.get_id(i)
            self.order.insert(index, uid)
            self._members.add(uid)

    def _validate_index(self, index: int, allow_end: bool = False) -> int:
        length = len(self.order)
        if length == 0 and not allow_end:
            raise ItemNotFoundError("Progression is empty")

        if index < 0:
            index = length + index

        max_index = length if allow_end else length - 1
        if index < 0 or index > max_index:
            raise ItemNotFoundError(
                f"Index {index} out of range for progression of length {length}"
            )
        return index

    def move(self, from_index: int, to_index: int) -> None:
        from_index = self._validate_index(from_index)
        to_index = self._validate_index(to_index, allow_end=True)

        item = self.order[from_index]
        del self.order[from_index]
        if from_index < to_index:
            to_index -= 1
        self.order.insert(to_index, item)

    def swap(self, index1: int, index2: int) -> None:
        index1 = self._validate_index(index1)
        index2 = self._validate_index(index2)
        self.order[index1], self.order[index2] = (
            self.order[index2],
            self.order[index1],
        )

    def reverse(self) -> None:
        self.order.reverse()

    def __reversed__(self) -> Progression[T]:
        return Progression(order=list(self.order)[::-1])

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Progression):
            return NotImplemented
        return (list(self.order) == list(other.order)) and (self.name == other.name)

    def __gt__(self, other: Progression[T]) -> bool:
        return list(self.order) > list(other.order)

    def __lt__(self, other: Progression[T]) -> bool:
        return list(self.order) < list(other.order)

    def __ge__(self, other: Progression[T]) -> bool:
        return list(self.order) >= list(other.order)

    def __le__(self, other: Progression[T]) -> bool:
        return list(self.order) <= list(other.order)

    def __repr__(self) -> str:
        return f"Progression(name={self.name}, order={self.order})"


def prog(order: Any, name: str = None, /) -> Progression:
    return Progression(order=order, name=name)
