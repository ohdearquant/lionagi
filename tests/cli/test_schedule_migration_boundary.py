# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Move-table precedence and the schedule parser's planned import boundary."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from lionagi import _moved

_OLD = "lionagi.cli._orchestrate_parsing"
_NEW = "lionhub.orchestration.flow.parsing"
_PARENT = "lionagi.cli"
_PLACEHOLDER = "hypothetical.cli"


def _project_root():
    return next(
        parent
        for parent in Path(__file__).resolve().parents
        if (parent / "lionagi/_moved.py").is_file()
    )


@pytest.mark.parametrize("specific_first", [True, False])
def test_specific_synthetic_move_beats_parent_in_either_order(monkeypatch, specific_first):
    parent = _moved.Moved(_PARENT, _PLACEHOLDER, "0.37.0", "2.0.0")
    specific = _moved.Moved(_OLD, _NEW, "0.37.0", "2.0.0")
    rows = (specific, parent) if specific_first else (parent, specific)
    monkeypatch.setattr(_moved, "MOVED", rows)

    # resolve() is pure table resolution, not an import: module locations are
    # irrelevant to this synthetic table-resolution check.
    assert _moved.resolve(_OLD) == _NEW
    assert _moved.resolve(_PARENT) == _PLACEHOLDER
    assert _moved.resolve(_OLD + "_extra") == _PLACEHOLDER + "._orchestrate_parsing_extra"
    assert _moved.resolve("lionagi.cli.status") == _PLACEHOLDER + ".status"
    assert _moved.resolve("lionagi.client") is None

    # With the specific row removed, the exact old name falls through to the
    # hypothetical parent: this control proves the specific assertion matters.
    monkeypatch.setattr(_moved, "MOVED", (parent,))
    assert _moved.resolve(_OLD) == _PLACEHOLDER + "._orchestrate_parsing"


_FLOW = "lionhub.orchestration.flow"
_SCHEDULE_PACKAGE = "lionhub.orchestration.schedule"
_FORBIDDEN_ROOTS = ("lionagi.cli", "lionagi.studio", "lionhub.cli")


def _under(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def _module_imports(source: str, package: str = _SCHEDULE_PACKAGE) -> list[tuple[int, str]]:
    """Resolve static imports and literal importlib/__import__ calls, never execute source.

    An ImportFrom's base is the module; when the base is a parent of a governed
    root, its imported names can themselves be modules and are checked too.
    An ImportFrom *from* the exact flow parser imports attributes, not implied
    submodules, so its attribute names are not misclassified as import paths.
    """
    tree = ast.parse(source)
    import_module_names = {"import_module"}
    importlib_names = {"importlib"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_names.add(alias.asname or "importlib")
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    import_module_names.add(alias.asname or alias.name)

    governed = (*_FORBIDDEN_ROOTS, _FLOW)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative = "." * node.level + (node.module or "")
                base = importlib.util.resolve_name(relative, package)
            else:
                base = node.module or ""
            # ``from ...flow import parsing`` denotes the child module; do
            # not separately treat its syntactic parent as an imported target.
            if base != _FLOW:
                found.append((node.lineno, base))
            if base != _NEW and any(
                root == base or root.startswith(base + ".") or base.startswith(root + ".")
                for root in governed
            ):
                found.extend((node.lineno, f"{base}.{a.name}") for a in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            is_import_module = (isinstance(func, ast.Name) and func.id in import_module_names) or (
                isinstance(func, ast.Attribute)
                and func.attr == "import_module"
                and isinstance(func.value, ast.Name)
                and func.value.id in importlib_names
            )
            is_dunder_import = (isinstance(func, ast.Name) and func.id == "__import__") or (
                isinstance(func, ast.Attribute)
                and func.attr == "__import__"
                and isinstance(func.value, ast.Name)
                and (func.value.id == "builtins" or func.value.id in importlib_names)
            )
            if not (is_import_module or is_dunder_import):
                continue
            name_arg = (
                node.args[0]
                if node.args
                else next((kw.value for kw in node.keywords if kw.arg == "name"), None)
            )
            if not isinstance(name_arg, ast.Constant) or not isinstance(name_arg.value, str):
                continue
            name = name_arg.value
            if is_dunder_import:
                level = (
                    node.args[4]
                    if len(node.args) > 4
                    else next((kw.value for kw in node.keywords if kw.arg == "level"), None)
                )
                if isinstance(level, ast.Constant) and isinstance(level.value, int) and level.value:
                    name = importlib.util.resolve_name("." * level.value + name, package)
                fromlist = (
                    node.args[3]
                    if len(node.args) > 3
                    else next((kw.value for kw in node.keywords if kw.arg == "fromlist"), None)
                )
                if isinstance(fromlist, (ast.List, ast.Tuple)) and name != _NEW:
                    found.extend(
                        (node.lineno, f"{name}.{item.value}")
                        for item in fromlist.elts
                        if isinstance(item, ast.Constant) and isinstance(item.value, str)
                    )
                    if name == _FLOW:
                        continue
            if name.startswith("."):
                if not is_import_module:
                    continue
                anchor = (
                    node.args[1]
                    if len(node.args) > 1
                    else next((kw.value for kw in node.keywords if kw.arg == "package"), None)
                )
                if isinstance(anchor, ast.Constant) and isinstance(anchor.value, str):
                    base_package = anchor.value
                elif isinstance(anchor, ast.Name) and anchor.id == "__package__":
                    base_package = package
                else:
                    continue
                name = importlib.util.resolve_name(name, base_package)
            found.append((node.lineno, name))
    return found


def _forbidden_imports(source: str) -> list[tuple[int, str]]:
    """Apply the narrow synthetic schedule-parser fence to resolved imports."""
    hits = []
    for line, name in _module_imports(source):
        if any(_under(name, root) for root in _FORBIDDEN_ROOTS):
            hits.append((line, name))
        elif _under(name, _FLOW) and name != _NEW:
            hits.append((line, name))
    return hits


@pytest.mark.parametrize(
    "source",
    [
        "import lionhub.orchestration.flow.parsing as parser\n",
        "from lionhub.orchestration.flow.parsing import add_orchestrate_subparser\n",
        "from lionhub.orchestration.flow import parsing\n",
        "from ..flow.parsing import add_orchestrate_subparser\n",
        "def f():\n    from ..flow.parsing import add_orchestrate_subparser\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from ..flow import parsing\n",
        "import importlib as il\nil.import_module('..flow.parsing', package=__package__)\n",
        "from importlib import import_module as load\nload('lionhub.orchestration.flow.parsing')\n",
        "__import__('flow', globals(), None, ['parsing'], 2)\n",
        "import json\n# import lionagi.studio.cli\ntext = 'lionhub.cli.bad'\n",
    ],
)
def test_schedule_boundary_accepts_only_the_permitted_flow_edge(source):
    assert _forbidden_imports(source) == []
    if "flow" in source:
        assert any(name == _NEW for _, name in _module_imports(source)), source


@pytest.mark.parametrize(
    "source",
    [
        "import lionagi.cli.orchestrate\n",
        "from lionagi import studio\n",
        "from lionagi.studio import cli\n",
        "from lionhub.cli import main\n",
        "from ..flow import checkpoint\n",
        "import lionhub.orchestration.flow\n",
        "from ..flow.execution import run\n",
        "import lionhub.orchestration.flow.parsing_extra\n",
        "from ..flow import parsing_extra\n",
        "def f():\n    import lionagi.cli._runs\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import lionagi.studio.app\n",
        "import importlib\nimportlib.import_module('..flow.execution', package=__package__)\n",
        "from importlib import import_module as load\nload('lionhub.cli.main')\n",
        "__import__('lionagi.studio.cli')\n",
        "builtins.__import__('lionagi.studio.cli')\n",
        "importlib.__import__('lionhub.cli.main')\n",
        "__import__('lionagi', fromlist=['studio'])\n",
        "__import__('flow', globals(), None, ['checkpoint'], 2)\n",
    ],
)
def test_schedule_boundary_rejects_forbidden_import_forms(source):
    assert _forbidden_imports(source), source


def test_future_flow_parser_has_a_specific_move_row():
    target = _project_root() / "lionhub/src/lionhub/orchestration/flow/parsing.py"
    if not target.is_file():
        pytest.skip("step-3 flow parsing destination does not exist yet")
    assert any(row.old == _OLD and row.new == _NEW for row in _moved.MOVED)


def test_future_schedule_parser_boundary_when_it_exists():
    # The step-3 destination does not exist before the move.
    target = _project_root() / "lionhub/src/lionhub/orchestration/schedule/parsing.py"
    if not target.is_file():
        pytest.skip("step-3 schedule parsing destination does not exist yet")
    assert _forbidden_imports(target.read_text(encoding="utf-8")) == []
