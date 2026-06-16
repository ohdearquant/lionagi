from __future__ import annotations

from typing import Any, Final, Literal, TypeVar, Union

__all__ = (
    "Undefined",
    "Unset",
    "MaybeUndefined",
    "MaybeUnset",
    "MaybeSentinel",
    "SingletonType",
    "UndefinedType",
    "UnsetType",
    "is_sentinel",
    "not_sentinel",
    "T",
)

T = TypeVar("T")


class _SingletonMeta(type):
    """Metaclass that guarantees exactly one instance per subclass."""

    _cache: dict[type, SingletonType] = {}

    def __call__(cls, *a, **kw):
        if cls not in cls._cache:
            cls._cache[cls] = super().__call__(*a, **kw)
        return cls._cache[cls]


class SingletonType(metaclass=_SingletonMeta):
    """Base class for singleton sentinels; identity preserved across copy/deepcopy."""

    __slots__: tuple[str, ...] = ()

    def __deepcopy__(self, memo):  # copy & deepcopy both noop
        return self

    def __copy__(self):
        return self

    # concrete classes *must* override the two methods below
    def __bool__(self) -> bool: ...
    def __repr__(self) -> str: ...


class UndefinedType(SingletonType):
    """Sentinel for a key or field entirely absent from a namespace; falsy, identity-preserving."""

    __slots__ = ()

    def __bool__(self) -> Literal[False]:
        return False

    def __repr__(self) -> Literal["Undefined"]:
        return "Undefined"

    def __str__(self) -> Literal["Undefined"]:
        return "Undefined"

    def __reduce__(self):
        return "Undefined"


class UnsetType(SingletonType):
    """Sentinel for a parameter present but not yet assigned a value; distinct from None."""

    __slots__ = ()

    def __bool__(self) -> Literal[False]:
        return False

    def __repr__(self) -> Literal["Unset"]:
        return "Unset"

    def __str__(self) -> Literal["Unset"]:
        return "Unset"

    def __reduce__(self):
        return "Unset"


Undefined: Final = UndefinedType()
"""A key or field entirely missing from a namespace"""
Unset: Final = UnsetType()
"""A key present but value not yet provided."""

MaybeUndefined = Union[T, UndefinedType]
MaybeUnset = Union[T, UnsetType]
MaybeSentinel = Union[T, UndefinedType, UnsetType]

_EMPTY_TUPLE = (tuple(), set(), frozenset(), dict(), list(), "")


def is_sentinel(
    value: Any,
    *,
    none_as_sentinel: bool = False,
    empty_as_sentinel: bool = False,
) -> bool:
    """Check if a value is any sentinel (Undefined or Unset)."""
    if none_as_sentinel and value is None:
        return True
    if empty_as_sentinel and value in _EMPTY_TUPLE:
        return True
    return value is Undefined or value is Unset


def not_sentinel(
    value: Any, none_as_sentinel: bool = False, empty_as_sentinel: bool = False
) -> bool:
    """Check if a value is NOT a sentinel. Useful for filtering operations."""
    return not is_sentinel(
        value,
        none_as_sentinel=none_as_sentinel,
        empty_as_sentinel=empty_as_sentinel,
    )
