# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""_run_flow's terminal-notify wiring: correct payload derivation from the finally
block, and containment of hook failures (they must never affect the run's own
terminal status/exit)."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import yaml

from lionagi import Branch, Session
from lionagi.cli.orchestrate._orchestration import OrchestrationEnv
from lionagi.cli.orchestrate.flow import _run_flow

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


def _make_env(tmp_path: Path) -> OrchestrationEnv:
    orc_branch = Branch(name="orchestrator")
    session = Session(default_branch=orc_branch)
    run = SimpleNamespace(run_id="run-test-1", artifact_root=tmp_path / "artifacts")
    return OrchestrationEnv(
        run=run,
        session=session,
        orc_branch=orc_branch,
        builder=MagicMock(),
        orc_profile=None,
        default_model_spec="claude",
        bare=False,
        effort=None,
        theme=None,
        yolo=False,
        bypass=False,
        verbose=False,
        fast=False,
        cwd=None,
    )


def _capture_command(out_file: Path) -> str:
    return (
        f"{shlex.quote(sys.executable)} -c "
        '"import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2])" '
        f"{shlex.quote(str(out_file))} '{{payload}}'"
    )


def _write_project_settings(project_dir: Path, on_terminal: str) -> None:
    lionagi_dir = project_dir / ".lionagi"
    lionagi_dir.mkdir(parents=True, exist_ok=True)
    (lionagi_dir / "settings.yaml").write_text(yaml.dump({"notify": {"on_terminal": on_terminal}}))


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_run_flow_derives_correct_notify_fields(temp_db_path: Path, tmp_path: Path):
    """The finally block must hand fire_terminal_notify the run's own
    invocation_id/kind/playbook/status/save_dir/cwd/exit_class/timestamps —
    not recomputed or guessed values."""
    env = _make_env(tmp_path)
    invocation_id = str(uuid4())
    save_dir = str(tmp_path / "saves")
    cwd = str(tmp_path / "repo")

    with (
        patch(
            "lionagi.cli.orchestrate.flow.setup_orchestration",
            AsyncMock(return_value=env),
        ),
        patch(
            "lionagi.cli.orchestrate.flow._run_flow_inner",
            AsyncMock(return_value="ok result"),
        ),
        patch(
            "lionagi.cli.orchestrate.flow.fire_terminal_notify",
            AsyncMock(),
        ) as notify_mock,
    ):
        result, terminal_status = await _run_flow(
            "claude",
            "do the thing",
            save_dir=save_dir,
            cwd=cwd,
            invocation_id=invocation_id,
            playbook_name=None,
            notify="echo override",
        )

    assert result == "ok result"
    assert terminal_status == "completed"
    notify_mock.assert_called_once()
    kw = notify_mock.call_args.kwargs
    assert kw["invocation_id"] == invocation_id
    assert kw["kind"] == "flow"
    assert kw["playbook"] is None
    assert kw["status"] == "completed"
    assert kw["save_dir"] == save_dir
    assert kw["cwd"] == cwd
    assert kw["exit_class"] == "success"
    assert kw["override_command"] == "echo override"
    assert kw["started_at"] <= kw["ended_at"]


async def test_run_flow_reports_kind_play_with_playbook_name(temp_db_path: Path, tmp_path: Path):
    env = _make_env(tmp_path)
    invocation_id = str(uuid4())

    with (
        patch(
            "lionagi.cli.orchestrate.flow.setup_orchestration",
            AsyncMock(return_value=env),
        ),
        patch(
            "lionagi.cli.orchestrate.flow._run_flow_inner",
            AsyncMock(return_value="ok"),
        ),
        patch(
            "lionagi.cli.orchestrate.flow.fire_terminal_notify",
            AsyncMock(),
        ) as notify_mock,
    ):
        await _run_flow(
            "claude",
            "do the thing",
            invocation_id=invocation_id,
            playbook_name="ship",
        )

    kw = notify_mock.call_args.kwargs
    assert kw["kind"] == "play"
    assert kw["playbook"] == "ship"


async def test_run_flow_fires_real_hook_with_settings_configured_template(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """End-to-end: settings-configured notify.on_terminal actually runs and
    receives the real resolved payload for this invocation."""
    monkeypatch.setenv("HOME", str(tmp_path / "isolated_home"))
    env = _make_env(tmp_path)
    invocation_id = str(uuid4())
    out_file = tmp_path / "captured.json"
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _write_project_settings(repo_dir, _capture_command(out_file))

    with (
        patch(
            "lionagi.cli.orchestrate.flow.setup_orchestration",
            AsyncMock(return_value=env),
        ),
        patch(
            "lionagi.cli.orchestrate.flow._run_flow_inner",
            AsyncMock(return_value="ok"),
        ),
    ):
        result, terminal_status = await _run_flow(
            "claude",
            "do the thing",
            cwd=str(repo_dir),
            invocation_id=invocation_id,
        )

    assert result == "ok"
    assert terminal_status == "completed"
    payload = json.loads(out_file.read_text())
    assert payload["invocation_id"] == invocation_id
    assert payload["status"] == "completed"
    assert payload["kind"] == "flow"
    assert payload["exit_class"] == "success"


async def test_run_flow_swallows_failing_hook_and_keeps_real_terminal_status(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A hook that exits nonzero must not change the run's own returned
    output/terminal_status, and must not raise out of _run_flow."""
    monkeypatch.setenv("HOME", str(tmp_path / "isolated_home"))
    env = _make_env(tmp_path)
    invocation_id = str(uuid4())
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    failing_cmd = f'{shlex.quote(sys.executable)} -c "import sys; sys.exit(1)"'
    _write_project_settings(repo_dir, failing_cmd)

    with (
        patch(
            "lionagi.cli.orchestrate.flow.setup_orchestration",
            AsyncMock(return_value=env),
        ),
        patch(
            "lionagi.cli.orchestrate.flow._run_flow_inner",
            AsyncMock(return_value="ok result"),
        ),
    ):
        result, terminal_status = await _run_flow(
            "claude",
            "do the thing",
            cwd=str(repo_dir),
            invocation_id=invocation_id,
        )

    assert result == "ok result"
    assert terminal_status == "completed"


async def test_run_flow_swallows_hook_timeout(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from lionagi.cli.orchestrate import _notify as notify_mod

    monkeypatch.setattr(notify_mod, "_HOOK_TIMEOUT", 0.2)
    monkeypatch.setenv("HOME", str(tmp_path / "isolated_home"))

    env = _make_env(tmp_path)
    invocation_id = str(uuid4())
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    slow_cmd = f'{shlex.quote(sys.executable)} -c "import time; time.sleep(5)"'
    _write_project_settings(repo_dir, slow_cmd)

    with (
        patch(
            "lionagi.cli.orchestrate.flow.setup_orchestration",
            AsyncMock(return_value=env),
        ),
        patch(
            "lionagi.cli.orchestrate.flow._run_flow_inner",
            AsyncMock(return_value="ok result"),
        ),
    ):
        result, terminal_status = await _run_flow(
            "claude",
            "do the thing",
            cwd=str(repo_dir),
            invocation_id=invocation_id,
        )

    assert result == "ok result"
    assert terminal_status == "completed"


async def test_run_flow_no_hook_configured_is_a_noop(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "isolated_home"))
    env = _make_env(tmp_path)
    invocation_id = str(uuid4())
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    with (
        patch(
            "lionagi.cli.orchestrate.flow.setup_orchestration",
            AsyncMock(return_value=env),
        ),
        patch(
            "lionagi.cli.orchestrate.flow._run_flow_inner",
            AsyncMock(return_value="ok result"),
        ),
        patch("asyncio.create_subprocess_shell") as spawn,
    ):
        result, terminal_status = await _run_flow(
            "claude",
            "do the thing",
            cwd=str(empty_dir),
            invocation_id=invocation_id,
        )

    assert result == "ok result"
    assert terminal_status == "completed"
    spawn.assert_not_called()
