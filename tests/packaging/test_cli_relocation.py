# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The CLI body lives at ``lionhub.serve.cli`` and ``lionagi.cli`` keeps working through 1.x.

ADR-0139 step 6 (Amendment 5 item 3, D1, D2 and D4): the installed ``li`` script names the
new module, both module names run the CLI in a fresh process, and the more specific
``lionagi.cli.*`` rows keep their own destinations next to the parent row.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from lionagi import _moved

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
OLD, NEW = "lionagi.cli", "lionhub.serve.cli"
SPECIFIC_ROWS = tuple(row for row in _moved.MOVED if row.old.startswith(OLD + "."))


def _run(args: list[str], home: Path) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("LIONAGI_") and key != "PYTHONWARNINGS"
    }
    env.update(HOME=str(home), LIONAGI_HOME=str(home / "lionagi"))
    # The working directory is outside the checkout, so the installed packages are the ones run.
    return subprocess.run(  # noqa: S603 -- this interpreter, fixed arguments
        [sys.executable, *args],
        cwd=home,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_cli_canonical_target():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["scripts"]["li"] == f"{NEW}.main:main"
    (installed,) = [e for e in entry_points(group="console_scripts") if e.name == "li"]
    assert installed.value == f"{NEW}.main:main"
    assert installed.load() is importlib.import_module(f"{NEW}.main").main


@pytest.mark.parametrize(
    ("argv", "status"),
    [
        (["--version"], 0),
        (["--help"], 0),
        (["kill", "--help"], 0),
        (["definitely-not-a-command"], 2),
    ],
    ids=lambda value: " ".join(value) if isinstance(value, list) else str(value),
)
def test_cli_module_invocations_in_fresh_processes(argv, status, tmp_path):
    new = _run(["-m", NEW, *argv], tmp_path)
    old = _run(["-m", OLD, *argv], tmp_path)
    assert new.returncode == status, new.stderr[-2000:]
    assert old.returncode == status, old.stderr[-2000:]
    if status == 0:
        assert new.stdout
    assert old.stdout == new.stdout
    assert "DeprecationWarning" not in new.stderr
    # The old name adds one deprecation warning, which names the new module, and the
    # source line the warning quotes; the rest of stderr is the new name's.
    lines = old.stderr.splitlines()
    marks = [index for index, line in enumerate(lines) if "DeprecationWarning" in line]
    assert len(marks) == 1, old.stderr
    (mark,) = marks
    assert f"moved to {NEW}." in lines[mark]
    assert lines[mark + 1].startswith("  ")
    assert lines[:mark] + lines[mark + 2 :] == new.stderr.splitlines()


def test_every_specific_cli_row_keeps_its_destination_next_to_the_parent_row():
    parent = _moved._row(OLD)
    assert parent is not None
    assert (parent.old, parent.new) == (OLD, NEW)
    assert SPECIFIC_ROWS
    for row in SPECIFIC_ROWS:
        assert _moved._row(row.old) == row
        assert _moved.resolve(row.old) == row.new
        assert _moved.resolve(row.old + "_probe") == NEW + row.old[len(OLD) :] + "_probe"


_ORDER_SCRIPT = """
import importlib, importlib.util, json, sys, warnings
warnings.simplefilter("ignore", DeprecationWarning)
order, rows = sys.argv[1], json.loads(sys.argv[2])
old_root, new_root = "lionagi.cli", "lionhub.serve.cli"
bad = []
if order == "parent-first":
    importlib.import_module(old_root + ".main")
for old, new in rows:
    module = importlib.import_module(old)
    if module is not importlib.import_module(new) or module.__name__ != new:
        bad.append([old, "is not " + new])
    twin = new_root + old[len(old_root):]
    if importlib.util.find_spec(twin) is not None:
        bad.append([old, "has a module at " + twin])
if order == "rows-first":
    importlib.import_module(old_root + ".main")
    for old, new in rows:
        if sys.modules[old] is not sys.modules[new]:
            bad.append([old, "changed after the parent loaded"])
if sys.modules[old_root] is not sys.modules[new_root]:
    bad.append([old_root, "is not " + new_root])
print(json.dumps(bad))
"""


@pytest.mark.parametrize("order", ["parent-first", "rows-first"])
def test_specific_cli_rows_survive_parent_move(order, tmp_path):
    """Each specific row in both import orders against the parent row.

    ``parent-first`` loads the CLI's composition root through the old name before any
    specific row; ``rows-first`` imports every specific row before it. Importing a row
    always loads the parent package itself, so the order is about the CLI modules.
    """
    rows = json.dumps([[row.old, row.new] for row in SPECIFIC_ROWS])
    result = _run(["-c", _ORDER_SCRIPT, order, rows], tmp_path)
    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout.splitlines()[-1]) == []
