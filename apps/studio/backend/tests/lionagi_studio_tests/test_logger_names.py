# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The application's loggers keep the names they had under lionagi.studio until 2.0.

Logging configuration keyed on ``lionagi.studio`` and the hub's own ``lionagi.studio.*``
loggers keep receiving the application's records. Each module logs under its own old name,
or under the old package logger ``lionagi.studio``; ``getLogger(__name__)`` would log under
``lionagi_studio`` instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import lionagi_studio
from lionagi import _moved

APP_ROOT = Path(lionagi_studio.__file__).resolve().parent
OLD_PACKAGE = "lionagi.studio"


def _module_name(path: Path, root: Path) -> str:
    parts = ["lionagi_studio", *path.relative_to(root).with_suffix("").parts]
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _logger_calls(path: Path) -> list[tuple[int, ast.expr | None]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "getLogger":
            calls.append((node.lineno, node.args[0] if node.args else None))
    return calls


def _wrong_logger_names(path: Path, root: Path = APP_ROOT) -> list[str]:
    module = _module_name(path, root)
    wrong = []
    for lineno, arg in _logger_calls(path):
        where = f"{path.relative_to(root)}:{lineno}"
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            wrong.append(f"{where}: not a literal name")
        elif arg.value != OLD_PACKAGE and _moved.resolve(arg.value) != module:
            wrong.append(f"{where}: {arg.value!r} is not {module}'s old name")
    return wrong


def test_every_application_logger_keeps_its_old_name():
    files = sorted(APP_ROOT.rglob("*.py"))
    calls = [call for path in files for call in _logger_calls(path)]
    assert len(calls) >= 9, "the application names fewer loggers than it did at the move"
    assert [hit for path in files for hit in _wrong_logger_names(path)] == []


def test_the_check_refuses_the_new_name_and_another_module_s_name(tmp_path):
    (tmp_path / "services").mkdir()
    probe = tmp_path / "services" / "agents.py"
    cases = {
        "logging.getLogger(__name__)\n": 1,
        'logging.getLogger("lionagi_studio.services.agents")\n': 1,
        'logging.getLogger("lionagi.studio.services.shows")\n': 1,
        "logging.getLogger()\n": 1,
        'logging.getLogger("lionagi.studio.services.agents")\n': 0,
        'logging.getLogger("lionagi.studio")\n': 0,
    }
    for source, expected in cases.items():
        probe.write_text("import logging\n" + source, encoding="utf-8")
        assert len(_wrong_logger_names(probe, tmp_path)) == expected, source
