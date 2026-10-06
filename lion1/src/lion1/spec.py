"""A Spec is one thing an actor lets be said outward (ADR-0001/C1).

A Spec is a Pydantic model class registered under a name. The class is the guidance the model
reads and the validator the system runs; there is no second schema.
"""

from __future__ import annotations

import itertools
import json
import keyword
import re
import types
import typing
import unicodedata
from collections.abc import Callable, Iterable, Iterator

from pydantic import AliasChoices, BaseModel

__all__ = (
    "Spec",
    "spec_name",
    "field_key",
    "field_keys",
    "render_guidance",
    "Operable",
)

Spec = type[BaseModel]

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
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

    The keys are chosen together for each group of fields whose paths overlap, so that a value written
    at each field's key is read back as that field (`_reads_back`); a field's key bears on no field
    outside its group. A field is offered its one key first unless another field is read from in or
    around it, then its paths in the order validation tries them; earlier fields are served first.
    When no choice among a group's first 1024 reads every field of it back, each field of the group is
    named by the first path validation tries. Each call chooses every field's key: to name several
    fields, take `field_keys` once."""
    return field_keys(spec)[name]


def field_keys(spec: Spec) -> dict[str, list[str | int]]:
    """Every field's `field_key`, chosen together, by field name. Read from the spec as it stands, so
    a changed config or a rebuilt class is followed."""
    paths = {name: _paths(spec, name) for name in spec.model_fields}
    around = _index(paths)
    offered = {}
    for name, own in paths.items():
        key = _one_key(spec, name)
        shared = bool(around(key) - {name})
        offered[name] = own if shared else [key, *(p for p in own if p != key)]
    keys: dict[str, list[str | int]] = {}
    for group in _groups(paths, around):
        # in order of preference; past the first 1024 the group keeps its first paths
        for choice in itertools.islice(itertools.product(*(offered[n] for n in group)), 1024):
            chosen = dict(zip(group, choice, strict=True))
            if _reads_back(chosen, paths):
                break
        else:
            chosen = {name: paths[name][0] for name in group}
        keys.update(chosen)
    return {name: keys[name] for name in paths}


def _index(paths: dict[str, list[list[str | int]]]) -> Callable[[list[str | int]], set[str]]:
    """Who reads in or around a path: the fields with a path that is it, leads into it, or lies inside
    it (`_overlap`), found by the path's own prefixes rather than by every other path."""
    exact: dict[tuple[str | int, ...], set[str]] = {}
    under: dict[tuple[str | int, ...], set[str]] = {}
    for name, own in paths.items():
        for path in own:
            key = tuple(path)
            exact.setdefault(key, set()).add(name)
            for i in range(1, len(key) + 1):
                under.setdefault(key[:i], set()).add(name)

    def around(path: list[str | int]) -> set[str]:
        key = tuple(path)
        found = set(under.get(key, ()))
        for i in range(1, len(key)):
            found |= exact.get(key[:i], set())
        return found

    return around


def _groups(
    paths: dict[str, list[list[str | int]]], around: Callable[[list[str | int]], set[str]]
) -> list[list[str]]:
    """The fields joined wherever a path of one overlaps a path of another, each group in declaration
    order."""
    root = {name: name for name in paths}

    def find(name: str) -> str:
        while root[name] != name:
            root[name] = root[root[name]]
            name = root[name]
        return name

    for name, own in paths.items():
        for path in own:
            for other in around(path):
                root[find(other)] = find(name)
    groups: dict[str, list[str]] = {}
    for name in paths:
        groups.setdefault(find(name), []).append(name)
    return list(groups.values())


def _reads_back(keys: dict[str, list[str | int]], paths: dict[str, list[list[str | int]]]) -> bool:
    """Whether every field is read back from its key when each is written there and nothing else is: no
    key is another's or lies inside it, and each path validation tries before a field's key is then
    missing (it touches no key) or lies inside another field's value, which holds there what it held
    when that value was read, with no missing path tried before it."""
    around = _index({name: [key] for name, key in keys.items()})
    for name, key in keys.items():
        if around(key) - {name}:
            return False
        missing = False
        for path in paths[name][: paths[name].index(key)]:
            hit = around(path) - {name}
            if _overlap(path, key) or any(len(path) <= len(keys[k]) for k in hit) or (hit and missing):
                return False
            missing = missing or not hit
    return True


def _one_key(spec: Spec, name: str) -> list[str | int]:
    alias = spec.model_fields[name].validation_alias
    if not spec.model_config.get("validate_by_alias", True):
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
    if alias is None or not spec.model_config.get("validate_by_alias", True):
        return [[name]]
    choices = alias.choices if isinstance(alias, AliasChoices) else [alias]
    paths = [[c] if isinstance(c, str) else list(c.path) for c in choices]
    return paths + [[name]] if _by_name(spec) else paths


def _by_name(spec: Spec) -> bool:
    # populate_by_name sets validate_by_name when the config leaves it unset
    return bool(spec.model_config.get("validate_by_name"))


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
    keys = field_keys(spec)
    for name, field in spec.model_fields.items():
        # a path is said as the nesting it reads, `report["summary"]`: a dotted key would read as one key
        first, *rest = keys[name]
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
