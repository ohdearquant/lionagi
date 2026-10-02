"""A Spec is one thing an actor lets be said outward (ADR-0001/C1).

A Spec is a Pydantic model class registered under a name. The class is the guidance the model
reads and the validator the system runs; there is no second schema.
"""

from __future__ import annotations

import json
import keyword
import re
import types
import typing
import unicodedata
from collections.abc import Iterable, Iterator

from pydantic import AliasChoices, BaseModel, ConfigDict

__all__ = (
    "Spec",
    "spec_name",
    "field_key",
    "render_guidance",
    "Operable",
)

Spec = type[BaseModel]

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
# pydantic 2.11 reads validate_by_alias and validate_by_name; before it a config may carry them, and
# validation reads by alias, and by name too under populate_by_name, whatever they say
_BY_SETTINGS = "validate_by_name" in ConfigDict.__annotations__
# a key the actor's two readers both take bare: an OUT{} tag quotes a bare `[A-Za-z_]\w*` key (lndl's
# `_BARE_KEY`), and a program reads a Python name, NFKC-normalized, never a keyword or a `__` name
_BARE = re.compile(r"[A-Za-z_]\w*")


def spec_name(spec: Spec) -> str:
    """The name a Spec is said under: `__spec_name__` when the class sets it, else its snake_case name."""
    explicit = getattr(spec, "__spec_name__", None)
    if isinstance(explicit, str) and explicit:
        return explicit
    return _CAMEL.sub("_", spec.__name__).lower()


def field_key(spec: Spec, name: str) -> list[str | int]:
    """Where validation reads field `name`, key by key, one key when one will do: its alias unless the
    spec reads names only, the first alternative that is one key, the name when the alias is a path
    and the spec reads names too; else the alias path whole, which only a nested value fills.

    That one key is kept only while no other field is read from it, or from anywhere validation tries
    before it, nested inside it or around it, and nothing validation tries before it lies inside it or
    around it, so validation reads the field back from it; else the first path validation tries, when
    no other field is read from it (a later path is read only when every earlier one is missing, and
    a field written there would be read from the first); else the one key, as before. So two fields'
    values never land in one place when a key exists that keeps them apart."""
    paths = _paths(spec, name)
    key = _one_key(spec, name)
    earlier = paths[: paths.index(key)]
    others = [p for other in spec.model_fields if other != name for p in _paths(spec, other)]
    if not any(_overlap(p, q) for p in [*earlier, key] for q in others) and not any(
        _overlap(p, key) for p in earlier
    ):
        return key
    if not any(_overlap(paths[0], q) for q in others):
        return paths[0]
    return key


def _one_key(spec: Spec, name: str) -> list[str | int]:
    alias = spec.model_fields[name].validation_alias
    if _BY_SETTINGS and not spec.model_config.get("validate_by_alias", True):
        return [name]
    if isinstance(alias, AliasChoices):  # validation reads any one of them
        alias = next((c for c in alias.choices if isinstance(c, str)), alias.choices[0])
    if alias is None:
        return [name]
    if isinstance(alias, str):
        return [alias]
    if _by_name(spec):
        return [name]
    return list(alias.path)


def _paths(spec: Spec, name: str) -> list[list[str | int]]:
    """Every path validation reads field `name` from, in the order it tries them: the alias, or each of
    its alternatives, then the name when the spec reads names too."""
    alias = spec.model_fields[name].validation_alias
    if alias is None or (_BY_SETTINGS and not spec.model_config.get("validate_by_alias", True)):
        return [[name]]
    choices = alias.choices if isinstance(alias, AliasChoices) else [alias]
    paths = [[c] if isinstance(c, str) else list(c.path) for c in choices]
    return paths + [[name]] if _by_name(spec) else paths


def _by_name(spec: Spec) -> bool:
    # pydantic 2.11 on: populate_by_name sets validate_by_name when it is unset
    by_name = spec.model_config.get("validate_by_name") if _BY_SETTINGS else None
    if by_name is None:  # before 2.11 only populate_by_name
        by_name = spec.model_config.get("populate_by_name")
    return bool(by_name)


def _overlap(a: list[str | int], b: list[str | int]) -> bool:
    """Whether one path is the other or leads into it."""
    return a[: len(b)] == b or b[: len(a)] == a


def _said(key: str) -> str:
    """A path's first key as the guidance says it: bare when an OUT{} tag and a program both read it back
    as itself, else quoted as the keys after it are."""
    bare = (
        _BARE.fullmatch(key) is not None
        and key.isidentifier()
        and not keyword.iskeyword(key)
        and not key.startswith("__")
        and unicodedata.normalize("NFKC", key) == key
    )
    return key if bare else _quoted(key)


def _quoted(key: str | int) -> str:
    """A path key as a literal both readers take: a list index as its number; a string in double quotes,
    its printable characters as they are and the rest escaped as Python reads an escape."""
    if not isinstance(key, str):
        return json.dumps(key)
    out = []
    for c in key:
        if c in '"\\':
            out.append("\\" + c)
        elif c.isprintable():
            out.append(c)
        elif ord(c) > 0xFFFF:  # JSON would write a surrogate pair, which Python reads as two characters
            out.append(f"\\U{ord(c):08x}")
        else:
            out.append(json.dumps(c)[1:-1])
    return '"' + "".join(out) + '"'


def _render_type(annotation: object) -> str:
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is None:
        # a class, a type variable, a NewType: by its name, never its repr (`~T`, `module.UserId`)
        return getattr(annotation, "__name__", None) or str(annotation)
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
        # a path is said as the nesting it reads, `report["summary"]`: a dotted key would read as one key
        first, *rest = field_key(spec, name)
        key = _said(str(first)) + "".join(f"[{_quoted(k)}]" for k in rest)
        line = f"  {key}: {_render_type(field.annotation)}"
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
