"""Composable payload predicates: TypeFilter (isinstance match) and SpecFilter (field-value match via Spec.q DSL)."""

from __future__ import annotations

import logging
import operator
from collections.abc import Callable
from typing import Any

__all__ = (
    "Filter",
    "TypeFilter",
    "SpecFilter",
    "FieldRef",
    "RoleFilter",
    "as_filter",
    "all_of",
    "resolve_path",
)

logger = logging.getLogger(__name__)


def field_values(payload: Any) -> dict[str, Any]:
    """Field name → value for a payload (Pydantic model or dict); else empty."""
    model_fields = getattr(type(payload), "model_fields", None)
    if model_fields:
        return {name: getattr(payload, name, None) for name in model_fields}
    if isinstance(payload, dict):
        return payload
    return {}


_MISSING = object()


def resolve_path(payload: Any, dotted: str) -> Any:
    """Walk dotted attribute/item path on payload; return ``_MISSING`` if any segment is absent."""
    cur: Any = payload
    for part in dotted.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return _MISSING
            cur = cur[part]
        else:
            cur = getattr(cur, part, _MISSING)
            if cur is _MISSING:
                return _MISSING
    return cur


class Filter:
    """A composable predicate over a payload, yielding matched values."""

    def matches(self, payload: Any) -> list[Any]:
        """The matched value(s) to hand to a handler; empty when no match."""
        raise NotImplementedError

    def __call__(self, payload: Any) -> bool:
        return bool(self.matches(payload))

    def __and__(self, other: Filter | Callable) -> SpecFilter:
        o = as_filter(other)
        return SpecFilter(lambda p: self(p) and o(p), f"({self!r} & {o!r})")

    def __or__(self, other: Filter | Callable) -> SpecFilter:
        o = as_filter(other)
        return SpecFilter(lambda p: self(p) or o(p), f"({self!r} | {o!r})")

    def __invert__(self) -> SpecFilter:
        return SpecFilter(lambda p: not self(p), f"~{self!r}")


class TypeFilter(Filter):
    """Matches a payload that *is* ``type_``, or carries a field of that type."""

    __slots__ = ("type_",)

    def __init__(self, type_: type) -> None:
        self.type_ = type_

    def matches(self, payload: Any) -> list[Any]:
        if isinstance(payload, self.type_):
            return [payload]
        return [v for v in field_values(payload).values() if isinstance(v, self.type_)]

    def __repr__(self) -> str:
        return f"TypeFilter({self.type_.__name__})"


class SpecFilter(Filter):
    """Predicate filter over an arbitrary callable; safe=True silences exceptions instead of logging."""

    __slots__ = ("_fn", "_repr", "safe")

    def __init__(
        self, fn: Callable[[Any], bool], repr_: str = "SpecFilter", *, safe: bool = False
    ) -> None:
        self._fn = fn
        self._repr = repr_
        self.safe = safe

    def matches(self, payload: Any) -> list[Any]:
        try:
            return [payload] if self._fn(payload) else []
        except Exception:
            if not self.safe:
                logger.warning("Filter predicate %s raised on payload", self._repr, exc_info=True)
            return []

    def __repr__(self) -> str:
        return self._repr


def as_filter(x: Filter | type | Callable) -> Filter:
    """Coerce a type, __as_filter__ provider, callable, or Filter into a Filter."""
    if isinstance(x, Filter):
        return x
    if isinstance(x, type):
        return TypeFilter(x)
    conv = getattr(x, "__as_filter__", None)
    if conv is not None:
        result = conv()
        if not isinstance(result, Filter):
            raise TypeError(
                f"{type(x).__name__}.__as_filter__() must return a Filter, got {result!r}"
            )
        return result
    if callable(x):
        return SpecFilter(x, getattr(x, "__name__", "predicate"))
    raise TypeError(f"Cannot use {x!r} as a Filter")


def all_of(*keys: Filter | type | Callable) -> Filter:
    """AND-compose filters; all must match the same payload. Zero keys raises TypeError."""
    flts = [as_filter(k) for k in keys]
    if not flts:
        raise TypeError("all_of() requires at least one filter")
    if len(flts) == 1:
        return flts[0]
    composed = "(" + " & ".join(repr(f) for f in flts) + ")"
    return SpecFilter(lambda p: all(f(p) for f in flts), composed)


class RoleFilter(Filter):
    """Matches a Signal whose ``emitter_role`` equals the subscribed role; operates on the envelope, not the payload."""

    __slots__ = ("role",)

    def __init__(self, role: str) -> None:
        self.role = role

    def matches(self, event: Any) -> list[Any]:
        # Called with the *event* (Signal envelope) by SessionObserver._match.
        role = getattr(event, "emitter_role", None)
        if role != self.role:
            return []
        payload = getattr(event, "data", None)
        return [payload] if payload is not None else [event]

    def __repr__(self) -> str:
        return f"RoleFilter({self.role!r})"


class FieldRef:
    """Handle to a named field that builds SpecFilters via comparison operators (from Spec.q)."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def _cmp(self, op: Callable, other: Any, sym: str) -> SpecFilter:
        name = self.name

        def check(payload: Any) -> bool:
            val = resolve_path(payload, name)
            if val is _MISSING:
                return False
            return op(val, other)

        return SpecFilter(check, f"{name}{sym}{other!r}", safe=True)

    def __eq__(self, other: Any) -> SpecFilter:  # type: ignore[override]
        return self._cmp(operator.eq, other, "==")

    def __ne__(self, other: Any) -> SpecFilter:  # type: ignore[override]
        return self._cmp(operator.ne, other, "!=")

    def __gt__(self, other: Any) -> SpecFilter:
        return self._cmp(operator.gt, other, ">")

    def __ge__(self, other: Any) -> SpecFilter:
        return self._cmp(operator.ge, other, ">=")

    def __lt__(self, other: Any) -> SpecFilter:
        return self._cmp(operator.lt, other, "<")

    def __le__(self, other: Any) -> SpecFilter:
        return self._cmp(operator.le, other, "<=")

    def is_in(self, choices: Any) -> SpecFilter:
        return self._cmp(lambda v, c: v in c, choices, " in ")

    def present(self) -> SpecFilter:
        name = self.name

        def check(p: Any) -> bool:
            val = resolve_path(p, name)
            return val is not None and val is not _MISSING

        return SpecFilter(check, f"{name}?", safe=True)

    __hash__ = None  # type: ignore[assignment]  # __eq__ returns a Filter, not a bool
