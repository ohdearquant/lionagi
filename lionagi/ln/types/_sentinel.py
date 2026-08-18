from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
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
_CollapseAxis = Literal["none", "empty"]

# ADR-0119 compatibility debt. This is an architectural/CI boundary, not a
# security boundary: repository callers are tied to these lexical owners by a
# static contract test, while the private gateway deliberately avoids fragile
# frame inspection.
LEGACY_SENTINEL_COLLAPSE_ALLOWLIST: Final[frozenset[tuple[str, _CollapseAxis]]] = frozenset(
    {
        ("lionagi.casts.pattern.Pattern._config", "none"),
        ("lionagi.casts.pattern.Pattern._config", "empty"),
        ("lionagi.casts.pattern.Role.emission_operable", "none"),
        ("lionagi.casts.pattern.Role.emission_operable", "empty"),
        ("lionagi.ln._async_call.AlcallParams._config", "none"),
        ("lionagi.models.field_model.FieldModel._config", "none"),
        ("lionagi.models.note._strip_sentinels", "none"),
        ("lionagi.models.note._strip_sentinels", "empty"),
        ("lionagi.operations.fields.Instruct.handle", "none"),
        ("lionagi.operations.fields.Instruct.handle", "empty"),
        ("lionagi.operations.types.MorphParam._config", "none"),
        ("lionagi.protocols.messages.instruction.InstructionContent._config", "none"),
        ("lionagi.protocols.messages.instruction.InstructionContent._config", "empty"),
        ("lionagi.protocols.messages.message.MessageContent._config", "none"),
        ("lionagi.ln.types.spec.Spec.__init__", "none"),
        ("lionagi.ln.types.spec.Spec.annotation", "none"),
        ("lionagi.ln.types.spec.Spec.annotated", "none"),
    }
)


def _identity_is_sentinel(value: Any) -> bool:
    return value is Undefined or value is Unset


@dataclass(frozen=True, slots=True)
class _SentinelPolicy:
    none_as_sentinel: bool
    empty_as_sentinel: bool

    def is_sentinel(self, value: Any) -> bool:
        if self.none_as_sentinel and value is None:
            return True
        if self.empty_as_sentinel and value in _EMPTY_TUPLE:
            return True
        return _identity_is_sentinel(value)


@lru_cache(maxsize=64)
def _compat_policy(
    *,
    site: str,
    none_as_sentinel: bool = False,
    empty_as_sentinel: bool = False,
) -> _SentinelPolicy:
    """Validate and compile one named legacy collapse policy."""
    if none_as_sentinel and (site, "none") not in LEGACY_SENTINEL_COLLAPSE_ALLOWLIST:
        raise ValueError(f"Legacy sentinel collapse axis 'none' is not allowlisted for {site!r}")
    if empty_as_sentinel and (site, "empty") not in LEGACY_SENTINEL_COLLAPSE_ALLOWLIST:
        raise ValueError(f"Legacy sentinel collapse axis 'empty' is not allowlisted for {site!r}")
    return _SentinelPolicy(
        none_as_sentinel=none_as_sentinel,
        empty_as_sentinel=empty_as_sentinel,
    )


def _compat_is_sentinel(
    value: Any,
    *,
    site: str,
    none_as_sentinel: bool = False,
    empty_as_sentinel: bool = False,
) -> bool:
    """Apply a named legacy collapse contract after validating each axis."""
    return _compat_policy(
        site=site,
        none_as_sentinel=none_as_sentinel,
        empty_as_sentinel=empty_as_sentinel,
    ).is_sentinel(value)


def _compat_not_sentinel(
    value: Any,
    *,
    site: str,
    none_as_sentinel: bool = False,
    empty_as_sentinel: bool = False,
) -> bool:
    return not _compat_is_sentinel(
        value,
        site=site,
        none_as_sentinel=none_as_sentinel,
        empty_as_sentinel=empty_as_sentinel,
    )


def is_sentinel(
    value: Any,
    *,
    none_as_sentinel: bool = False,
    empty_as_sentinel: bool = False,
) -> bool:
    """Check sentinel identity; legacy value collapse is internal-only."""
    if none_as_sentinel or empty_as_sentinel:
        raise ValueError(
            "Legacy None/empty sentinel collapse is restricted to allowlisted "
            "internal compatibility sites"
        )
    return value is Undefined or value is Unset


def not_sentinel(
    value: Any, none_as_sentinel: bool = False, empty_as_sentinel: bool = False
) -> bool:
    """Check sentinel non-identity; legacy value collapse is internal-only."""
    if none_as_sentinel or empty_as_sentinel:
        raise ValueError(
            "Legacy None/empty sentinel collapse is restricted to allowlisted "
            "internal compatibility sites"
        )
    return value is not Undefined and value is not Unset
