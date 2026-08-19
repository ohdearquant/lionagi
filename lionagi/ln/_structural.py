# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Private runtime structural keys for equality, hashing, and safe caches."""

from __future__ import annotations

import dataclasses
import os
import struct
import sys
import types
import typing
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import PosixPath, PurePosixPath, PureWindowsPath, WindowsPath
from typing import Any
from uuid import UUID

from ._cache import BoundedLRUCache

__all__ = ("UnhashableStructuralValueError",)


class UnhashableStructuralValueError(TypeError):
    """A live value graph is structurally comparable but unsafe to hash."""

    __slots__ = ("path", "value_type")

    def __init__(self, path: str, value_type: type[Any]) -> None:
        self.path = path
        self.value_type = value_type
        super().__init__(
            "mutable structural value at "
            f"{path} ({value_type.__module__}.{value_type.__qualname__}) "
            "cannot participate in a runtime hash"
        )


@dataclass(frozen=True, slots=True, eq=False)
class _IdentityKey:
    """Strong identity key immune to overloaded type/callable equality."""

    target: object = field(repr=False)

    def __hash__(self) -> int:
        return id(self.target)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _IdentityKey) and self.target is other.target

    def __repr__(self) -> str:
        module = getattr(self.target, "__module__", type(self.target).__module__)
        name = getattr(self.target, "__qualname__", type(self.target).__qualname__)
        return f"_IdentityKey({module}.{name})"


@dataclass(frozen=True, slots=True, eq=False)
class _StructuralKey:
    """Detached value key plus non-semantic safety and ordering metadata."""

    value: Hashable
    cache_stable: bool = field(compare=False, hash=False, repr=False)
    hash_safe: bool = field(compare=False, hash=False, repr=False)
    _sort_token: bytes = field(compare=False, hash=False, repr=False)
    _unsafe_path: str | None = field(compare=False, hash=False, repr=False, default=None)
    _unsafe_type: type[Any] | None = field(compare=False, hash=False, repr=False, default=None)
    _weight: int = field(init=False, compare=False, hash=False, repr=False)
    _hash_value: int = field(init=False, compare=False, hash=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_hash_value", hash(self.value))
        # The ordering token already frames every descendant's token, so its length
        # is the one retained-cost estimate no branch can forget to supply.
        object.__setattr__(self, "_weight", len(self._sort_token))

    def __hash__(self) -> int:
        return self._hash_value

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _StructuralKey) and self.value == other.value

    def require_hashable(self) -> _StructuralKey:
        if not self.hash_safe:
            raise UnhashableStructuralValueError(
                self._unsafe_path or "$",
                self._unsafe_type or object,
            )
        return self


_NONE = 0
_BOOL = 1
_INT = 2
_FLOAT = 3
_COMPLEX = 4
_STR = 5
_BYTES = 6
_ENUM = 7
_TYPING = 8
_MAPPING = 9
_LIST = 10
_TUPLE = 11
_SET = 12
_FROZENSET = 13
_DATACLASS = 14
_PYDANTIC = 15
_MSGSPEC = 16
_CALLABLE = 17
_OPAQUE = 18
_OPAQUE_MUTABLE = 19
_SENTINEL = 20
_PATH = 21
_UUID = 22
_ELLIPSIS = 23
_TYPING_SINGLETON = 24

_stable_dataclass_keys: BoundedLRUCache[_IdentityKey, _StructuralKey] = BoundedLRUCache(
    "LIONAGI_STRUCTURAL_CACHE_SIZE",
    10000,
)
# An entry count alone does not bound bytes: one cached key strongly retains its
# whole target, so a projection that carries a large payload is not cached at all.
_MAX_CACHED_WEIGHT = int(os.environ.get("LIONAGI_STRUCTURAL_CACHE_VALUE_LIMIT", "8192"))
_PATH_TYPES = (PurePosixPath, PureWindowsPath, PosixPath, WindowsPath)
_TYPING_SINGLETONS = tuple(
    (name, singleton)
    for name in ("Any", "NoReturn", "Never", "Self", "LiteralString")
    if (singleton := getattr(typing, name, None)) is not None
)


def _encode_text(value: str) -> bytes:
    return value.encode("utf-8", "surrogatepass")


def _encode_int(value: int) -> bytes:
    """Width-minimal two's complement, which unlike str() has no digit ceiling."""
    return value.to_bytes((value.bit_length() + 8) // 8, "big", signed=True)


def _lion_sentinel_name(value: object) -> str | None:
    """Resolve the two Lion sentinels by object identity without an import cycle."""
    module = sys.modules.get("lionagi.ln.types._sentinel")
    if module is None:
        return None
    for name in ("Undefined", "Unset"):
        if value is getattr(module, name, None):
            return name
    return None


def _frame(tag: bytes, *parts: bytes) -> bytes:
    framed = bytearray(tag)
    for part in parts:
        framed.extend(len(part).to_bytes(8, "big"))
        framed.extend(part)
    return bytes(framed)


def _identity_token(value: object) -> bytes:
    # Presentation attributes such as __module__/__qualname__ are writable on
    # functions and classes. Ordering for identity-semantic values must depend
    # only on the same immutable identity that equality uses.
    return _frame(b"r", _encode_text(str(id(value))))


def _held_by_its_module(value: object) -> bool:
    # A callable's token is its identity, so its length says nothing about what the
    # callable retains. Caching one the interpreter already holds alive by name costs
    # no extra memory; caching a closure or a type() result retains whatever it carries.
    module = sys.modules.get(getattr(value, "__module__", "") or "")
    qualname = getattr(value, "__qualname__", "") or ""
    if module is None or not qualname or "<locals>" in qualname:
        return False
    target: object = module
    for part in qualname.split("."):
        target = getattr(target, part, None)
        if target is None:
            return False
    return target is value


def _combine(
    marker: int,
    parts: tuple[_StructuralKey, ...],
    token_tag: bytes,
    *,
    prefix: tuple[Hashable, ...] = (),
) -> _StructuralKey:
    unsafe = next((part for part in parts if not part.hash_safe), None)
    return _StructuralKey(
        (marker, *prefix, tuple(part.value for part in parts)),
        cache_stable=all(part.cache_stable for part in parts),
        hash_safe=unsafe is None,
        _sort_token=_frame(token_tag, *(part._sort_token for part in parts)),
        _unsafe_path=unsafe._unsafe_path if unsafe else None,
        _unsafe_type=unsafe._unsafe_type if unsafe else None,
    )


def _unordered(parts: list[_StructuralKey], path: str) -> tuple[_StructuralKey, ...]:
    parts.sort(key=lambda part: part._sort_token)
    for left, right in zip(parts, parts[1:], strict=False):
        if left._sort_token != right._sort_token:
            continue
        try:
            equal = bool(left == right)
        except Exception as exc:
            raise UnhashableStructuralValueError(path, object) from exc
        if not equal:
            raise UnhashableStructuralValueError(path, object)
    return tuple(parts)


def _ordered_mapping_entries(
    entries: list[tuple[_StructuralKey, _StructuralKey]],
    path: str,
) -> tuple[tuple[_StructuralKey, _StructuralKey], ...]:
    entries.sort(key=lambda entry: (entry[0]._sort_token, entry[1]._sort_token))
    for left, right in zip(entries, entries[1:], strict=False):
        left_token = (left[0]._sort_token, left[1]._sort_token)
        right_token = (right[0]._sort_token, right[1]._sort_token)
        if left_token != right_token:
            continue
        try:
            equal = left[0] == right[0] and left[1] == right[1]
        except Exception as exc:
            raise UnhashableStructuralValueError(path, object) from exc
        if not equal:
            raise UnhashableStructuralValueError(path, object)
    return tuple(entries)


def _is_pydantic_model(value: Any) -> bool:
    return any(
        base.__module__.startswith("pydantic") and base.__name__ == "BaseModel"
        for base in type(value).__mro__
    )


def _is_msgspec_struct(value: Any) -> bool:
    return any(
        base.__module__ == "msgspec" and base.__name__ == "Struct" for base in type(value).__mro__
    )


def _project(value: Any, path: str, active: set[int]) -> _StructuralKey:
    value_type = type(value)

    if value is None:
        return _StructuralKey((_NONE,), True, True, b"n")
    typing_singleton = next(
        (name for name, singleton in _TYPING_SINGLETONS if value is singleton),
        None,
    )
    if typing_singleton is not None:
        # Public typing singleton runtime representations changed across
        # supported Python versions; their cache semantics must not.
        return _StructuralKey(
            (_TYPING_SINGLETON, typing_singleton),
            True,
            True,
            _frame(b"k", _encode_text(typing_singleton)),
        )
    if value is Ellipsis:
        return _StructuralKey((_ELLIPSIS,), True, True, b"z")
    if value_type is bool:
        return _StructuralKey((_BOOL, value), True, True, b"b1" if value else b"b0")
    if value_type is int:
        return _StructuralKey((_INT, value), True, True, _frame(b"i", _encode_int(value)))
    if value_type is float:
        encoded = struct.pack(">d", value)
        return _StructuralKey((_FLOAT, encoded), True, True, _frame(b"f", encoded))
    if value_type is complex:
        encoded = struct.pack(">dd", value.real, value.imag)
        return _StructuralKey((_COMPLEX, encoded), True, True, _frame(b"c", encoded))
    if value_type is str:
        return _StructuralKey((_STR, value), True, True, _frame(b"s", _encode_text(value)))
    if value_type is bytes:
        return _StructuralKey((_BYTES, value), True, True, _frame(b"y", value))

    sentinel_name = _lion_sentinel_name(value)
    if sentinel_name is not None:
        identity = _IdentityKey(value)
        return _StructuralKey(
            (_SENTINEL, identity),
            True,
            True,
            _frame(b"w", _encode_text(sentinel_name)),
        )

    if isinstance(value, Enum):
        projected = _project(value.value, f"{path}.value", active)
        type_key = _IdentityKey(value_type)
        return _StructuralKey(
            (_ENUM, type_key, projected.value),
            projected.cache_stable,
            projected.hash_safe,
            _frame(b"e", _identity_token(value_type), projected._sort_token),
            projected._unsafe_path,
            projected._unsafe_type,
        )

    if any(value_type is path_type for path_type in _PATH_TYPES):
        # pathlib paths are immutable value objects.  Preserve pathlib's
        # case-insensitive Windows equality while keeping concrete path types
        # distinct (PurePath vs Path, POSIX vs Windows).
        parts = (
            tuple(part.lower() for part in value.parts)
            if isinstance(value, PureWindowsPath)
            else value.parts
        )
        type_key = _IdentityKey(value_type)
        encoded_parts = tuple(_encode_text(part) for part in parts)
        return _StructuralKey(
            (_PATH, type_key, parts),
            cache_stable=True,
            hash_safe=True,
            _sort_token=_frame(
                b"h",
                _identity_token(value_type),
                *encoded_parts,
            ),
        )

    if value_type is UUID:
        type_key = _IdentityKey(value_type)
        return _StructuralKey(
            (_UUID, type_key, value.bytes),
            cache_stable=True,
            hash_safe=True,
            _sort_token=_frame(b"j", _identity_token(value_type), value.bytes),
        )

    origin = typing.get_origin(value)
    if origin is not None:
        raw_arguments = typing.get_args(value)
        # typing represents Callable's parameter specification as a fresh list.
        # That list is interpreter-owned syntax, not mutable user state.
        if origin is Callable and raw_arguments and isinstance(raw_arguments[0], list):
            raw_arguments = (tuple(raw_arguments[0]), *raw_arguments[1:])
        arguments = tuple(
            _project(argument, f"{path}.args[{index}]", active)
            for index, argument in enumerate(raw_arguments)
        )
        combined = _combine(
            _TYPING,
            arguments,
            b"g",
            prefix=(_IdentityKey(value_type), _IdentityKey(origin)),
        )
        return dataclasses.replace(
            combined,
            _sort_token=_frame(
                b"g",
                _identity_token(value_type),
                _identity_token(origin),
                combined._sort_token,
            ),
        )

    if value_type is dict:
        identity = _enter(value, path, active)
        try:
            entries: list[tuple[_StructuralKey, _StructuralKey]] = []
            for index, (key, item) in enumerate(value.items()):
                key_projection = _project(key, f"{path}.keys[{index}]", active)
                value_projection = _project(item, f"{path}.values[{index}]", active)
                entries.append((key_projection, value_projection))
            ordered = _ordered_mapping_entries(entries, path)
            unsafe = next(
                (part for entry in ordered for part in entry if not part.hash_safe),
                None,
            )
            token = _frame(
                b"m",
                *(_frame(b"p", key._sort_token, item._sort_token) for key, item in ordered),
            )
            return _StructuralKey(
                (_MAPPING, tuple((key.value, item.value) for key, item in ordered)),
                cache_stable=False,
                hash_safe=False,
                _sort_token=token,
                _unsafe_path=(unsafe._unsafe_path if unsafe else path),
                _unsafe_type=(unsafe._unsafe_type if unsafe else value_type),
            )
        finally:
            active.remove(identity)

    if value_type is list or value_type is tuple:
        identity = _enter(value, path, active)
        try:
            children = tuple(
                _project(item, f"{path}[{index}]", active) for index, item in enumerate(value)
            )
            marker = _LIST if value_type is list else _TUPLE
            combined = _combine(marker, children, b"l" if marker == _LIST else b"t")
            if marker == _LIST:
                return dataclasses.replace(
                    combined,
                    cache_stable=False,
                    hash_safe=False,
                    _unsafe_path=combined._unsafe_path or path,
                    _unsafe_type=combined._unsafe_type or value_type,
                )
            return combined
        finally:
            active.remove(identity)

    if value_type is set or value_type is frozenset:
        identity = _enter(value, path, active)
        try:
            children = _unordered(
                [_project(item, f"{path}[{index}]", active) for index, item in enumerate(value)],
                path,
            )
            marker = _SET if value_type is set else _FROZENSET
            combined = _combine(marker, children, b"u" if marker == _SET else b"v")
            if marker == _SET:
                return dataclasses.replace(
                    combined,
                    cache_stable=False,
                    hash_safe=False,
                    _unsafe_path=combined._unsafe_path or path,
                    _unsafe_type=combined._unsafe_type or value_type,
                )
            return combined
        finally:
            active.remove(identity)

    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        identity_key = _IdentityKey(value)
        cached = _stable_dataclass_keys.get(identity_key)
        if cached is not None:
            return cached
        identity = _enter(value, path, active)
        try:
            fields = dataclasses.fields(value)
            children = tuple(
                _project(
                    getattr(value, field_info.name),
                    f"{path}.{field_info.name}",
                    active,
                )
                for field_info in fields
            )
            type_key = _IdentityKey(value_type)
            combined = _combine(
                _DATACLASS,
                children,
                b"d",
                prefix=(type_key, tuple(field_info.name for field_info in fields)),
            )
            frozen = bool(value_type.__dataclass_params__.frozen)
            if frozen:
                result = dataclasses.replace(
                    combined,
                    _sort_token=_frame(
                        b"d",
                        _identity_token(value_type),
                        combined._sort_token,
                    ),
                )
                if result.cache_stable and result._weight <= _MAX_CACHED_WEIGHT:
                    _stable_dataclass_keys.put(identity_key, result)
                return result
            return dataclasses.replace(
                combined,
                cache_stable=False,
                hash_safe=False,
                _sort_token=_frame(b"d", _identity_token(value_type), combined._sort_token),
                _unsafe_path=combined._unsafe_path or path,
                _unsafe_type=combined._unsafe_type or value_type,
            )
        finally:
            active.remove(identity)

    if _is_pydantic_model(value):
        identity = _enter(value, path, active)
        try:
            dumped = typing.cast(Any, value).model_dump(mode="python")
            projected = _project(dumped, path, active)
            return _StructuralKey(
                (_PYDANTIC, _IdentityKey(value_type), projected.value),
                cache_stable=False,
                hash_safe=False,
                _sort_token=_frame(
                    b"p",
                    _identity_token(value_type),
                    projected._sort_token,
                ),
                _unsafe_path=projected._unsafe_path or path,
                _unsafe_type=projected._unsafe_type or value_type,
            )
        finally:
            active.remove(identity)

    if _is_msgspec_struct(value):
        identity = _enter(value, path, active)
        try:
            import msgspec

            projected = _project(msgspec.to_builtins(value), path, active)
            return _StructuralKey(
                (_MSGSPEC, _IdentityKey(value_type), projected.value),
                projected.cache_stable,
                projected.hash_safe,
                _frame(b"q", _identity_token(value_type), projected._sort_token),
                projected._unsafe_path,
                projected._unsafe_type,
            )
        finally:
            active.remove(identity)

    if callable(value):
        cache_stable = (
            isinstance(value, (type, types.FunctionType))
            or (
                isinstance(value, types.BuiltinFunctionType)
                and isinstance(getattr(value, "__self__", None), types.ModuleType)
            )
        ) and _held_by_its_module(value)
        identity = _IdentityKey(value)
        return _StructuralKey(
            (_CALLABLE, identity),
            cache_stable,
            True,
            _frame(b"a", _identity_token(value)),
        )

    if not isinstance(value, Hashable):
        identity = _IdentityKey(value)
        return _StructuralKey(
            (_OPAQUE_MUTABLE, identity),
            cache_stable=False,
            hash_safe=False,
            _sort_token=_frame(b"x", _identity_token(value)),
            _unsafe_path=path,
            _unsafe_type=value_type,
        )

    identity = _IdentityKey(value)
    return _StructuralKey(
        (_OPAQUE, identity),
        cache_stable=False,
        hash_safe=True,
        _sort_token=_frame(b"o", _identity_token(value)),
    )


def _enter(value: Any, path: str, active: set[int]) -> int:
    identity = id(value)
    if identity in active:
        raise UnhashableStructuralValueError(path, type(value))
    active.add(identity)
    return identity


def _structural_key(value: Any) -> _StructuralKey:
    """Return a detached, type-sensitive runtime value key."""
    return _project(value, "$", set())


def _try_stable_cache_key(value: Any) -> _StructuralKey | None:
    """Return a key only when retaining it cannot snapshot mutable state."""
    try:
        key = _structural_key(value)
    except UnhashableStructuralValueError:
        return None
    if not key.cache_stable or key._weight > _MAX_CACHED_WEIGHT:
        return None
    return key


def _structural_hash(value: Any) -> int:
    """Hash a recursively immutable structural value or fail closed."""
    return hash(_structural_key(value).require_hashable())
