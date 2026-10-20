# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""List conversion utilities with flattening, deduplication, and NA handling."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum as _Enum
from itertools import chain
from typing import Any, ClassVar

from ._hash import hash_dict
from ._lazy_init import LazyInit
from ._structural import (
    UnhashableStructuralValueError,
    _dataclass_structural_key,
    _is_memoryview_hash_refusal,
)
from .types import DataClass, Params

__all__ = ("to_list", "ToListParams")

_lazy = LazyInit()
_MODEL_LIKE = None
_MAP_LIKE = None
_SINGLETONE_TYPES = None
_SKIP_TYPE = None
_SKIP_TUPLE_SET = None
_BYTE_LIKE = (str, bytes, bytearray)
_TUPLE_SET = (tuple, set, frozenset)


def _do_init() -> None:
    """Initialize lazy type constants (Pydantic, sentinel types)."""
    from pydantic import BaseModel
    from pydantic_core import PydanticUndefinedType

    from .types import UndefinedType, UnsetType

    try:
        from msgspec import Struct

        _model_like = (BaseModel, Struct)
    except ImportError:
        _model_like = (BaseModel,)

    global _MODEL_LIKE, _MAP_LIKE, _SINGLETONE_TYPES, _SKIP_TYPE, _SKIP_TUPLE_SET
    _MODEL_LIKE = _model_like
    _MAP_LIKE = (Mapping, *_MODEL_LIKE)
    _SINGLETONE_TYPES = (UndefinedType, UnsetType, PydanticUndefinedType)
    _SKIP_TYPE = (*_BYTE_LIKE, *_MAP_LIKE, _Enum)
    _SKIP_TUPLE_SET = (*_SKIP_TYPE, *_TUPLE_SET)


def _equal_either_way(left: Any, right: Any) -> bool:
    """Whether two values compare equal, asking both directions.

    A value such as an array or a data frame gives no single truth value for
    ``==``, and a value's own ``__eq__`` may refuse a pair: with ``AttributeError``
    when it reads a field the other value does not carry, with ``TypeError`` or
    ``ValueError``, with ``RecursionError`` when it descends into a cycle without end,
    or with ``UnhashableStructuralValueError`` when the base equality cannot key a
    state. Each of those reads as unequal in the direction that raised, as
    ``DataClass.__eq__`` reads its own uncomparable state, so an unreadable state on one
    side does not decide the answer; any other error propagates. Counting a comparison
    that raises as unequal keeps both values rather than letting an unreadable pair
    decide the result, so two distinct cyclic values are two values and neither is lost.
    The comparison is asked inside the guard that reads it, so a result without one truth
    value is never tested bare. A non-transitive pair can still make the result depend on
    the order the values arrive in.
    """
    if left is right:
        return True
    for one, other in ((left, right), (right, left)):
        try:
            if one == other:
                return True
        except (
            AttributeError,
            RecursionError,
            TypeError,
            ValueError,
            UnhashableStructuralValueError,
        ):
            continue
    return False


def _dataclass_content_key(value: DataClass) -> int | None:
    """A transient hash of a DataClass value's declared field state, or ``None``.

    ``None`` means the state is one the base equality cannot compare either: a deleted
    field, a cycle, or nesting past the recursion limit. Any other error, such as a
    ``ValueError`` from a field value's own ``__hash__``, is a defect in that value and
    propagates.
    """
    try:
        return hash(_dataclass_structural_key(value._field_state()))
    except (AttributeError, RecursionError, UnhashableStructuralValueError):
        return None


def _own_content_key(value: Any) -> int | None:
    """A value's own hash, or ``None`` when the value reports itself unhashable.

    Only ``TypeError`` means "unhashable, use the equality path", exactly as the ``i not
    in seen`` membership test and a ``set`` read it. Any other error from a value's own
    ``__hash__`` -- a name it no longer carries, a descent into a cycle without end --
    is a bug in that ``__hash__``, and it propagates rather than being hidden as "no
    key". A ``memoryview`` is the one built-in that reports itself unhashable with
    ``ValueError``; those refusals are read as unhashable too, and every other
    ``ValueError`` propagates.
    """
    try:
        return hash(value)
    except TypeError:
        return None
    except ValueError as exc:
        if _is_memoryview_hash_refusal(exc):
            return None
        raise


# Deduplication keys each ``DataClass`` value by one of two hashes. A value that keeps
# the base equality is keyed by a transient hash of its declared field state, so equal
# copies share a bucket; a value whose class defines its own equality is keyed by its own
# hash. A keyed value is compared inside its key's bucket and against the kept values of
# the other kind that could not be keyed, so when every value can be keyed the work is
# linear in the values. A value that cannot be keyed is compared against the kept values
# whose own equality can decide, so for u such values the work grows by about u times the
# count of kept values. A value's id is recorded whether it was kept or dropped, so a
# repeat is never keyed or compared a second time.
def _dedupe_dataclasses(values: Iterable, out: list) -> None:
    """Append the ``DataClass`` values of *values* that are not duplicates to *out*."""
    keyed: dict[int, list[DataClass]] = {}
    base_keyless: list[DataClass] = []
    own_keyless: list[DataClass] = []
    own_all: list[DataClass] = []
    all_values: list[DataClass] = []
    processed_ids: set[int] = set()
    for value in values:
        if not isinstance(value, DataClass):
            continue
        if id(value) in processed_ids:
            continue
        processed_ids.add(id(value))
        own_equality = type(value).__eq__ is not DataClass.__eq__
        key = _own_content_key(value) if own_equality else _dataclass_content_key(value)
        if key is None:
            # A value whose state cannot be keyed: an own-equality value is compared
            # against every kept value, because its equality may join a pair the base
            # keys call different; a base-equality value is compared only against the
            # kept own-equality values, since the base equality cannot compare its own
            # unkeyable state and would raise.
            compared: Iterable[DataClass] = all_values if own_equality else own_all
        elif own_equality:
            # A keyed value with its own equality is compared inside its bucket and
            # against every kept value that could not be keyed.
            compared = chain(keyed.get(key, ()), base_keyless, own_keyless)
        else:
            # A keyed base-equality value is compared inside its bucket and against the
            # kept own-equality values that could not be keyed, because only the other
            # side's equality can join a pair the base equality calls different.
            compared = chain(keyed.get(key, ()), own_keyless)
        if any(_equal_either_way(existing, value) for existing in compared):
            continue
        if key is None:
            (own_keyless if own_equality else base_keyless).append(value)
        else:
            keyed.setdefault(key, []).append(value)
        if own_equality:
            own_all.append(value)
        all_values.append(value)
        out.append(value)


def _dedupe_operation_fields(values: Iterable) -> list:
    """Keep the first of equal operation-field values, in input order.

    A plain value is keyed by its own hash and compared only inside its bucket, so the
    work is linear in the values that can be keyed. A mapping or model value has no key
    and is compared pairwise against the kept values, because a mutable operation output
    may be opaque and its equality need not match any projection of its content, so for u
    such values the work grows by about u times the count of kept values. A value already
    processed is dropped by identity before any key is taken or any comparison runs, and
    every comparison asks both directions under one policy.
    """
    buckets: dict[int, list] = {}
    unkeyed: list = []
    processed_ids: set[int] = set()
    out: list = []
    for value in values:
        if id(value) in processed_ids:
            continue
        processed_ids.add(id(value))
        key = None if isinstance(value, _MAP_LIKE) else _own_content_key(value)
        compared: Iterable = (
            chain(buckets.get(key, ()), unkeyed)
            if key is not None
            else chain(chain.from_iterable(buckets.values()), unkeyed)
        )
        if any(_equal_either_way(existing, value) for existing in compared):
            continue
        if key is None:
            unkeyed.append(value)
        else:
            buckets.setdefault(key, []).append(value)
        out.append(value)
    return out


def to_list(
    input_: Any,
    /,
    *,
    flatten: bool = False,
    dropna: bool = False,
    unique: bool = False,
    use_values: bool = False,
    flatten_tuple_set: bool = False,
) -> list:
    """Convert any input to a list with optional flatten/dropna/unique/use_values transforms; raises ValueError if unique without flatten."""
    _lazy.ensure(_do_init)

    def _process_list(
        lst: list[Any],
        flatten: bool,
        dropna: bool,
        skip_types: tuple[type, ...],
    ) -> list[Any]:
        """Recursively process list with flatten/dropna logic."""
        result: list[Any] = []
        for item in lst:
            if dropna and (item is None or isinstance(item, _SINGLETONE_TYPES)):
                continue

            is_iterable = isinstance(item, Iterable)
            should_skip = isinstance(item, skip_types)

            if is_iterable and not should_skip:
                item_list = list(item)
                if flatten:
                    result.extend(_process_list(item_list, flatten, dropna, skip_types))
                else:
                    result.append(_process_list(item_list, flatten, dropna, skip_types))
            else:
                result.append(item)

        return result

    def _to_list_type(input_: Any, use_values: bool) -> list[Any]:
        """Convert input to initial list based on type."""
        if input_ is None or isinstance(input_, _SINGLETONE_TYPES):
            return []

        if isinstance(input_, list):
            return input_

        if isinstance(input_, type) and issubclass(input_, _Enum):
            members = input_.__members__.values()
            return [member.value for member in members] if use_values else list(members)

        if isinstance(input_, _BYTE_LIKE):
            return list(input_) if use_values else [input_]

        if isinstance(input_, Mapping):
            return list(input_.values()) if use_values and hasattr(input_, "values") else [input_]

        if isinstance(input_, _MODEL_LIKE):
            return [input_]

        if isinstance(input_, Iterable) and not isinstance(input_, _BYTE_LIKE):
            return list(input_)

        return [input_]

    if unique and not flatten:
        raise ValueError("unique=True requires flatten=True")

    initial_list = _to_list_type(input_, use_values=use_values)
    skip_types: tuple[type, ...] = _SKIP_TYPE if flatten_tuple_set else _SKIP_TUPLE_SET
    processed = _process_list(initial_list, flatten=flatten, dropna=dropna, skip_types=skip_types)

    if unique:
        dataclass_kept: list = []
        _dedupe_dataclasses(processed, dataclass_kept)
        kept_dataclasses = {id(value) for value in dataclass_kept}
        seen: set = set()
        # A mapping or model value is keyed by a structural hash of its contents, so
        # equal contents share a bucket; a value whose content key cannot be taken
        # carries no key. A keyed value is compared inside its bucket and against every
        # kept keyless value; a keyless value is compared against every kept value,
        # every bucket and the keyless list included, because nothing it carries says
        # which bucket it could equal. Values that carry a hash of their own keep the
        # native set's bucket-only comparison above.
        keyed: dict[int, list] = {}
        keyless: list = []
        processed_ids: set[int] = set()
        emitted_dataclasses: set[int] = set()
        out: list = []
        for i in processed:
            if isinstance(i, DataClass):
                if id(i) in kept_dataclasses and id(i) not in emitted_dataclasses:
                    emitted_dataclasses.add(id(i))
                    out.append(i)
                continue
            try:
                duplicate = i in seen
            except TypeError:
                # Unhashable item (e.g. a dict or a model): mirror the identity-first
                # fast path native set membership gives, over the content key.
                duplicate = None
            except ValueError as exc:
                # A memoryview refuses membership with ValueError; read it as unhashable
                # the same way, and let any other ValueError propagate.
                if not _is_memoryview_hash_refusal(exc):
                    raise
                duplicate = None
            if duplicate is False:
                seen.add(i)
                out.append(i)
                continue
            if duplicate is True or id(i) in processed_ids:
                continue
            processed_ids.add(id(i))
            if not isinstance(i, _MAP_LIKE):
                raise ValueError(
                    "Unhashable type encountered in list unique value processing."
                ) from None
            try:
                key = hash_dict(i)
            except (AttributeError, RecursionError, TypeError):
                # The contents give no key -- a cycle the content hash reports as
                # ``TypeError``, or an acyclic value deep enough to exhaust the
                # recursion limit -- so this value is compared against every kept
                # value. A serializer's ``ValueError`` is a defect in the value and
                # propagates, as it does from the structural key.
                bucket = keyless
                compared: Iterable = chain(chain.from_iterable(keyed.values()), keyless)
            else:
                bucket = keyed.setdefault(key, [])
                compared = chain(bucket, keyless)
            if any(_equal_either_way(existing, i) for existing in compared):
                continue
            bucket.append(i)
            out.append(i)
        return out

    return processed


@dataclass(slots=True, frozen=True, init=False, eq=False)
class ToListParams(Params):
    _func: ClassVar[Any] = to_list

    flatten: bool
    """If True, recursively flatten nested iterables."""
    dropna: bool
    """If True, remove None and undefined values."""
    unique: bool
    """If True, remove duplicates (requires flatten=True)."""
    use_values: bool
    """If True, extract values from enums/mappings."""
    flatten_tuple_set: bool
    """If True, include tuples and sets in flattening."""

    def __call__(self, input_: Any, **kw) -> list:
        """Convert parameters to a list."""
        kwargs = {**self.default_kw(), **kw}
        return to_list(input_, **kwargs)
