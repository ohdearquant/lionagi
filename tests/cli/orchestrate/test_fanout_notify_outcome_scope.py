# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A settings-driven `notify.on_terminal` exec adapter fired by a fan-out run
must have its outcome attributed to that run, matching the flow path."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from lionagi.cli.orchestrate import fanout as fanout_module
from lionagi.state.lifecycle.callbacks import DEFAULT_TERMINAL_CALLBACKS
from lionagi.state.lifecycle.notify_settings import register_settings_terminal_callback

from .test_fanout_artifacts import _fanout_env


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


def _capture_command(out_file: Path) -> list[str]:
    return shlex.split(
        f"{shlex.quote(sys.executable)} -c "
        '"import pathlib, sys; '
        "data = sys.stdin.read(); "
        'pathlib.Path(sys.argv[1]).write_text(data)" '
        f"{shlex.quote(str(out_file))}"
    )


def _write_project_settings(project_dir: Path, argv: list[str]) -> None:
    lionagi_dir = project_dir / ".lionagi"
    lionagi_dir.mkdir(parents=True, exist_ok=True)
    (lionagi_dir / "settings.yaml").write_text(
        yaml.dump(
            {
                "notify": {
                    "on_terminal": {
                        "enabled": True,
                        "adapter": {"kind": "exec", "argv": argv},
                    }
                }
            }
        )
    )


async def _run_settings_only_fanout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, notify: str | None = None
):
    """Fan-out with a settings-configured exec adapter and no `--notify`,
    short-circuited to an empty plan so only the terminal path runs."""
    monkeypatch.setenv("HOME", str(tmp_path / "isolated_home"))
    env, run, _session = _fanout_env(tmp_path)
    out_file = tmp_path / "captured.json"
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _write_project_settings(repo_dir, _capture_command(out_file))

    monkeypatch.setattr(fanout_module, "setup_orchestration", AsyncMock(return_value=env))
    monkeypatch.setattr(fanout_module, "plan", AsyncMock(return_value=[]))

    assert register_settings_terminal_callback(project_dir=str(repo_dir)) is True
    try:
        _result, status = await fanout_module._run_fanout(
            "codex/model",
            "prompt",
            cwd=str(repo_dir),
            notify=notify,
        )
    finally:
        DEFAULT_TERMINAL_CALLBACKS.unregister("notify.settings.on_terminal")
    return env, run, out_file, status


async def test_settings_only_fanout_records_adapter_outcome_against_its_run(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The adapter fires and its exec outcome lands in this run's own
    notify_outcome.json — the run-bound scope is what attributes it."""
    _env, run, out_file, status = await _run_settings_only_fanout(tmp_path, monkeypatch)

    assert status == "completed"
    payload = json.loads(out_file.read_text())
    assert payload["entity"]["kind"] == "session"
    assert run.notify_outcome_path.exists(), "adapter outcome was not attributed to the run"
    outcome = json.loads(run.notify_outcome_path.read_text())
    assert outcome["ok"] is True
    assert outcome["exit_code"] == 0


async def test_fanout_notify_override_owns_delivery_without_outcome_scope(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`--notify` is an exclusive override for the same entity, so the
    run-bound settings scope is skipped and the settings adapter does not
    fire — registering both would deliver twice for one event."""
    hook_out = tmp_path / "hook.txt"
    _env, run, out_file, status = await _run_settings_only_fanout(
        tmp_path,
        monkeypatch,
        notify=f"{shlex.quote(sys.executable)} -c "
        '"import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])" '
        f"{shlex.quote(str(hook_out))} {{status}}",
    )

    assert status == "completed"
    assert hook_out.read_text() == "completed"
    assert not out_file.exists()
    assert not run.notify_outcome_path.exists()


async def test_fanout_terminal_notify_scopes_are_unregistered_after_the_run(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Both scoped registrations are torn down, so a later run in the same
    process can never inherit this run's outcome binding."""
    env, _run, _out_file, _status = await _run_settings_only_fanout(tmp_path, monkeypatch)

    session_id = str(env.session.id)
    assert f"notify.settings.on_terminal.session.{session_id}" not in DEFAULT_TERMINAL_CALLBACKS
    assert f"notify.flow.session.{session_id}" not in DEFAULT_TERMINAL_CALLBACKS
