# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Check the Studio application import fence from ADR-0136 G4 and ADR-0138 F7.

The production wheel may reach the Studio application only through its explicit
launcher. Through 1.x, a longer compatibility row can take an old Studio path to
the wheel, while the remaining old paths reach the application after the move.
The three kept old parents are the only exceptions to that fallback. Two are empty; the Operator
parent forwards three names into the application, so reading one of those names through it, by
``from ... import`` or as an attribute, counts as an application reference.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from scripts.check_feature_boundaries import Scanner, dotted, read_moved_aliases, resolve_alias

_ROOT = Path(__file__).resolve().parents[2]
_HUB_SOURCE = Path("lionhub/src")
_HUB_TESTS = Path("lionhub/tests")
_APP_SOURCE = Path("apps/studio/backend/src")
_OLD_APP = "lionagi.studio"
_APP = "lionagi_studio"
_OLD_OPERATOR = f"{_OLD_APP}.operator"
_KEPT_PARENTS = {_OLD_APP, f"{_OLD_APP}.services", _OLD_OPERATOR}
_OPERATOR_SOURCE = Path("lionagi/studio/operator/__init__.py")


def _forwarded_names() -> frozenset[str]:
    """The names the kept Operator parent forwards, read from its source."""
    tree = ast.parse((_ROOT / _OPERATOR_SOURCE).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_FORWARDED" for target in node.targets
        ):
            names = {
                element.value
                for element in ast.walk(node.value)
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
            assert names, "the Operator parent's _FORWARDED holds no names"
            return frozenset(names)
    raise AssertionError(f"{_OPERATOR_SOURCE} has no _FORWARDED assignment")


_FORWARDED = _forwarded_names()

_LAUNCHER = _HUB_SOURCE / "lionhub/serve/cli/studio.py"
_LAUNCH_TARGET = "lionagi_studio.app:app"
_LAUNCH_COUNT = 2

# Hub tests import no Studio application module, under its old name or its own: an application
# check lives with the application's tests, and a hub guard names the application only as text.
# An entry here would be an exact, counted exception, so a new one is a deliberate edit.
_PRE_MOVE_HUB_TESTS: dict[str, dict[str, int]] = {}


class _Reference(NamedTuple):
    line: int
    target: str
    application_module: str


def _within(name: str, package: str) -> bool:
    return name == package or name.startswith(package + ".")


def _application_module(target: str, aliases: dict[str, str]) -> str | None:
    if _within(target, _APP):
        return target
    if target in _KEPT_PARENTS or not _within(target, _OLD_APP):
        return None

    prefix = target
    while prefix != _OLD_APP:
        if prefix in aliases:
            destination = aliases[prefix] + target[len(prefix) :]
            return destination if _within(destination, _APP) else None
        prefix = prefix.rpartition(".")[0]
    return _APP + target[len(_OLD_APP) :]


def _argument(node: ast.Call, index: int, keyword: str) -> ast.expr | None:
    if len(node.args) > index:
        return node.args[index]
    return next((arg.value for arg in node.keywords if arg.arg == keyword), None)


class _StudioScanner(Scanner):
    """Use the shared import scanner and include literal ASGI launch targets."""

    def __init__(self, source: str, path: Path, modules: set[str], aliases: dict[str, str]):
        super().__init__(source, path, modules, aliases)
        self.launches: list[tuple[int, str]] = []
        # Names the kept Operator parent forwards into the application, read through it.
        self.forwarded: list[tuple[int, str]] = []

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self.resolve("." * node.level + (node.module or "")) == _OLD_OPERATOR:
            for alias in node.names:
                if alias.name in _FORWARDED:
                    self.forwarded.append((node.lineno, f"{_OLD_OPERATOR}.{alias.name}"))
        super().visit_ImportFrom(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        head, separator, tail = (dotted(node) or "").partition(".")
        if separator and head in self.aliases:
            parent, _, name = f"{self.aliases[head]}.{tail}".rpartition(".")
            if parent == _OLD_OPERATOR and name in _FORWARDED:
                self.forwarded.append((node.lineno, f"{parent}.{name}"))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        head, separator, tail = (dotted(node.func) or "").partition(".")
        call = self.aliases.get(head, head) + separator + tail
        if call == "uvicorn.run":
            value = _argument(node, 0, "app")
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                self.launches.append((node.lineno, value.value))
        elif call in {"__import__", "builtins.__import__"}:
            name = _argument(node, 0, "name")
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                self.add(name.value, node.lineno, "dynamic_literal")
                fromlist = _argument(node, 3, "fromlist")
                if isinstance(fromlist, (ast.List, ast.Tuple)):
                    for item in fromlist.elts:
                        if isinstance(item, ast.Constant) and isinstance(item.value, str):
                            candidate = f"{name.value}.{item.value}"
                            if candidate in self.modules or resolve_alias(
                                candidate, self.moved_aliases, self.modules
                            ):
                                self.add(candidate, node.lineno, "dynamic_literal")
        super().visit_Call(node)


def _module_name(path: Path) -> str:
    for base in (_HUB_SOURCE, _HUB_TESTS, _APP_SOURCE):
        if path.is_relative_to(base):
            path = path.relative_to(base)
            break
    parts = path.with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _references(
    path: Path, text: str, modules: set[str], aliases: dict[str, str]
) -> list[_Reference]:
    source = _module_name(path)
    scanner = _StudioScanner(source, path, modules, aliases)
    scanner.visit(ast.parse(text, filename=str(path)))
    targets = [(line, target) for _, target, line, _ in scanner.edges]
    targets.extend(scanner.launches)
    found = [
        _Reference(line, target, _APP + _OLD_OPERATOR[len(_OLD_APP) :])
        for line, target in scanner.forwarded
    ]
    for line, target in targets:
        module = target.partition(":")[0]
        if application_module := _application_module(module, aliases):
            found.append(_Reference(line, target, application_module))
    return sorted(found)


def _tracked_python_paths() -> list[Path]:
    files = subprocess.run(
        ["git", "ls-files", "-z", "--", "lionagi", _HUB_SOURCE, _HUB_TESTS, _APP_SOURCE],
        cwd=_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return [
        Path(os.fsdecode(path)) for path in files.split(b"\0") if path.endswith((b".py", b".pyi"))
    ]


def _boundary_violations(
    references: Iterable[tuple[Path, _Reference]], expected: Counter[tuple[Path, str]]
) -> list[str]:
    observed: Counter[tuple[Path, str]] = Counter()
    launches = 0
    problems = []
    for path, ref in references:
        if path == _LAUNCHER and ref.target == _LAUNCH_TARGET:
            launches += 1
            continue
        if path.is_relative_to(_HUB_TESTS) and _within(ref.target, _OLD_APP):
            key = (path, ref.target)
            observed[key] += 1
            if observed[key] <= expected[key]:
                continue
        problems.append(f"{path}:{ref.line}: {ref.target} -> {ref.application_module}")

    if launches != _LAUNCH_COUNT:
        problems.append(f"{_LAUNCHER}: expected {_LAUNCH_COUNT} launch targets, saw {launches}")
    for (path, target), count in (expected - observed).items():
        problems.append(f"{path}: {count} unused pre-move test import(s) of {target}")
    return problems


def test_wheel_and_hub_tests_respect_the_studio_application_boundary() -> None:
    paths = _tracked_python_paths()
    aliases = read_moved_aliases(_ROOT / "lionagi/_moved.py")
    assert aliases, "could not read the wheel's move table"
    assert any(path.is_relative_to(_HUB_SOURCE) for path in paths)
    assert any(path.is_relative_to(_HUB_TESTS) for path in paths)
    assert any(path.parts[0] == "lionagi" for path in paths)

    modules = {_module_name(path) for path in paths}
    expected = Counter(
        {
            (_HUB_TESTS / "lionhub_tests" / relative, target): count
            for target, entries in _PRE_MOVE_HUB_TESTS.items()
            for relative, count in entries.items()
        }
    )
    references = (
        (path, ref)
        for path in paths
        if not path.is_relative_to(_APP_SOURCE)
        for ref in _references(path, (_ROOT / path).read_text(encoding="utf-8"), modules, aliases)
    )
    problems = _boundary_violations(references, expected)
    assert not problems, "\n".join(problems)


# Text outside an import can reach the application too: a module run by name
# (`python -m lionagi_studio`) or an import string handed to another process. In the wheel and
# hub sources only the launcher and the move table may hold one, under the application's own
# name or an old one the move table sends to it. A docstring is prose and is not counted, nor is
# a logger name: the application's loggers keep their old names until the removal release.
_NAME_HOLDERS = frozenset({_LAUNCHER, Path("lionagi/_moved.py")})
# A whole string that is an old dotted module path, optionally with an ``:attribute``.
_OLD_NAME = re.compile(r"lionagi\.studio(\.[A-Za-z_]\w*)*(:[A-Za-z_]\w*)?")


def _docstrings(tree: ast.AST) -> set[int]:
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, holders)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }


def _logger_names(tree: ast.AST) -> set[int]:
    return {
        id(node.args[0])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (dotted(node.func) or "").rpartition(".")[2] == "getLogger"
        and node.args
    }


def _application_named(source: str, aliases: dict[str, str]) -> list[int]:
    """Lines of string literals, docstrings and logger names aside, that name the application:
    its own root, or an old path the move table sends to it."""
    tree = ast.parse(source)
    prose = _docstrings(tree) | _logger_names(tree)
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in prose
        and (
            _APP in node.value
            or (
                _OLD_NAME.fullmatch(node.value) is not None
                and _application_module(node.value.partition(":")[0], aliases) is not None
            )
        )
    )


def test_only_the_launcher_and_move_table_name_the_application_as_text() -> None:
    aliases = read_moved_aliases(_ROOT / "lionagi/_moved.py")
    assert aliases, "could not read the wheel's move table"
    paths = [
        path
        for path in _tracked_python_paths()
        if (path.parts[0] == "lionagi" or path.is_relative_to(_HUB_SOURCE))
        and path not in _NAME_HOLDERS
    ]
    assert any(path.is_relative_to(_HUB_SOURCE) for path in paths)
    assert any(path.parts[0] == "lionagi" for path in paths)
    problems = [
        f"{path}:{line}"
        for path in paths
        for line in _application_named((_ROOT / path).read_text(encoding="utf-8"), aliases)
    ]
    assert not problems, "\n".join(problems)
    assert _application_named((_ROOT / _LAUNCHER).read_text(encoding="utf-8"), aliases)


def test_application_named_as_text_is_found_and_prose_is_not() -> None:
    aliases = {
        "lionagi.studio": "lionagi_studio",
        "lionagi.studio.services.runs": "lionhub.serve.daemon.services.runs",
    }
    source = """\
\"\"\"Serves lionagi_studio when it is installed.\"\"\"
import subprocess, sys


def run():
    \"\"\"Starts lionagi_studio.\"\"\"
    subprocess.run([sys.executable, "-m", "lionagi_studio"])
    target = f"lionagi_studio.app:{'app'}"
    subprocess.run([sys.executable, "-m", "lionagi.studio.operator.permission_mcp"])
    target = "lionagi.studio.app:app"
    daemon = "lionagi.studio.services.runs"
    kept = ("lionagi.studio", "lionagi.studio.services", "lionagi.studio.operator")
    note = "see lionagi.studio.app for the old path"
    log = logging.getLogger("lionagi.studio.config")
"""
    assert _application_named(source, aliases) == [7, 8, 9, 10]


def test_studio_boundary_detects_old_and_canonical_imports_in_each_form() -> None:
    path = _HUB_TESTS / "lionhub_tests/example.py"
    source = """\
from lionagi.studio.services import workflow_compile
from lionagi.studio import config as current_config
import lionagi_studio
from importlib import import_module as load
load("lionagi.studio.operator.run_progress")
load(".services.workflow_compile", package="lionagi.studio")
import builtins as py
py.__import__("lionagi.studio.services", fromlist=["workflow_compile"])
"""
    modules = {"lionagi.studio.services.workflow_compile", "lionagi.studio.config"}
    assert _references(path, source, modules, {}) == [
        _Reference(
            1,
            "lionagi.studio.services.workflow_compile",
            "lionagi_studio.services.workflow_compile",
        ),
        _Reference(2, "lionagi.studio.config", "lionagi_studio.config"),
        _Reference(3, "lionagi_studio", "lionagi_studio"),
        _Reference(
            5, "lionagi.studio.operator.run_progress", "lionagi_studio.operator.run_progress"
        ),
        _Reference(
            6,
            "lionagi.studio.services.workflow_compile",
            "lionagi_studio.services.workflow_compile",
        ),
        _Reference(
            8,
            "lionagi.studio.services.workflow_compile",
            "lionagi_studio.services.workflow_compile",
        ),
    ]


def test_longest_studio_daemon_move_and_empty_parents_remain_available() -> None:
    path = _HUB_SOURCE / "lionhub/example.py"
    aliases = {
        "lionagi.studio": "lionagi_studio",
        "lionagi.studio.scheduler": "lionhub.orchestration.scheduler",
        "lionagi.studio.services.runs": "lionhub.serve.daemon.services.runs",
    }
    source = """\
import lionagi.studio
import lionagi.studio.services
import lionagi.studio.scheduler.engine
from lionagi.studio.services import runs, workflow_compile
__import__("lionagi.studio.services", fromlist=("workflow_compile",))
"""
    modules = {"lionhub.serve.daemon.services.runs", "lionagi_studio.services.workflow_compile"}
    assert _references(path, source, modules, aliases) == [
        _Reference(
            4,
            "lionagi.studio.services.workflow_compile",
            "lionagi_studio.services.workflow_compile",
        ),
        _Reference(
            5,
            "lionagi.studio.services.workflow_compile",
            "lionagi_studio.services.workflow_compile",
        ),
    ]


def test_a_forwarded_operator_name_is_an_application_reference_and_its_parent_is_not() -> None:
    # The Operator parent is kept at its old path and imports nothing, so importing it, or the
    # daemon module below it (which takes its own longer row to the hub), reaches no
    # application code. Reading one of the names it forwards does, by import or as an attribute.
    path = _HUB_SOURCE / "lionhub/example.py"
    aliases = {
        "lionagi.studio": "lionagi_studio",
        "lionagi.studio.operator.redact": "lionhub.serve.daemon.services.redact",
    }
    source = """\
import lionagi.studio.operator
from lionagi.studio.operator import OperatorCoordinator
import lionagi.studio.operator.redact
from lionagi.studio.operator import redact
lionagi.studio.operator.get_operator_coordinator()
import lionagi.studio.operator as old_operator
old_operator.reset_operator_coordinator_for_testing
old_operator.not_forwarded
"""
    operator = "lionagi_studio.operator"
    assert _references(path, source, set(), aliases) == [
        _Reference(2, "lionagi.studio.operator.OperatorCoordinator", operator),
        _Reference(5, "lionagi.studio.operator.get_operator_coordinator", operator),
        _Reference(7, "lionagi.studio.operator.reset_operator_coordinator_for_testing", operator),
    ]


def test_the_forwarded_names_are_read_from_the_operator_parent() -> None:
    import lionagi.studio.operator as kept

    assert _FORWARDED == kept._FORWARDED


def test_move_table_declaration_is_compatibility_data() -> None:
    path = Path("lionagi/_moved.py")
    table = 'MOVED = (Moved("lionagi.studio", "lionagi_studio", "1.0", "2.0"),)\n'
    assert _references(path, table, set(), {}) == []
    assert _references(path, table + "import lionagi_studio\n", set(), {}) == [
        _Reference(2, "lionagi_studio", "lionagi_studio"),
    ]


def test_studio_asgi_targets_are_import_references() -> None:
    source = """\
import uvicorn as server
server.run("lionagi.studio.app:app")
server.run(app="lionagi_studio.app:alternate")
"""
    assert _references(_LAUNCHER, source, set(), {}) == [
        _Reference(2, "lionagi.studio.app:app", "lionagi_studio.app"),
        _Reference(3, "lionagi_studio.app:alternate", "lionagi_studio.app"),
    ]


def test_launcher_and_old_test_allowances_are_exact() -> None:
    hub_test = _HUB_TESTS / "lionhub_tests/example.py"
    not_the_launcher = _HUB_SOURCE / "lionhub/example.py"
    legacy_test = _Reference(1, "lionagi.studio.config", "lionagi_studio.config")
    canonical_test = _Reference(1, "lionagi_studio.config", "lionagi_studio.config")
    expected = Counter({(hub_test, legacy_test.target): 1})
    launch = _Reference(2, _LAUNCH_TARGET, "lionagi_studio.app")
    launches = [(_LAUNCHER, launch), (_LAUNCHER, launch._replace(line=3))]
    assert _boundary_violations([*launches, (hub_test, legacy_test)], expected) == []
    assert _boundary_violations([*launches, (hub_test, legacy_test)] * 2, expected) == [
        f"{hub_test}:1: lionagi.studio.config -> lionagi_studio.config",
        f"{_LAUNCHER}: expected 2 launch targets, saw 4",
    ]

    assert _boundary_violations(
        [*launches, (not_the_launcher, launch), (hub_test, canonical_test)], expected
    ) == [
        f"{not_the_launcher}:2: {_LAUNCH_TARGET} -> lionagi_studio.app",
        f"{hub_test}:1: lionagi_studio.config -> lionagi_studio.config",
        f"{hub_test}: 1 unused pre-move test import(s) of lionagi.studio.config",
    ]
