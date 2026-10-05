"""docs/README.md is the map of the runtime, kept in step with the code: every module of src/lion1 is in
its table, each row of the value sets names a field whose type holds exactly the values the row lists
and every closed value set the runtime and the served actor declare has a row, the event kinds it
names are the ones they emit, and every relative link on it and on the README resolves."""

import ast
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


def literals() -> list[tuple[str, ...]]:
    """Every `Literal[...]` written in src/lion1 and hub/serve, as its values in order."""
    found = []
    for path in sorted([*SRC.glob("*.py"), *SERVE.glob("*.py")]):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "Literal"
            ):
                items = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
                found.append(tuple(item.value for item in items))
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


def test_each_value_set_row_names_a_field_whose_type_holds_exactly_its_values():
    table = rows()
    assert len(table) >= 11 and table[0][0] == "`Message.role`"  # the parse reads what it should
    for field, kind, listed in table:
        hint = declared(field)
        assert re.findall(r"`([^`]+)`", re.split(r"[;(]", listed)[0]) == values_of(hint), field
        named = re.match(r"`(\w+)\.(\w+)`", kind)
        if named:  # the type cell names the alias the field is annotated with
            alias = getattr(MODULES[named[1]], named[2])
            assert hint in (alias, alias | None), field


def test_every_closed_value_set_the_code_declares_has_a_row():
    sets = literals()
    assert ("system", "user", "assistant") in sets and len(sets) >= 10  # the walk reads what it should
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
