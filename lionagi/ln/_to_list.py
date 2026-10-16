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
from ._structural import _dataclass_structural_key
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
        seen: set = set()
        dataclass_buckets: dict[int, list[DataClass]] = {}
        unkeyed_dataclasses: set[int] = set()
        own_equality: list[DataClass] = []
        kept_dataclasses: list[DataClass] = []
        unhashable_buckets: dict[Any, list] = {}
        out: list = []
        for i in processed:
            if isinstance(i, DataClass):
                # A value that keeps DataClass equality is bucketed by a hash of its
                # structure, taken now and never kept on the instance, and compared
                # with its bucket and with every kept value that defines its own
                # equality. One whose structure cannot be keyed (a writable memoryview,
                # nesting past the recursion limit) is one DataClass equality cannot
                # compare either, so it is kept unless it is the same object as a kept
                # value or a kept value's own equality calls it equal. A value that
                # defines its own equality is compared with every kept value, so a pair
                # that equality calls equal is found in either order.
                if type(i).__eq__ is DataClass.__eq__:
                    try:
                        bucket_key = hash(_dataclass_structural_key(i._field_state()))
                    except Exception:
                        bucket_key = None
                    if bucket_key is None:
                        if id(i) in unkeyed_dataclasses or any(
                            existing == i for existing in own_equality
                        ):
                            continue
                        unkeyed_dataclasses.add(id(i))
                    else:
                        bucket = dataclass_buckets.get(bucket_key, ())
                        if any(
                            existing is i or existing == i
                            for existing in chain(bucket, own_equality)
                        ):
                            continue
                        dataclass_buckets.setdefault(bucket_key, []).append(i)
                else:
                    if any(existing is i or existing == i for existing in kept_dataclasses):
                        continue
                    own_equality.append(i)
                kept_dataclasses.append(i)
                out.append(i)
                continue
            try:
                if i not in seen:
                    seen.add(i)
                    out.append(i)
            except TypeError:
                # Unhashable item (e.g. a dict): bucket by structural hash, then
                # confirm identity-or-equality within the bucket, mirroring the
                # identity-first fast path native set membership gives above.
                if isinstance(i, _MAP_LIKE):
                    hash_value = hash_dict(i)
                else:
                    raise ValueError(
                        "Unhashable type encountered in list unique value processing."
                    ) from None
                bucket = unhashable_buckets.get(hash_value)
                if bucket is None:
                    unhashable_buckets[hash_value] = [i]
                    out.append(i)
                elif not any(existing is i or existing == i for existing in bucket):
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
