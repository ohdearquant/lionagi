"""docs/README.md is the map of the runtime, kept in step with the code: every module of src/lion1 is in
its table, every closed value set the code declares is listed with its values in order, every event
kind the runtime emits is named, and every relative link on it and on the README resolves."""

import ast
import re
from pathlib import Path

from lion1.record import Kind

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "README.md"
SRC = ROOT / "src" / "lion1"
_LINK = re.compile(r"\]\(([^)\s]+)\)")


def section(title: str) -> str:
    """The text under one `## ` heading, to the next."""
    text = DOC.read_text()
    start = text.index(f"\n## {title}\n")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def literals() -> list[tuple[str, ...]]:
    """Every `Literal[...]` written in src/lion1, as its values in order."""
    found = []
    for path in sorted(SRC.glob("*.py")):
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
    """Every event kind src/lion1 passes to an `emit` call as a literal."""
    kinds = set()
    for path in sorted(SRC.glob("*.py")):
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


def test_every_closed_value_set_the_code_declares_is_listed_with_its_values_in_order():
    table = section("Value sets")
    sets = literals()
    assert ("system", "user", "assistant") in sets and len(sets) >= 8  # the walk reads what it should
    sets.append(tuple(str(k) for k in Kind))
    missing = [values for values in sets if ", ".join(f"`{v}`" for v in values) not in table]
    assert missing == []


def test_every_event_kind_the_runtime_emits_is_named():
    kinds = emitted()
    assert {"run.started", "command.settled", "run.completed"} <= kinds
    words = section("Words")
    assert sorted(k for k in kinds if f"`{k}`" not in words) == []


def test_every_relative_link_resolves():
    broken = []
    for page in (DOC, ROOT / "README.md"):
        targets = [t for t in _LINK.findall(page.read_text()) if "://" not in t and not t.startswith("#")]
        assert targets, f"{page} has no links: the pattern no longer reads them"
        broken += [f"{page.relative_to(ROOT)}: {t}" for t in targets if not (page.parent / t).exists()]
    assert broken == []


def test_the_readme_points_at_the_map():
    assert "](docs/README.md)" in (ROOT / "README.md").read_text()
