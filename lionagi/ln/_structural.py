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
import weakref
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


class _RegistryAdmissionError(TypeError):
    """A declaration payload cannot provide an immutable typed value key."""

    def __init__(self, path: str, value_type: type[Any], reason: str) -> None:
        self.path = path
        self.value_type = value_type
        self.reason = reason
        super().__init__(
            f"registry payload at {path} ({value_type.__module__}.{value_type.__qualname__}): "
            f"{reason}"
        )


# ``memoryview`` is the one built-in whose hash refusals are ``ValueError`` rather than
# ``TypeError``: for a writable view, for a view whose format is not a byte format, and
# for a released view. The messages are the interpreter's own and are stable across
# supported versions. Recognizing exactly these keeps the promised equality fallback
# for a value whose hash reaches a memoryview without swallowing any other
# ``ValueError`` a value's own ``__hash__`` raises.
_MEMORYVIEW_HASH_REFUSALS = frozenset(
    {
        "cannot hash writable memoryview object",
        "memoryview: hashing is restricted to formats 'B', 'b' or 'c'",
        "operation forbidden on released memoryview object",
    }
)
# Pydantic's serializer refuses a model that holds itself with a ``ValueError`` whose
# message starts with this text, whether it finds the repeated object or runs out of depth.
_CIRCULAR_REFERENCE = "Circular reference detected"


def _is_memoryview_hash_refusal(exc: ValueError) -> bool:
    """Whether *exc* is a ``ValueError`` a ``memoryview``'s own hash raises.

    Hashing a writable, non-byte-format or released ``memoryview`` -- on its own or
    nested in a container or model whose hash reaches it -- raises one of these. Every
    other ``ValueError`` from a value's own ``__hash__`` is a defect in that ``__hash__``
    and must propagate, so a refusal is recognized by its own message rather than by
    the exception type.
    """
    return (
        len(exc.args) == 1 and type(exc.args[0]) is str and exc.args[0] in _MEMORYVIEW_HASH_REFUSALS
    )


def _is_circular_reference(exc: ValueError) -> bool:
    """Whether *exc* is the serializer's refusal of a model that holds itself."""
    return (
        len(exc.args) == 1
        and type(exc.args[0]) is str
        and exc.args[0].startswith(_CIRCULAR_REFERENCE)
    )


def _serialized(value: Any, path: str, build: Callable[[], Any]) -> Any:
    """Return *build()*, the builtins a model serializes to, or refuse the model.

    Pydantic's ``model_dump`` refuses a model that holds itself with ``ValueError``, and
    ``msgspec.to_builtins`` reaches the recursion limit on one, before the projection can
    walk it, so the model is refused with the same error a cycle the projection meets on
    its own path raises. Any other error from the model's own serializer, a
    ``ValueError`` included, propagates.
    """
    try:
        return build()
    except RecursionError as exc:
        raise UnhashableStructuralValueError(path, type(value)) from exc
    except ValueError as exc:
        if not _is_circular_reference(exc):
            raise
        raise UnhashableStructuralValueError(path, type(value)) from exc


def _forward_collection(
    key_ref: weakref.ref[_IdentityKey], on_collect: Callable[[_IdentityKey], None]
) -> Callable[[weakref.ref], None]:
    """A weakref callback that calls ``on_collect`` with the key while the key is alive."""

    def callback(_ref: weakref.ref) -> None:
        key = key_ref()
        if key is not None:
            on_collect(key)

    return callback


class _IdentityKey:
    """Identity key that does not extend the lifetime of what it identifies.

    Immune to overloaded type/callable equality, and weak where it can be. A cached key
    only ever produces a hit for a target something is still holding and looking up, so
    keeping the target alive from inside a cache earns nothing and costs whatever the
    target retains. The hash is the id taken while the target was alive, so it stays usable
    after the target dies; equality demands both referents still be live, so an id the
    interpreter has reused cannot collide with a dead entry, and the dead entry matches
    nothing until it leaves the cache: for a key given `on_collect`, at the cache's next
    read or write after its target is collected, and for any other when the cache evicts it
    (a plain dict never does).
    A target that cannot be weakly referenced is held strongly, which keeps it and
    everything it carries alive for as long as the key lives; `holds_weakly` says which
    of the two a key is, so a caller storing one past the call can decide whether to.

    A dead entry is never hit, but until it is removed it shares a hash bucket with every
    later object allocated at the same address, and each lookup there compares against it.
    `on_collect` is called with the key when its weakly-held target is collected, while
    the key itself is still alive, so a `BoundedLRUCache` can remove the entry stored
    under it; the dataclass projection cache gives it to every key it stores. The key's
    weakref holds `on_collect` until the target is collected, so a bound method such as
    `cache.forget` would put the cache in a reference cycle with every key it stores, freed
    only by the cycle collector or once those targets are collected; the projection cache
    passes its removal queue's `append`, which does not hold the cache. A plain
    dict keyed by `_IdentityKey`, such as `_SingletonMeta._cache`, gives none and keeps
    a dead entry until it is deleted. A key nested inside a larger key, such as a tuple,
    is not the entry's key, so `on_collect` cannot remove that entry when the target is
    collected.
    """

    __slots__ = ("__weakref__", "_hash", "_ref", "_strong")

    def __init__(
        self, target: object, on_collect: Callable[[_IdentityKey], None] | None = None
    ) -> None:
        self._hash = id(target)
        # The key is referenced weakly, so the key, its reference and the callback form no
        # cycle. An `on_collect` that held the cache storing this key would form one
        # through the cache, so the dataclass projection cache passes its queue's append.
        callback = (
            None if on_collect is None else _forward_collection(weakref.ref(self), on_collect)
        )
        try:
            self._ref: weakref.ref | None = weakref.ref(target, callback)
            self._strong: object = None
        except TypeError:
            self._ref = None
            self._strong = target

    @property
    def target(self) -> object:
        """The identified object, or None once it has been collected."""
        return self._strong if self._ref is None else self._ref()

    @property
    def holds_weakly(self) -> bool:
        """Whether this key can be kept without keeping its target alive."""
        return self._ref is not None

    def __hash__(self) -> int:
        return self._hash

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _IdentityKey):
            return NotImplemented
        mine = self.target
        return mine is not None and mine is other.target

    def __repr__(self) -> str:
        target = self.target
        if target is None:
            return "_IdentityKey(<collected>)"
        module = getattr(target, "__module__", type(target).__module__)
        name = getattr(target, "__qualname__", type(target).__qualname__)
        return f"_IdentityKey({module}.{name})"


@dataclass(frozen=True, slots=True, eq=False)
class _ValueEqualityKey:
    """Hashable wrapper that always delegates comparison to the wrapped value."""

    value: Hashable

    def __hash__(self) -> int:
        return hash(self.value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, _ValueEqualityKey):
            return NotImplemented
        return bool(self.value == other.value)


@dataclass(frozen=True, slots=True, eq=False)
class _StructuralKey:
    """Detached value key plus non-semantic safety and ordering metadata."""

    value: Hashable
    cache_stable: bool = field(compare=False, hash=False, repr=False)
    hash_safe: bool = field(compare=False, hash=False, repr=False)
    _sort_token: bytes = field(compare=False, hash=False, repr=False)
    _unsafe_path: str | None = field(compare=False, hash=False, repr=False, default=None)
    _unsafe_type: type[Any] | None = field(compare=False, hash=False, repr=False, default=None)
    # Whether anything under this key is held by identity alone and is not already held
    # alive elsewhere. Such a component contributes almost nothing to the weight below
    # while retaining whatever it captured, so weight cannot price it and it is tracked
    # apart. It decides admission in `_try_stable_cache_key`: identity is not a sound
    # key for a callable no name ever reached.
    _pins: bool = field(compare=False, hash=False, repr=False, default=False)
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
_MEMORYVIEW = 25

# Keyed by identity, so a frozen dataclass's key is computed once: its fields are assumed not
# to change after that. A field written through `object.__setattr__` once the key is cached is
# not seen, and the old key is returned until newer entries evict it or its object is collected.
_stable_dataclass_keys: BoundedLRUCache[_IdentityKey, _StructuralKey] = BoundedLRUCache(
    "LIONAGI_STRUCTURAL_CACHE_SIZE",
    10000,
)
# An entry count alone does not bound bytes. Only a weakly-held key is stored, so a cached
# key never keeps its target alive, but the projected primitives are copied into the key
# and held for as long as the entry is: a projection carrying a large payload of them is
# not cached at all.
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


def _reachable_by_name(value: object) -> bool:
    # Whether the interpreter already holds this callable alive under the name it
    # advertises. __module__ and __qualname__ are both writable, so the name has to
    # resolve back to this exact object before it says anything about lifetime.
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
        _pins=any(part._pins for part in parts),
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


def _project_memoryview(
    view: memoryview, path: str, active: set[int], *, value_semantics: bool
) -> _StructuralKey:
    """Key a memoryview by the values it holds, or by identity.

    A memoryview compares equal to another view of the same shape holding equal values,
    whatever the two formats, and to the bytes holding the same values when it is
    one-dimensional. Its projection follows that comparison, as the hash representation
    in ``_hash`` does: a one-dimensional view of whole-number values from 0 to 255 is
    the ``bytes`` it holds, whatever its format, and any other view is keyed by its
    shape and by Python's own value equality over the values it unpacks to. Only a
    read-only view of a ``bytes`` object holds values that cannot change; a writable
    view, a read-only view of a mutable buffer, a released view, and a view whose
    format cannot be unpacked are keyed by identity and are unsafe to hash, as a
    ``bytearray`` is. Never cached, because releasing the view later leaves it equal
    only to itself.
    """
    try:
        values = view.tolist() if view.readonly and type(view.obj) is bytes else None
    except (ValueError, NotImplementedError):
        values = None
    if values is None:
        return _StructuralKey(
            (_OPAQUE_MUTABLE, _IdentityKey(view)),
            cache_stable=False,
            hash_safe=False,
            _sort_token=_frame(b"x", _identity_token(view)),
            _unsafe_path=path,
            _unsafe_type=memoryview,
        )
    if not _nested_lengths_match(values, view.shape):
        # The values cannot be read in the shape the view compares, so no projection
        # represents it; a key that cannot stand for an equal value is refused.
        raise UnhashableStructuralValueError(path, memoryview)
    if view.ndim == 1 and all(
        isinstance(value, int | float) and 0 <= value <= 255 and value == int(value)
        for value in values
    ):
        # The bytes key is the one a bytes value gets, which may be cached; this one never
        # is, since a released view no longer equals the bytes it held.
        return dataclasses.replace(
            _project(
                bytes(int(value) for value in values),
                path,
                active,
                value_semantics=value_semantics,
            ),
            cache_stable=False,
        )
    # The tuples are this call's own copy of the values, so they do not make the key
    # unsafe.
    projected = _project(_nested_values(values), path, active, value_semantics=True)
    return _StructuralKey(
        (_MEMORYVIEW, view.shape, projected.value),
        cache_stable=False,
        hash_safe=True,
        _sort_token=_frame(
            b"mv",
            _frame(b"t", *(_encode_int(size) for size in view.shape)),
            projected._sort_token,
        ),
        _pins=projected._pins,
    )


def _nested_lengths_match(values: Any, shape: tuple[int, ...]) -> bool:
    """Whether unpacked view values nest exactly to *shape*."""
    if not shape:
        # A zero-dimensional view unpacks to its one value.
        return not isinstance(values, list)
    if not isinstance(values, list) or len(values) != shape[0]:
        return False
    if len(shape) == 1:
        return not any(isinstance(value, list) for value in values)
    return all(_nested_lengths_match(value, shape[1:]) for value in values)


def _nested_values(values: Any) -> tuple[Any, ...]:
    """Unpacked view values as nested tuples whose equality is the view's.

    A ``?`` view compares its values as the integers they hold, so a ``bool`` becomes
    that integer, and a whole-number ``float`` equals the integer it holds, so it becomes
    that integer too; every other value is kept as it was unpacked.
    """
    if isinstance(values, list):
        return tuple(_nested_values(value) for value in values)
    if type(values) is bool or (type(values) is float and values.is_integer()):
        return (int(values),)
    return (values,)


def _project(
    value: Any,
    path: str,
    active: set[int],
    *,
    value_semantics: bool = False,
    _registry_admission: bool = False,
) -> _StructuralKey:
    value_type = type(value)

    if _registry_admission:
        typing_form = typing.get_origin(value) is not None
        enum_member = isinstance(value, Enum)
        typing_singleton = any(value is singleton for _, singleton in _TYPING_SINGLETONS)
        reason = None
        if _lion_sentinel_name(value) is not None:
            reason = "sentinel"
        elif value_type is memoryview:
            reason = "memoryview"
        elif _is_pydantic_model(value) or _is_msgspec_struct(value):
            reason = "model"
        elif value_type in (dict, list, set, bytearray):
            reason = "mutable"
        elif (
            callable(value)
            and not (typing_form or enum_member or typing_singleton)
            and not (
                isinstance(value, (type, types.FunctionType))
                or (
                    isinstance(value, types.BuiltinFunctionType)
                    and isinstance(getattr(value, "__self__", None), types.ModuleType)
                )
            )
        ):
            reason = "unstable_identity"
        elif (
            dataclasses.is_dataclass(value)
            and not isinstance(value, type)
            and not typing_form
            and not enum_member
            and (
                "__dataclass_params__" not in value_type.__dict__
                or not value_type.__dataclass_params__.frozen
            )
        ):
            reason = "mutable"
        if reason is not None:
            raise _RegistryAdmissionError(path, value_type, reason)

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
        if not value_semantics:
            encoded = struct.pack(">d", value)
            return _StructuralKey((_FLOAT, encoded), True, True, _frame(b"f", encoded))
        sort_value = 0.0 if value == 0.0 else value
        sort_token = struct.pack(">d", sort_value)
        return _StructuralKey(
            (_FLOAT, _ValueEqualityKey(value)), True, True, _frame(b"f", sort_token)
        )
    if value_type is complex:
        if not value_semantics:
            encoded = struct.pack(">dd", value.real, value.imag)
            return _StructuralKey((_COMPLEX, encoded), True, True, _frame(b"c", encoded))
        sort_value = complex(
            0.0 if value.real == 0.0 else value.real,
            0.0 if value.imag == 0.0 else value.imag,
        )
        sort_token = struct.pack(">dd", sort_value.real, sort_value.imag)
        return _StructuralKey(
            (_COMPLEX, _ValueEqualityKey(value)), True, True, _frame(b"c", sort_token)
        )
    if value_type is str:
        return _StructuralKey((_STR, value), True, True, _frame(b"s", _encode_text(value)))
    if value_type is bytes:
        return _StructuralKey((_BYTES, value), True, True, _frame(b"y", value))
    if value_type is memoryview:
        return _project_memoryview(value, path, active, value_semantics=value_semantics)

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
        identity = _enter(value, path, active, registry=True) if _registry_admission else None
        try:
            projected = _project(
                value.value,
                f"{path}.value",
                active,
                value_semantics=value_semantics,
                _registry_admission=_registry_admission,
            )
            type_key = _IdentityKey(value_type)
            return _StructuralKey(
                (_ENUM, type_key, projected.value),
                projected.cache_stable,
                projected.hash_safe,
                _frame(b"e", _identity_token(value_type), projected._sort_token),
                projected._unsafe_path,
                projected._unsafe_type,
                _pins=projected._pins,
            )
        finally:
            if identity is not None:
                active.remove(identity)

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
        identity = _enter(value, path, active, registry=True) if _registry_admission else None
        try:
            raw_arguments = typing.get_args(value)
            # typing represents Callable's parameter specification as a fresh list.
            # That list is interpreter-owned syntax, not mutable user state.
            if origin is Callable and raw_arguments and isinstance(raw_arguments[0], list):
                raw_arguments = (tuple(raw_arguments[0]), *raw_arguments[1:])
            arguments = tuple(
                _project(
                    argument,
                    f"{path}.args[{index}]",
                    active,
                    value_semantics=value_semantics,
                    _registry_admission=_registry_admission,
                )
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
        finally:
            if identity is not None:
                active.remove(identity)

    if value_type is dict or (value_semantics and isinstance(value, dict)):
        identity = _enter(value, path, active, registry=_registry_admission)
        try:
            entries: list[tuple[_StructuralKey, _StructuralKey]] = []
            for index, (key, item) in enumerate(value.items()):
                key_projection = _project(
                    key,
                    f"{path}.keys[{index}]",
                    active,
                    value_semantics=value_semantics,
                    _registry_admission=_registry_admission,
                )
                value_projection = _project(
                    item,
                    f"{path}.values[{index}]",
                    active,
                    value_semantics=value_semantics,
                    _registry_admission=_registry_admission,
                )
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
                _pins=any(part._pins for entry in ordered for part in entry),
            )
        finally:
            active.remove(identity)

    if value_type in (list, tuple) or (value_semantics and isinstance(value, list | tuple)):
        identity = _enter(value, path, active, registry=_registry_admission)
        try:
            children = tuple(
                _project(
                    item,
                    f"{path}[{index}]",
                    active,
                    value_semantics=value_semantics,
                    _registry_admission=_registry_admission,
                )
                for index, item in enumerate(value)
            )
            marker = _LIST if isinstance(value, list) else _TUPLE
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

    if value_type in (set, frozenset) or (value_semantics and isinstance(value, set | frozenset)):
        identity = _enter(value, path, active, registry=_registry_admission)
        try:
            children = _unordered(
                [
                    _project(
                        item,
                        (f"{path}[*]" if _registry_admission else f"{path}[{index}]"),
                        active,
                        value_semantics=value_semantics,
                        _registry_admission=_registry_admission,
                    )
                    for index, item in enumerate(value)
                ],
                path,
            )
            marker = _SET if isinstance(value, set) else _FROZENSET
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
        cached = (
            None
            if value_semantics or _registry_admission
            else _stable_dataclass_keys.get(identity_key)
        )
        if cached is not None:
            return cached
        identity = _enter(value, path, active, registry=_registry_admission)
        try:
            fields = dataclasses.fields(value)
            children = tuple(
                _project(
                    getattr(value, field_info.name),
                    f"{path}.{field_info.name}",
                    active,
                    value_semantics=value_semantics,
                    _registry_admission=_registry_admission,
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
                # A key that cannot be held weakly keeps this instance alive, and the
                # instance keeps its fields alive with it, so it is not stored. The types
                # this cache exists for carry a `__weakref__` slot for that reason; any
                # other declaration type is simply not cached here.
                if (
                    result.cache_stable
                    and result._weight <= _MAX_CACHED_WEIGHT
                    and identity_key.holds_weakly
                    and not value_semantics
                    and not _registry_admission
                ):
                    # The queue's own append rather than `forget`, a bound method that would
                    # hold the cache and put every stored key in a cycle with it.
                    remove_later = _stable_dataclass_keys._pending_removals.append
                    _stable_dataclass_keys.put(_IdentityKey(value, on_collect=remove_later), result)
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
        identity = _enter(value, path, active, registry=_registry_admission)
        try:
            dumped = _serialized(
                value, path, lambda: typing.cast(Any, value).model_dump(mode="python")
            )
            projected = _project(
                dumped,
                path,
                active,
                value_semantics=value_semantics,
                _registry_admission=_registry_admission,
            )
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
                _pins=projected._pins,
            )
        finally:
            active.remove(identity)

    if _is_msgspec_struct(value):
        identity = _enter(value, path, active, registry=_registry_admission)
        try:
            import msgspec

            projected = _project(
                _serialized(value, path, lambda: msgspec.to_builtins(value)),
                path,
                active,
                value_semantics=value_semantics,
                _registry_admission=_registry_admission,
            )
            return _StructuralKey(
                (_MSGSPEC, _IdentityKey(value_type), projected.value),
                projected.cache_stable,
                projected.hash_safe,
                _frame(b"q", _identity_token(value_type), projected._sort_token),
                projected._unsafe_path,
                projected._unsafe_type,
                _pins=projected._pins,
            )
        finally:
            active.remove(identity)

    if callable(value):
        # Whether identity is a sound key: it is for types and functions, and for a
        # builtin bound to a module. A callable instance is excluded because its
        # behaviour lives in mutable state that its identity says nothing about.
        cache_stable = isinstance(value, (type, types.FunctionType)) or (
            isinstance(value, types.BuiltinFunctionType)
            and isinstance(getattr(value, "__self__", None), types.ModuleType)
        )
        return _StructuralKey(
            (_CALLABLE, _IdentityKey(value)),
            cache_stable,
            True,
            _frame(b"a", _identity_token(value)),
            _pins=not _registry_admission and not _reachable_by_name(value),
        )

    if _registry_admission:
        raise _RegistryAdmissionError(path, value_type, "unsupported_type")

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

    if value_semantics:
        type_key = _IdentityKey(value_type)
        try:
            value_token = _encode_int(hash(value))
        except (TypeError, ValueError) as exc:
            # The value reports itself unhashable, so no content key exists for it and
            # the base equality that asks for one reads it as an uncomparable state
            # rather than letting the refusal escape. A TypeError is the ordinary refusal,
            # and a memoryview the value's hash reaches refuses with ValueError. Every other
            # error from the value's own __hash__ propagates.
            if isinstance(exc, ValueError) and not _is_memoryview_hash_refusal(exc):
                raise
            raise UnhashableStructuralValueError(path, value_type) from exc
        return _StructuralKey(
            (_OPAQUE, type_key, _ValueEqualityKey(value)),
            cache_stable=False,
            hash_safe=True,
            _sort_token=_frame(b"o", _identity_token(value_type), value_token),
        )
    identity = _IdentityKey(value)
    return _StructuralKey(
        (_OPAQUE, identity),
        cache_stable=False,
        hash_safe=True,
        _sort_token=_frame(b"o", _identity_token(value)),
    )


def _enter(value: Any, path: str, active: set[int], *, registry: bool = False) -> int:
    identity = id(value)
    if identity in active:
        if registry:
            raise _RegistryAdmissionError(path, type(value), "cycle")
        raise UnhashableStructuralValueError(path, type(value))
    active.add(identity)
    return identity


def _registry_structural_key(value: Any) -> _StructuralKey:
    """Freshly admit an immutable payload through the existing typed projector."""
    return _project(value, "$", set(), value_semantics=False, _registry_admission=True)


def _structural_key(value: Any) -> _StructuralKey:
    """Return a detached, type-sensitive runtime value key."""
    return _project(value, "$", set())


def _dataclass_structural_key(value: Any) -> _StructuralKey:
    """Return a field-value key that preserves Python value equality."""
    return _project(value, "$", set(), value_semantics=True)


def _try_stable_cache_key(value: Any) -> _StructuralKey | None:
    """Return a key only when retaining it cannot snapshot mutable state."""
    try:
        key = _structural_key(value)
    except UnhashableStructuralValueError:
        return None
    if not key.cache_stable or key._weight > _MAX_CACHED_WEIGHT:
        return None
    # Asked once, at admission: does a name resolve to this callable right now. A name
    # withdrawn later is outside what this can see, and refusing here would not help --
    # building the annotation or model takes its own reference to the declaration's
    # callables, whether or not the result is then cached. What this refuses is a callable
    # no name ever reached, whose identity a cache cannot key on safely.
    if key._pins:
        return None
    return key


def _structural_hash(value: Any) -> int:
    """Hash a recursively immutable structural value or fail closed."""
    return hash(_structural_key(value).require_hashable())
