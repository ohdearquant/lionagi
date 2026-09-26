"""A Spec is one thing an actor lets be said outward (ADR-0001/C1).

A Spec is a Pydantic model class registered under a name. The class is the guidance the model
reads and the validator the system runs; there is no second schema.
"""

from __future__ import annotations

import re
import types
import typing
from collections.abc import Iterable, Iterator

from pydantic import BaseModel

__all__ = (
    "Spec",
    "spec_name",
    "render_guidance",
    "Operable",
)

Spec = type[BaseModel]

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def spec_name(spec: Spec) -> str:
    """The name a Spec is said under: `__spec_name__` when the class sets it, else its snake_case name."""
    explicit = getattr(spec, "__spec_name__", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    return _CAMEL.sub("_", spec.__name__).lower()


def _render_type(annotation: object) -> str:
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is None:
        if isinstance(annotation, type):
            return annotation.__name__
        return str(annotation)
    if origin is typing.Union or origin is types.UnionType:
        return " | ".join(_render_type(a) for a in args)
    if origin is typing.Literal:
        return " | ".join(repr(a) for a in args)
    name = getattr(origin, "__name__", str(origin))
    if args:
        return f"{name}[{', '.join(_render_type(a) for a in args)}]"
    return name


def render_guidance(spec: Spec) -> str:
    """What the model is told about one Spec: its name, its docstring's first line, its fields."""
    doc = (spec.__doc__ or "").strip().splitlines()
    head = f"{spec_name(spec)}"
    if doc:
        head += f": {doc[0].strip()}"
    lines = [head]
    for name, field in spec.model_fields.items():
        line = f"  {name}: {_render_type(field.annotation)}"
        if not field.is_required():
            try:
                default = field.get_default(call_default_factory=True)
            except ValueError:  # a factory that reads the validated data: nothing to show before then
                line += " = (computed)"
            else:
                line += f" = {default!r}"
        if field.description:
            line += f"  # {field.description}"
        lines.append(line)
    return "\n".join(lines)


class Operable:
    """Everything that may be operated: Specs by name, in registration order. An actor's Operable is
    what it has handlers for; a profile's is a `subset(...)` of its actor's."""

    def __init__(self, specs: Iterable[Spec] = ()):
        self._specs: dict[str, Spec] = {}
        for spec in specs:
            self.add(spec)

    def add(self, spec: Spec) -> str:
        name = spec_name(spec)
        existing = self._specs.get(name)
        if existing is not None and existing is not spec:
            raise ValueError(f"spec {name!r} is already registered by {existing.__qualname__}")
        self._specs[name] = spec
        return name

    def get(self, name: str) -> Spec | None:
        return self._specs.get(name)

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __iter__(self) -> Iterator[str]:
        return iter(self._specs)

    def __len__(self) -> int:
        return len(self._specs)

    def names(self) -> frozenset[str]:
        return frozenset(self._specs)

    def subset(self, names: Iterable[str]) -> Operable:
        names = list(names)  # read once: a generator would be spent by the check below
        missing = sorted(set(names) - set(self._specs))
        if missing:
            raise KeyError(f"specs not registered here: {missing}")
        return Operable(self._specs[n] for n in names)

    def guidance(self) -> str:
        """The prompt section listing everything this registry lets be said, in registration order."""
        return "\n\n".join(render_guidance(s) for s in self._specs.values())
