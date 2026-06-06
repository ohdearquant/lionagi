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


class Progression(Element, Ordering[T], Generic[T]):
    """Tracks an ordered sequence of item IDs, with optional naming.

    This class extends `Element` and implements `Ordering`, providing
    list-like operations for managing item IDs (based on `UUID`). It
    supports insertion, removal, slicing, and more. Items are stored in
    `order`, which is a simple list of IDs, and an optional `name`
    attribute can be assigned for identification.

    Thread Safety:
        Progression is **not** independently thread-safe.  Concurrent
        mutations (``append``, ``remove``, ``pop``) can leave ``order``
        and ``_members`` inconsistent.  When used inside a :class:`Pile`
        or :class:`Flow`, the outer container's lock provides the
        necessary synchronization.  Do not share a bare Progression
        across threads without external locking.

    Attributes:
        order (list[ID[E].ID]):
            The sequence of item IDs representing the progression.
        name (str | None):
            An optional human-readable identifier for the progression.
    """

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

    def model_post_init(self, __context: Any) -> None:
        """Initialize _members set from order for O(1) membership checks."""
        super().model_post_init(__context)
        self._members = set(self.order)

    def _rebuild_members(self) -> None:
        """Rebuild _members from order after bulk mutations."""
        self._members = set(self.order)

    @field_validator("order", mode="before")
    def _validate_ordering(cls, value: Any) -> deque[UUID]:
        """Ensures `order` is a valid deque of UUIDs.

        Args:
            value (Any): Input sequence (could be elements, IDs, etc.).

        Returns:
            deque[UUID]: The deque of validated UUID objects.
        """
        return deque(validate_order(value))

    @field_serializer("order")
    def _serialize_order(self, value: deque[UUID]) -> list[str]:
        """Serializes IDs in `order` to string form.

        Args:
            value (deque[UUID]): The order deque of ID objects.

        Returns:
            list[str]: A list of stringified IDs.
        """
        return [str(x) for x in self.order]

    def __len__(self) -> int:
        """Returns the number of items in this progression."""
        return len(self.order)

    def __bool__(self) -> bool:
        """Indicates if this progression has any items."""
        return bool(self.order)

    def __contains__(self, item: Any) -> bool:
        """Checks if one or more IDs exist in the progression. O(1) per ID.

        Args:
            item (Any): Could be an `Element`, `UUID`, `UUID`, string,
                or a sequence of these.

        Returns:
            bool: True if all IDs in `item` exist in this progression;
                otherwise False.
        """
        try:
            refs = validate_order(item)
            return all(ref in self._members for ref in refs)
        except (ValueError, TypeError):
            # validate_order raises ValueError for invalid items;
            # treat any such input as "not contained".
            return False

    def __getitem__(self, key: int | slice) -> UUID | list[UUID]:
        """Gets one or more items by index or slice.

        Args:
            key (int | slice): The index or slice.

        Returns:
            UUID | list[UUID]:
                A single ID if `key` is an int; a new `Progression`
                if `key` is a slice.

        Raises:
            ItemNotFoundError: If the index or slice is invalid.
            TypeError: If `key` is neither an int nor a slice.
        """
        if not isinstance(key, int | slice):
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
        """Sets items by index or slice.

        Args:
            key (int | slice):
                The position(s) to set.
            value (Any):
                One or more items to validate as IDs and assign.

        Raises:
            ValueError: If `value` can't be validated as IDs.
        """
        refs = validate_order(value)
        if isinstance(key, slice):
            # deque doesn't support slice assignment; rebuild via list
            as_list = list(self.order)
            as_list[key] = refs
            self.order = deque(as_list)
            self._rebuild_members()
        else:
            try:
                old = self.order[key]
                self.order[key] = refs[0]
                if old not in self.order:
                    self._members.discard(old)
                self._members.add(refs[0])
            except IndexError:
                # If key is out of range, insertion occurs
                self.order.insert(key, refs[0])
                self._members.add(refs[0])

    def __delitem__(self, key: int | slice) -> None:
        """Deletes item(s) by index or slice.

        Args:
            key (int | slice): The position(s) to delete.
        """
        if isinstance(key, slice):
            as_list = list(self.order)
            del as_list[key]
            self.order = deque(as_list)
        else:
            del self.order[key]
        self._rebuild_members()

    def __iter__(self):
        """Iterates over the IDs in this progression.

        Returns:
            Iterator[UUID]: An iterator over the ID elements.
        """
        return iter(self.order)

    def __next__(self) -> UUID:
        """Returns the next item if used as an iterator.

        Returns:
            UUID: The next item in the iteration.

        Raises:
            StopIteration: If there are no more items.
        """
        try:
            return next(iter(self.order))
        except StopIteration:
            raise StopIteration("No more items in the progression") from None

    def __list__(self) -> list[UUID]:
        """Returns a copy of all IDs in the progression.

        Returns:
            list[UUID]: A shallow copy of the ID list.
        """
        return list(self.order)

    def clear(self) -> None:
        """Removes all items from the progression."""
        self.order.clear()
        self._members.clear()

    def include(self, item: Any, /) -> bool:
        """Adds new IDs at the end if they are not already present.

        Args:
            item (Any): Could be a single ID/Element or a list/tuple
                of them.

        Returns:
            bool: True if at least one new ID was appended; otherwise
                False.
        """
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
        """Removes occurrences of the specified IDs.

        Args:
            item (Any):
                Could be a single ID/Element or a list/tuple of them.

        Returns:
            bool: True if one or more items were removed; otherwise False.
        """
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
        """Appends one or more IDs at the end of the progression.

        Args:
            item (Any):
                A single ID/Element or multiple items.
        """
        if isinstance(item, Element):
            self.order.append(item.id)
            self._members.add(item.id)
            return
        refs = validate_order(item)
        self.order.extend(refs)
        self._members.update(refs)

    def pop(self, index: int = -1) -> UUID:
        """Removes and returns one ID by index.

        Args:
            index (int):
                Position of the item to pop (default is the last item).

        Returns:
            UUID: The removed ID.

        Raises:
            ItemNotFoundError: If the index is invalid or out of range.
        """
        try:
            if index == -1 or index == len(self.order) - 1:
                uid = self.order.pop()
            elif index == 0:
                uid = self.order.popleft()
            else:
                # deque doesn't support pop(index); use del + __getitem__
                uid = self.order[index]
                del self.order[index]
            if uid not in self.order:
                self._members.discard(uid)
            return uid
        except Exception as e:
            raise ItemNotFoundError(str(e)) from e

    def popleft(self) -> UUID:
        """Removes and returns the first ID. O(1) with deque.

        Returns:
            UUID: The ID at the front of the progression.

        Raises:
            ItemNotFoundError: If the progression is empty.
        """
        if not self.order:
            raise ItemNotFoundError("No items in progression.")
        uid = self.order.popleft()
        if uid not in self.order:
            self._members.discard(uid)
        return uid

    def remove(self, item: Any, /) -> None:
        """Removes the first occurrence of each specified ID.

        Args:
            item (Any):
                One or more IDs/Elements to remove.

        Raises:
            ItemNotFoundError:
                If any ID is not present in the progression.
        """
        try:
            refs = validate_order(item)
        except ValueError as e:
            # Invalid UUID strings are treated as not found
            raise ItemNotFoundError(str(item)) from e
        if not refs:
            return
        missing = [r for r in refs if r not in self._members]
        if missing:
            raise ItemNotFoundError(str(missing))
        rset = set(refs)
        self.order = deque(x for x in self.order if x not in rset)
        self._rebuild_members()

    def count(self, item: Any, /) -> int:
        """Counts the number of occurrences of an ID.

        Args:
            item (Any): An ID/Element to count.

        Returns:
            int: Number of times the ID occurs in the progression.
        """
        ref = ID.get_id(item)
        return self.order.count(ref)

    def index(self, item: Any, start: int = 0, end: int | None = None) -> int:
        """Finds the index of the first occurrence of an ID.

        Args:
            item (Any):
                The ID/Element whose index is sought.
            start (int):
                Starting index for the search.
            end (int | None):
                Ending index (non-inclusive) for the search.

        Returns:
            int: The index of the item.

        Raises:
            ValueError: If the item is not found in that range.
        """
        ref = ID.get_id(item)
        # deque.index() supports start/end in Python 3.5+
        if end is not None:
            return self.order.index(ref, start, end)
        return self.order.index(ref, start)

    def extend(self, other: Progression) -> None:
        """Appends all IDs from another Progression to this one.

        Args:
            other (Progression): Another progression to merge.

        Raises:
            ValueError: If `other` is not a Progression.
        """
        if not isinstance(other, Progression):
            raise ValueError("Can only extend with another Progression.")
        self.order.extend(other.order)
        self._members.update(other.order)

    def __add__(self, other: Any) -> Progression[T]:
        """Returns a new Progression with IDs from both this and `other`.

        Args:
            other (Any):
                Item(s) that can be validated via `validate_order`.

        Returns:
            Progression[E]: A new progression with combined IDs.
        """
        new_refs = validate_order(other)
        return Progression(order=list(self.order) + new_refs)

    def __radd__(self, other: Any) -> Progression[T]:
        """Returns a new Progression with IDs from `other` + this.

        Args:
            other (Any):
                Item(s) that can be validated via `validate_order`.

        Returns:
            Progression[E]: A new progression with combined IDs.
        """
        new_refs = validate_order(other)
        return Progression(order=new_refs + list(self.order))

    def __iadd__(self, other: Any) -> Self:
        """In-place addition of IDs.

        Args:
            other (Any): One or more items to append.

        Returns:
            Self: The updated progression.
        """
        self.append(other)
        return self

    def __sub__(self, other: Any) -> Progression[T]:
        """Returns a new Progression excluding specified IDs.

        Args:
            other (Any): One or more items to remove.

        Returns:
            Progression[E]: A new progression with the IDs removed.
        """
        refs = validate_order(other)
        remove_set = set(refs)
        return Progression(order=[x for x in self.order if x not in remove_set])

    def __isub__(self, other: Any) -> Self:
        """In-place exclusion of specified IDs.

        Args:
            other (Any):
                One or more items to remove.

        Returns:
            Self: The updated progression.
        """
        self.remove(other)
        return self

    def insert(self, index: int, item: ID.RefSeq, /) -> None:
        """Inserts one or more IDs at a specified index.

        Args:
            index (int): Position to insert at.
            item (ID.RefSeq):
                One or more items to validate as IDs and insert.
        """
        item_ = validate_order(item)
        for i in reversed(item_):
            uid = ID.get_id(i)
            self.order.insert(index, uid)
            self._members.add(uid)

    def _validate_index(self, index: int, allow_end: bool = False) -> int:
        """Normalize and validate index (supports negative indexing).

        Args:
            index: Index to validate.
            allow_end: If True, allows index == len (for insertion).

        Returns:
            Normalized non-negative index.

        Raises:
            ItemNotFoundError: If index out of bounds.
        """
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
        """Move item from one position to another.

        Args:
            from_index: Source position (supports negative).
            to_index: Target position (supports negative).

        Raises:
            ItemNotFoundError: If either index is out of bounds.
        """
        from_index = self._validate_index(from_index)
        to_index = self._validate_index(to_index, allow_end=True)

        item = self.order[from_index]
        del self.order[from_index]
        if from_index < to_index:
            to_index -= 1
        self.order.insert(to_index, item)

    def swap(self, index1: int, index2: int) -> None:
        """Swap items at two positions.

        Args:
            index1: First position (supports negative).
            index2: Second position (supports negative).

        Raises:
            ItemNotFoundError: If either index is out of bounds.
        """
        index1 = self._validate_index(index1)
        index2 = self._validate_index(index2)
        self.order[index1], self.order[index2] = (
            self.order[index2],
            self.order[index1],
        )

    def reverse(self) -> None:
        """Reverse order in-place. _members unchanged since set is unordered."""
        self.order.reverse()

    def __reversed__(self) -> Progression[T]:
        """Returns a new reversed Progression.

        Returns:
            Progression[E]: A reversed copy of the current progression.
        """
        return Progression(order=list(self.order)[::-1])

    def __eq__(self, other: object) -> bool:
        """Checks equality with another Progression.

        Args:
            other (object): Another progression to compare.

        Returns:
            bool: True if both `order` and `name` match; otherwise False.
        """
        if not isinstance(other, Progression):
            return NotImplemented
        return (list(self.order) == list(other.order)) and (self.name == other.name)

    def __gt__(self, other: Progression[T]) -> bool:
        """Compares if this progression is "greater" by ID order."""
        return list(self.order) > list(other.order)

    def __lt__(self, other: Progression[T]) -> bool:
        """Compares if this progression is "less" by ID order."""
        return list(self.order) < list(other.order)

    def __ge__(self, other: Progression[T]) -> bool:
        """Compares if this progression is >= the other by ID order."""
        return list(self.order) >= list(other.order)

    def __le__(self, other: Progression[T]) -> bool:
        """Compares if this progression is <= the other by ID order."""
        return list(self.order) <= list(other.order)

    def __repr__(self) -> str:
        """Returns a string representation of the progression.

        Returns:
            str: A formatted string showing name and order contents.
        """
        return f"Progression(name={self.name}, order={self.order})"


def prog(order: Any, name: str = None, /) -> Progression:
    """Convenience function to quickly create a new Progression.

    Args:
        order (Any):
            A sequence of IDs or items convertible to IDs via
            `validate_order`.
        name (str | None):
            An optional name for this progression.

    Returns:
        Progression: A new Progression instance with the given order
        and name.
    """
    return Progression(order=order, name=name)
