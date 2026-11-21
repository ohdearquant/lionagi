# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""``python -m lionagi.studio`` still starts the application's server, through the old name."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lionagi_studio.config import HOST, STUDIO_PORT
from lionhub.serve.daemon.settings import _SHUTDOWN_GRACE_SECONDS

# The only attribute the entry module reads from uvicorn is ``run``. The stub records the
# call instead of serving, so the test runs the real entry module and import path.
_STUB_UVICORN = """
import json, os, sys


def run(app, **kwargs):
    application = sys.modules.get("lionagi_studio.app")
    record = {
        "app_is_the_application": application is not None and app is application.app,
        "kwargs": kwargs,
        "main_file": sys.modules["__main__"].__file__,
    }
    with open(os.environ["STUB_UVICORN_RECORD"], "w", encoding="utf-8") as handle:
        json.dump(record, handle)
"""


@pytest.mark.parametrize("old_name", ["lionagi.studio", "lionagi_studio"])
def test_python_dash_m_runs_the_application_entry(tmp_path: Path, old_name: str) -> None:
    (tmp_path / "uvicorn.py").write_text(_STUB_UVICORN, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    record = tmp_path / "record.json"
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONWARNINGS", "LIONAGI_STATE_DB_URL"}
    }
    env.update(HOME=str(home), LIONAGI_HOME=str(home / ".lionagi"), STUB_UVICORN_RECORD=str(record))

    # ``-m`` puts the working directory first on sys.path, so the stub shadows uvicorn.
    result = subprocess.run(  # noqa: S603 -- this interpreter and the package under test
        [sys.executable, "-B", "-m", old_name],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    called = json.loads(record.read_text(encoding="utf-8"))
    assert called["app_is_the_application"] is True
    assert called["kwargs"] == {
        "host": HOST,
        "port": STUDIO_PORT,
        "timeout_graceful_shutdown": _SHUTDOWN_GRACE_SECONDS,
    }
    assert Path(called["main_file"]).parts[-3:] == ("src", "lionagi_studio", "__main__.py")

    warnings = [line for line in result.stderr.splitlines() if "DeprecationWarning:" in line]
    if old_name == "lionagi_studio":
        assert warnings == [], result.stderr
    else:
        assert len(warnings) == 1, result.stderr
        assert warnings[0].startswith(
            "<command line>:1: DeprecationWarning: lionagi.studio.__main__ moved to "
            "lionagi_studio.__main__; the old import path is removed in lionagi 2.0.0."
        ), warnings[0]


@pytest.mark.parametrize("entry", ["permission_mcp", "application_mcp"])
def test_an_operator_entry_run_by_its_old_name_warns_once(tmp_path: Path, entry: str) -> None:
    """Run by its old name, an Operator server entry warns for that name alone.

    ``runpy`` gives the entry the old package as ``__package__``, so an import relative to it
    would go through further old names and warn for each. Stdin is at its end, so the server
    returns at once.
    """
    home = tmp_path / "home"
    home.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONWARNINGS", "LIONAGI_STATE_DB_URL"}
    }
    env.update(HOME=str(home), LIONAGI_HOME=str(home / ".lionagi"))
    old_name = f"lionagi.studio.operator.{entry}"

    result = subprocess.run(  # noqa: S603 -- this interpreter and the package under test
        [sys.executable, "-B", "-m", old_name],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    warnings = [line for line in result.stderr.splitlines() if "DeprecationWarning:" in line]
    assert len(warnings) == 1, result.stderr
    assert (
        f"DeprecationWarning: {old_name} moved to lionagi_studio.operator.{entry};" in warnings[0]
    )
