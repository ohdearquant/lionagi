# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""List conversion utilities with flattening, deduplication, and NA handling."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum as _Enum
from typing import Any, ClassVar

from ._hash import hash_dict
from ._lazy_init import LazyInit
from .types import Params

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
    """Convert input to list with optional transformations.

    Type handling:
        - None/Undefined/Unset: returns []
        - list: returned as-is (not copied)
        - Enum class: list of members (or values if use_values=True)
        - str/bytes/bytearray: wrapped as [input_] unless use_values=True
        - Mapping: wrapped as [input_] unless use_values=True (extracts values)
        - BaseModel/Struct: wrapped as [input_]
        - Other iterables: converted via list()
        - Non-iterables: wrapped as [input_]

    Args:
        input_: Value to convert.
        flatten: Recursively flatten nested iterables.
        dropna: Remove None and sentinel values (Undefined, Unset).
        unique: Remove duplicates. Requires flatten=True.
        use_values: Extract values from Enum classes and Mappings.
        flatten_tuple_set: When flatten=True, also flatten tuples/sets/frozensets.

    Returns:
        Processed list.

    Raises:
        ValueError: unique=True without flatten=True, or unhashable non-mapping item.

    Edge Cases:
        - Nested lists: preserved unless flatten=True
        - Unhashable items with unique=True: falls back to hash_dict for mappings
        - Empty input: returns []
    """
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
        seen = set()
        out = []
        use_hash_fallback = False
        for i in processed:
            try:
                if not use_hash_fallback and i not in seen:
                    seen.add(i)
                    out.append(i)
            except TypeError:
                if not use_hash_fallback:
                    # Restart with hash-based deduplication
                    use_hash_fallback = True
                    seen = set()
                    out = []
                    for j in processed:
                        try:
                            hash_value = hash(j)
                        except TypeError:
                            if isinstance(j, _MAP_LIKE):
                                hash_value = hash_dict(j)
                            else:
                                raise ValueError(
                                    "Unhashable type encountered in list unique value processing."
                                ) from None
                        if hash_value not in seen:
                            seen.add(hash_value)
                            out.append(j)
                    break
        return out

    return processed


@dataclass(slots=True, frozen=True, init=False)
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
        partial = self.as_partial()
        return partial(input_, **kw)
