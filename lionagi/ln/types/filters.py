"""Filters — composable predicates that match an emitted payload.

A capability emission is a dynamic model whose fields are named ``Spec``s. A
``Filter`` decides whether a payload is interesting and yields the matched
value(s) to hand to a handler. Two kinds, unified under one abstraction:

- ``TypeFilter(T)`` — matches when the payload *is* a ``T``, or carries a field
  whose value is a ``T``. Hands back the matched instance(s). (A type
  subscription is just a filter that scans ``model_fields`` for ``isinstance``.)
- ``SpecFilter`` — matches a named field by value, built via ``Spec.q``::

      flower = Spec(str, name="flower_name")
      session.observe(flower.q == "rose")   # flower.q → FieldRef("flower_name")

  ``flower.q == "rose"`` is not a bool — it is a ``SpecFilter``, a predicate
  asking "does the payload carry a ``flower_name`` field equal to ``rose``?".

Filters compose with ``&`` / ``|`` / ``~``. ``observe``/``route``/``gate`` all
speak Filter — a plain callable predicate is wrapped via :func:`as_filter`.

Named *Filter*, not *Condition*: ``Condition`` is already a protocols concept
(``await condition.apply``). And the operators live on ``FieldRef`` (handed out
by ``Spec.q``), not on ``Spec`` itself — ``Spec.__eq__`` is load-bearing for
the Operable system (sets, dedup, caches).
"""

from __future__ import annotations

import logging
import operator
from collections.abc import Callable
from typing import Any

__all__ = ("Filter", "TypeFilter", "SpecFilter", "FieldRef", "as_filter")

logger = logging.getLogger(__name__)


def field_values(payload: Any) -> dict[str, Any]:
    """Field name → value for a payload (Pydantic model or dict); else empty."""
    model_fields = getattr(type(payload), "model_fields", None)
    if model_fields:
        return {name: getattr(payload, name, None) for name in model_fields}
    if isinstance(payload, dict):
        return payload
    return {}


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
    """A predicate filter — matches the whole payload when its check passes.

    Built by ``FieldRef`` comparisons (``spec.q == value``), by composition,
    and by wrapping a plain callable. Hands back the payload on a match.

    ``safe`` (set by ``FieldRef``) swallows exceptions — a missing or
    type-incompatible field is just a non-match, by design. Arbitrary user
    predicates wrapped via :func:`as_filter` are **not** safe: their exceptions
    are logged with the predicate repr (so a buggy subscription is visible) and
    then treated as a non-match rather than silently disappearing.
    """

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
    """Coerce a type, callable, or Filter into a Filter."""
    if isinstance(x, Filter):
        return x
    if isinstance(x, type):
        return TypeFilter(x)
    if callable(x):
        return SpecFilter(x, getattr(x, "__name__", "predicate"))
    raise TypeError(f"Cannot use {x!r} as a Filter")


class FieldRef:
    """A handle to a named field that builds :class:`SpecFilter`s via comparison.

    Obtained from ``Spec.q``. The operators return Filters, not bools, so a
    FieldRef must not be used as a dict key or set member.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def _cmp(self, op: Callable, other: Any, sym: str) -> SpecFilter:
        name = self.name

        def check(payload: Any) -> bool:
            values = field_values(payload)
            if name not in values:
                return False
            return op(values[name], other)

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
        return SpecFilter(lambda p: field_values(p).get(name) is not None, f"{name}?", safe=True)

    __hash__ = None  # type: ignore[assignment]  # __eq__ returns a Filter, not a bool
