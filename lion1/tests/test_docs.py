"""docs/README.md is the map of the runtime, kept in step with the code: every module of src/lion1 is in
its table, each row of the value sets names a field whose type holds exactly the values the row lists
and every closed value set the runtime and the served actor declare has a row, the event kinds it
names are the ones they emit, and every relative link on it and on the README resolves."""

import ast
import importlib
import re
import typing
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

import hub.serve.service as service
from lion1 import actor, record, store
from lion1.actor import Run
from lion1.spec import spec_name

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "README.md"
SRC = ROOT / "src" / "lion1"
SERVE = ROOT / "hub" / "serve"
MODULES = {"actor": actor, "record": record, "store": store, "service": service}
_LINK = re.compile(r"\]\(([^)\s]+)\)")


def section(title: str) -> str:
    """The text under one `## ` heading, to the next."""
    text = DOC.read_text()
    start = text.index(f"\n## {title}\n")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def literals_in(source: str) -> list[tuple[str, ...]]:
    """Every `Literal[...]` in `source`, as its values in order, however the file binds the name: imported
    from `typing` (or `typing_extensions`), aliased, or reached through the module, itself aliased."""
    tree = ast.parse(source)
    names, modules = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("typing", "typing_extensions"):
            names |= {a.asname or a.name for a in node.names if a.name == "Literal"}
        elif isinstance(node, ast.Import):
            modules |= {a.asname or a.name for a in node.names if a.name in ("typing", "typing_extensions")}
    found = []
    for node in ast.walk(tree):
        base = node.value if isinstance(node, ast.Subscript) else None
        if (isinstance(base, ast.Name) and base.id in names) or (
            isinstance(base, ast.Attribute)
            and base.attr == "Literal"
            and isinstance(base.value, ast.Name)
            and base.value.id in modules
        ):
            items = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            found.append(tuple(item.value for item in items))
    return found


def literals() -> list[tuple[str, ...]]:
    """Every `Literal[...]` written in src/lion1 and hub/serve, as its values in order."""
    return [
        v for path in sorted([*SRC.glob("*.py"), *SERVE.glob("*.py")]) for v in literals_in(path.read_text())
    ]


def enums() -> list[tuple[str, ...]]:
    """Every enum a module of src/lion1 or hub/serve defines, as its members in order."""
    found = []
    for path in sorted([*SRC.glob("*.py"), *SERVE.glob("*.py")]):
        package = "lion1" if path.parent == SRC else "hub.serve"
        name = package if path.stem == "__init__" else f"{package}.{path.stem}"
        module = importlib.import_module(name)
        found += [
            tuple(values_of(c))
            for c in vars(module).values()
            if isinstance(c, type) and issubclass(c, Enum) and c.__module__ == name
        ]
    return found


def emitted() -> set[str]:
    """Every event kind the runtime and the served actor pass to an `emit` call as a literal."""
    kinds = set()
    for path in sorted([*SRC.glob("*.py"), *SERVE.glob("*.py")]):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "emit"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                kinds.add(node.args[0].value)
    return kinds


def test_every_module_of_the_runtime_is_in_the_modules_table():
    table = section("The modules of `src/lion1`")
    modules = sorted(p.name for p in SRC.glob("*.py"))
    assert "actor.py" in modules and "__init__.py" in modules
    assert [m for m in modules if f"](../src/lion1/{m})" not in table] == []


def rows() -> list[tuple[str, str, str]]:
    """The value-set table as (field, type, values) cells."""
    lines = [ln for ln in section("Value sets").splitlines() if ln.startswith("| `")]
    return [tuple(c.strip() for c in ln.strip("|").split("|")) for ln in lines]


def declared(field: str) -> typing.Any:
    """The annotation a field cell names: `Cls.attr`, `attr` of `Cls.method`, `attr` of `Cls` (its
    constructor), or `attr` of the `name` command."""
    names = re.findall(r"`([^`]+)`", field)
    classes = {n: c for m in MODULES.values() for n, c in vars(m).items() if isinstance(c, type)}
    commands = {spec_name(c): c for c in classes.values() if issubclass(c, BaseModel) and c is not BaseModel}
    if len(names) == 1:
        owner, attr = names[0].split(".")
        target = classes[owner]
    else:
        attr, where = names
        owner, _, method = where.partition(".")
        target = commands[where] if where in commands else getattr(classes[owner], method or "__init__")
    return typing.get_type_hints(target, localns={"Run": Run})[attr]


def values_of(hint: typing.Any) -> list[str]:
    if isinstance(hint, type) and issubclass(hint, Enum):
        return [str(member) for member in hint]
    if typing.get_origin(hint) is typing.Literal:
        return list(typing.get_args(hint))
    return values_of(next(a for a in typing.get_args(hint) if a is not type(None)))  # X | None


# a values cell: the values in backticks, then at most one note: after "; " with no parentheses, or in them
_LISTED = re.compile(r"(?P<values>`[^`]+`(?:, `[^`]+`)*)(?:; (?P<said>[^()]+)| \((?P<aside>[^()]+)\))?")
# a type cell: the alias, the alias or None, the alias as an enum, or an inline Literal
_KIND = re.compile(r"`(?P<module>\w+)\.(?P<alias>\w+)`(?P<tail>| or None|, an enum)|a `Literal`")


def test_each_value_set_row_names_a_field_whose_type_holds_exactly_its_values():
    table = rows()
    assert len(table) >= 11 and table[0][0] == "`Message.role`"  # the parse reads what it should
    # a note keeps values out of the row: it may name an error class a mapped module defines and nothing
    # else in backticks; which of those errors the field raises is the note's prose, not checked here
    errors = {
        n
        for m in MODULES.values()
        for n, c in vars(m).items()
        if isinstance(c, type) and issubclass(c, BaseException) and c.__module__ == m.__name__
    }
    aliases = [
        v for m in MODULES.values() for v in vars(m).values() if typing.get_origin(v) is typing.Literal
    ]
    for field, kind, listed in table:
        hint = declared(field)
        cell = _LISTED.fullmatch(listed)
        assert cell, f"{field}: values cell {listed!r}"
        assert re.findall(r"`([^`]+)`", cell["values"]) == values_of(hint), field
        note = cell["said"] or cell["aside"] or ""
        named = [n for n in re.findall(r"`([^`]+)`", note) if n not in errors]
        assert named == [], f"{field}: a note names a value"
        typed = _KIND.fullmatch(kind)
        assert typed, f"{field}: type cell {kind!r}"
        if typed["alias"] is None:  # an inline Literal, which no module names
            assert typing.get_origin(hint) is typing.Literal and hint not in aliases, field
            continue
        alias = getattr(MODULES[typed["module"]], typed["alias"])
        assert hint == (alias | None if typed["tail"] == " or None" else alias), field
        assert typed["tail"] != ", an enum" or (isinstance(alias, type) and issubclass(alias, Enum)), field


def test_the_inventory_reads_every_spelling_of_literal_and_no_other_subscript():
    source = """
import typing
import typing as t
import typing_extensions
from typing import Literal
from typing import Literal as Lit
from typing_extensions import Literal as Ext

a: Literal["a"]
b: typing.Literal["b", "bb"]
c: t.Literal["c"]
d: Lit["d"]
e: typing_extensions.Literal["e"]
f: Ext["f"]
g: dict["g", int]
h: other.Literal["h"]
"""
    assert literals_in(source) == [("a",), ("b", "bb"), ("c",), ("d",), ("e",), ("f",)]
    # a `Literal` the file never imports from typing is some other name
    assert literals_in('class Literal: ...\nx: Literal["x"]') == []


def test_every_closed_value_set_the_code_declares_has_a_row():
    # a set is one row, found by its values: another field holding a listed set's values is covered by
    # that row, so the table lists sets, not every field that takes one. A declared set whose values no
    # row lists fails here, whichever field holds it
    sets = literals() + enums()
    assert ("system", "user", "assistant") in sets and len(sets) >= 11  # the walks read what they should
    assert tuple(str(k) for k in record.Kind) in sets
    listed = {tuple(values_of(declared(field))) for field, _, _ in rows()}
    assert [values for values in sets if values not in listed] == []


def test_the_event_kinds_named_are_exactly_the_ones_emitted():
    kinds = emitted()
    assert {"run.started", "command.settled", "run.completed", "run.waiting"} <= kinds
    bullet = section("Words").split("- The runtime emits", 1)[1].split("\n- ", 1)[0].split("\n#", 1)[0]
    assert set(re.findall(r"`([a-z]+\.[a-z]+)`", bullet)) == kinds


def test_every_relative_link_resolves():
    broken = []
    for page in (DOC, ROOT / "README.md"):
        targets = [t for t in _LINK.findall(page.read_text()) if "://" not in t and not t.startswith("#")]
        assert targets, f"{page} has no links: the pattern no longer reads them"
        broken += [f"{page.relative_to(ROOT)}: {t}" for t in targets if not (page.parent / t).exists()]
    assert broken == []


def test_the_readme_points_at_the_map():
    assert "](docs/README.md)" in (ROOT / "README.md").read_text()
