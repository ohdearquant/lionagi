# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

from lionagi.utils import UNDEFINED


def get_target_container(
    data: list[Any] | dict[Any, Any],
    indices: list[int | str],
) -> list[Any] | dict[Any, Any]:
    """Walk data to the container at indices path."""
    current = data
    for index in indices:
        if isinstance(current, list):
            if isinstance(index, str) and index.isdigit():
                index = int(index)
            if isinstance(index, int) and 0 <= index < len(current):
                current = current[index]
            else:
                raise IndexError("List index is invalid or out of range")
        elif isinstance(current, dict):
            if index in current:
                current = current[index]
            else:
                raise KeyError(f"Key not found in dictionary: {index!r}")
        else:
            raise TypeError("Current element is neither a list nor a dict")
    return current


def _ensure_list_index(lst: list[Any], index: int) -> None:
    """Extend list with None values until index is valid."""
    while len(lst) <= index:
        lst.append(None)


def nget(
    data: dict[Any, Any] | list[Any],
    indices: list[int | str],
    default: Any = UNDEFINED,
) -> Any:
    """Safe nested get; walk data using indices, return default if path missing."""
    if not indices:
        raise ValueError("indices must not be empty")
    try:
        container = get_target_container(data, indices[:-1])
        last = indices[-1]
        if isinstance(container, list):
            if isinstance(last, str) and last.isdigit():
                last = int(last)
            if isinstance(last, int) and 0 <= last < len(container):
                return container[last]
        elif isinstance(container, dict) and last in container:
            return container[last]
        if default is not UNDEFINED:
            return default
        raise KeyError(f"Path not found: {indices}")
    except (IndexError, KeyError, TypeError) as exc:
        if default is not UNDEFINED:
            return default
        raise KeyError(f"Path not found: {indices}") from exc


def nset(
    data: dict[str, Any] | list[Any],
    indices: list[str | int],
    value: Any,
) -> None:
    """Nested set; auto-creates intermediate dicts/lists based on next index type."""
    if not indices:
        raise ValueError("indices must not be empty")
    current = data
    for i, index in enumerate(indices[:-1]):
        next_index = indices[i + 1]
        if isinstance(current, list):
            if not isinstance(index, int):
                raise TypeError("Cannot use non-integer index on a list")
            _ensure_list_index(current, index)
            if current[index] is None:
                current[index] = [] if isinstance(next_index, int) else {}
        elif isinstance(current, dict):
            if isinstance(index, int):
                raise TypeError("Cannot use integer key on a dict")
            if index not in current:
                current[index] = [] if isinstance(next_index, int) else {}
        else:
            raise TypeError("Target container is not a list or dict")
        current = current[index]
    last = indices[-1]
    if isinstance(current, list):
        if not isinstance(last, int):
            raise TypeError("Cannot use non-integer index on a list")
        _ensure_list_index(current, last)
        current[last] = value
    elif isinstance(current, dict):
        if not isinstance(last, str):
            raise TypeError("Only string keys are allowed for dicts")
        current[last] = value
    else:
        raise TypeError("Cannot set value on non-list/dict element")


def npop(
    data: dict[str, Any] | list[Any],
    indices: list[str | int],
    default: Any = UNDEFINED,
) -> Any:
    """Nested pop; walk to parent, pop final key."""
    if not indices:
        raise ValueError("indices must not be empty")
    current = data
    for key in indices[:-1]:
        if isinstance(current, dict):
            if key not in current:
                if default is not UNDEFINED:
                    return default
                raise KeyError(f"Key not found: {key!r}")
            current = current[key]
        elif isinstance(current, list):
            if isinstance(key, str) and key.isdigit():
                key = int(key)
            if isinstance(key, int) and 0 <= key < len(current):
                current = current[key]
            else:
                if default is not UNDEFINED:
                    return default
                raise IndexError(f"List index out of range: {key}")
        else:
            if default is not UNDEFINED:
                return default
            raise TypeError(f"Cannot traverse into {type(current).__name__}")
    last = indices[-1]
    try:
        if isinstance(current, dict):
            if default is not UNDEFINED:
                return current.pop(last, default)
            return current.pop(last)
        elif isinstance(current, list):
            if isinstance(last, str) and last.isdigit():
                last = int(last)
            if not isinstance(last, int):
                raise TypeError("Cannot use non-integer index on a list")
            if last < 0 or last >= len(current):
                if default is not UNDEFINED:
                    return default
                raise IndexError(f"List index out of range: {last}")
            return current.pop(last)
        else:
            raise TypeError(f"Cannot pop from {type(current).__name__}")
    except (KeyError, IndexError):
        if default is not UNDEFINED:
            return default
        raise


def flatten(
    data: Any,
    *,
    sep: str = "|",
    max_depth: int | None = None,
) -> dict[str, Any]:
    """Flatten nested dict/list to flat dict with sep-joined path keys."""
    result: dict[str, Any] = {}
    stack: deque[tuple[Any, tuple[str, ...], int]] = deque([(data, (), 0)])
    while stack:
        obj, parent_key, depth = stack.pop()
        if max_depth is not None and depth >= max_depth:
            key_str = sep.join(parent_key)
            result[key_str] = obj
            continue
        if isinstance(obj, Mapping):
            for k, v in obj.items():
                new_key = parent_key + (str(k),)
                if isinstance(v, (Mapping, Sequence)) and not isinstance(
                    v, (str, bytes, bytearray)
                ):
                    stack.appendleft((v, new_key, depth + 1))
                else:
                    result[sep.join(new_key)] = v
        elif isinstance(obj, Sequence) and not isinstance(obj, (str, bytes, bytearray)):
            for i, v in enumerate(obj):
                new_key = parent_key + (str(i),)
                if isinstance(v, (Mapping, Sequence)) and not isinstance(
                    v, (str, bytes, bytearray)
                ):
                    stack.appendleft((v, new_key, depth + 1))
                else:
                    result[sep.join(new_key)] = v
        else:
            key_str = sep.join(parent_key) if parent_key else ""
            result[key_str] = obj
    return result


def unflatten(data: dict[str, Any], sep: str = "|") -> dict[str, Any]:
    """Reverse flatten; auto-detects list keys (consecutive integer strings)."""

    def _convert(d: dict) -> dict | list:
        if d and all(k.isdigit() for k in d):
            if all(str(i) in d for i in range(len(d))):
                return [
                    _convert(d[str(i)]) if isinstance(d[str(i)], dict) else d[str(i)]
                    for i in range(len(d))
                ]
            return {k: _convert(v) if isinstance(v, dict) else v for k, v in d.items()}
        return {k: _convert(v) if isinstance(v, dict) else v for k, v in d.items()}

    intermediate: dict[str, Any] = {}
    for key, value in data.items():
        parts = key.split(sep)
        current = intermediate
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]
        current[parts[-1]] = value
    return _convert(intermediate)


def deep_update(original: dict[Any, Any], update: dict[Any, Any]) -> dict[Any, Any]:
    """Recursive dict merge; nested dicts merge, other values overwrite."""
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(original.get(key), dict):
            original[key] = deep_update(original[key], value)
        else:
            original[key] = value
    return original


def deep_merge(base: dict, override: dict, *, mutate: bool = False) -> dict:
    """Recursively merge override into base.

    When mutate=True (default for settings), dicts recurse in-place and lists
    are concatenated.  When mutate=False, a new dict is returned and lists are
    overwritten by the override value.
    """
    if mutate:
        for k, v in override.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                deep_merge(base[k], v, mutate=True)
            elif mutate and k in base and isinstance(base[k], list) and isinstance(v, list):
                base[k] = base[k] + v
            else:
                base[k] = v
        return base
    merged = dict(base)
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = deep_merge(merged[k], v, mutate=False)
        else:
            merged[k] = v
    return merged


__all__ = [
    "get_target_container",
    "nget",
    "nset",
    "npop",
    "flatten",
    "unflatten",
    "deep_update",
    "deep_merge",
]
