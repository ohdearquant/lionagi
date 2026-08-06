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


class _MembersDeque(deque):
    """A ``deque`` that keeps a bound membership ``set`` in sync on every mutation.

    ``Progression.order`` is a public, directly-mutable deque: external code
    (tests, third-party callers, ``Pile`` internals) mutates it in place —
    ``p.order.append(x)``, ``p.order[0] = x``, ``p.order.popleft()``, etc. A
    length-only staleness check (comparing ``len(order)`` across calls) cannot
    detect a length-preserving external write (e.g. ``order[0] = x``, or a
    ``popleft()`` paired with an ``append()``), so this class instead updates
    the bound set eagerly, inside every mutating operation, using the same
    duplicate-aware discard rule as the rest of ``Progression`` (an id is only
    dropped from the set once no occurrence of it remains in the deque).
    """

    def __init__(self, iterable=(), /, members: set | None = None):
        super().__init__(iterable)
        self._members_ref: set | None = members

    def bind(self, members: set) -> None:
        """Point this deque at a (freshly rebuilt) membership set."""
        self._members_ref = members

    def __setitem__(self, key, value) -> None:
        if isinstance(key, slice):
            super().__setitem__(key, value)
            if self._members_ref is not None:
                self._members_ref.clear()
                self._members_ref.update(self)
        else:
            old = self[key]
            super().__setitem__(key, value)
            if self._members_ref is not None:
                if old not in self:
                    self._members_ref.discard(old)
                self._members_ref.add(value)

    def __delitem__(self, key) -> None:
        if isinstance(key, slice):
            super().__delitem__(key)
            if self._members_ref is not None:
                self._members_ref.clear()
                self._members_ref.update(self)
        else:
            old = self[key]
            super().__delitem__(key)
            if self._members_ref is not None and old not in self:
                self._members_ref.discard(old)

    def __iadd__(self, other):
        other = list(other)
        result = super().__iadd__(other)
        if self._members_ref is not None:
            self._members_ref.update(other)
        return result

    def append(self, x) -> None:
        super().append(x)
        if self._members_ref is not None:
            self._members_ref.add(x)

    def appendleft(self, x) -> None:
        super().appendleft(x)
        if self._members_ref is not None:
            self._members_ref.add(x)

    def pop(self):
        value = super().pop()
        if self._members_ref is not None and value not in self:
            self._members_ref.discard(value)
        return value

    def popleft(self):
        value = super().popleft()
        if self._members_ref is not None and value not in self:
            self._members_ref.discard(value)
        return value

    def insert(self, index, x) -> None:
        super().insert(index, x)
        if self._members_ref is not None:
            self._members_ref.add(x)

    def remove(self, value) -> None:
        super().remove(value)
        if self._members_ref is not None and value not in self:
            self._members_ref.discard(value)

    def extend(self, iterable) -> None:
        iterable = list(iterable)
        super().extend(iterable)
        if self._members_ref is not None:
            self._members_ref.update(iterable)

    def extendleft(self, iterable) -> None:
        iterable = list(iterable)
        super().extendleft(iterable)
        if self._members_ref is not None:
            self._members_ref.update(iterable)

    def clear(self) -> None:
        super().clear()
        if self._members_ref is not None:
            self._members_ref.clear()

    # `rotate` and `reverse` permute existing entries without changing which
    # ids are present, so the bound membership set never needs an update.


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
    # Length of `order` as of the last known-good `_members` sync, used only to
    # detect that `order` was replaced wholesale (`p.order = deque(...)`).
    #
    # CRITICAL DESIGN RATIONALE: `order` is a public, directly-mutable deque,
    # and external code (tests, third-party callers, `Pile` internals) is
    # expected to mutate it in place rather than going through `Progression`'s
    # methods. `order` is therefore always wrapped in a `_MembersDeque` (see
    # above), which keeps `_members` correct on every such mutation, including
    # length-preserving ones (`order[0] = x`, `popleft()` + `append()`) that a
    # length-only staleness check cannot see. Public behavior — membership
    # correctness after *any* direct `order` mutation — must never be
    # narrowed to "only length-changing mutations are safe".
    _order_len: int = PrivateAttr(default=0)

    def model_post_init(self, __context: Any) -> None:
        super().model_post_init(__context)
        self._rebuild_members()

    def _rebuild_members(self) -> None:
        if not isinstance(self.order, _MembersDeque):
            self.order = _MembersDeque(self.order)
        self._members = set(self.order)
        self.order.bind(self._members)
        self._order_len = len(self.order)

    def _ensure_synced(self) -> None:
        # Must be called before any read of, or incremental update to,
        # `_members`/`_order_len`. `_MembersDeque` keeps `_members` correct on
        # every direct mutation of `order` itself, but `order` can also be
        # replaced wholesale (`p.order = deque(...)`, or a plain-deque copy
        # produced by pydantic re-validation) — the isinstance/length check
        # below catches that case and rewraps + rebuilds.
        if not isinstance(self.order, _MembersDeque) or len(self.order) != self._order_len:
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
            self._ensure_synced()
            members = self._members
            return all(ref in members for ref in refs)
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
            self.order = deque(as_list)
            self._rebuild_members()
        else:
            self._ensure_synced()
            try:
                old = self.order[key]
                self.order[key] = refs[0]
                if old not in self.order:
                    self._members.discard(old)
                self._members.add(refs[0])
                self._order_len = len(self.order)
            except IndexError:
                self.order.insert(key, refs[0])
                self._members.add(refs[0])
                self._order_len = len(self.order)

    def __delitem__(self, key: int | slice) -> None:
        if isinstance(key, slice):
            as_list = list(self.order)
            del as_list[key]
            self.order = deque(as_list)
        else:
            del self.order[key]
        self._rebuild_members()

    def __iter__(self):
        return iter(self.order)

    # Iterable, but deliberately not an iterator: the cursor belongs to the
    # object ``iter(self)`` hands back, so two traversals never share position.
    # ``next(progression)`` raises TypeError; use ``next(iter(progression))``.

    def __list__(self) -> list[UUID]:
        return list(self.order)

    def clear(self) -> None:
        self.order.clear()
        self._members.clear()
        self._order_len = 0

    def include(self, item: Any, /) -> bool:
        try:
            refs = validate_order(item)
        except ValueError:
            return False
        if not refs:
            return True

        self._ensure_synced()
        appended = False
        for ref in refs:
            if ref not in self._members:
                self.order.append(ref)
                self._members.add(ref)
                appended = True
        self._order_len = len(self.order)
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
        self.order = deque(x for x in self.order if x not in rset)
        self._rebuild_members()
        return len(self.order) < before

    def append(self, item: Any, /) -> None:
        self._ensure_synced()
        if isinstance(item, Element):
            self.order.append(item.id)
            self._members.add(item.id)
            self._order_len = len(self.order)
            return
        refs = validate_order(item)
        self.order.extend(refs)
        self._members.update(refs)
        self._order_len = len(self.order)

    def pop(self, index: int = -1) -> UUID:
        self._ensure_synced()
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
            self._order_len = len(self.order)
            return uid
        except Exception as e:
            raise ItemNotFoundError(str(e)) from e

    def popleft(self) -> UUID:
        if not self.order:
            raise ItemNotFoundError("No items in progression.")
        self._ensure_synced()
        uid = self.order.popleft()
        if uid not in self.order:
            self._members.discard(uid)
        self._order_len = len(self.order)
        return uid

    def remove(self, item: Any, /) -> None:
        try:
            refs = validate_order(item)
        except ValueError as e:
            raise ItemNotFoundError(str(item)) from e
        if not refs:
            return
        self._ensure_synced()
        missing = [r for r in refs if r not in self._members]
        if missing:
            raise ItemNotFoundError(str(missing))
        rset = set(refs)
        self.order = deque(x for x in self.order if x not in rset)
        self._rebuild_members()

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
        self._ensure_synced()
        self.order.extend(other.order)
        self._members.update(other.order)
        self._order_len = len(self.order)

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
        self._ensure_synced()
        for i in reversed(item_):
            uid = ID.get_id(i)
            self.order.insert(index, uid)
            self._members.add(uid)
        self._order_len = len(self.order)

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


def prog(order: Any, name: str | None = None, /) -> Progression:
    return Progression(order=order, name=name)
