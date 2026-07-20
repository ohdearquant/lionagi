# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""_run_flow's terminal-notify wiring: `--notify` registers
scoped compatibility sugar over the terminal-callback registry rather than
firing a direct call, and the registered handler fires from the same guarded
lifecycle transition that persists the run's own terminal status -- so a
hook failure can never affect the run's own returned output/status."""

from __future__ import annotations

import json
import shlex
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
import yaml

from lionagi import Branch, Session
from lionagi.cli.orchestrate._orchestration import OrchestrationEnv
from lionagi.cli.orchestrate.flow import _run_flow
from lionagi.state.db import StateDB

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


async def _make_invocation(db_path: Path, invocation_id: str) -> None:
    """A real 'running' invocation row so the finally block's
    update_status('invocation', ...) is a genuine guarded transition (and
    therefore actually pushes a terminal envelope through the registry)."""
    db = StateDB(str(db_path))
    await db.open()
    try:
        await db.create_invocation(
            {"id": invocation_id, "skill": "flow", "started_at": time.time(), "status": "running"}
        )
    finally:
        await db.close()


def _make_env(tmp_path: Path) -> OrchestrationEnv:
    orc_branch = Branch(name="orchestrator")
    session = Session(default_branch=orc_branch)
    run = SimpleNamespace(
        run_id="run-test-1",
        artifact_root=tmp_path / "artifacts",
        write_manifest=lambda data: None,
    )
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
        '"import pathlib, sys; '
        "data = sys.stdin.read(); "
        'pathlib.Path(sys.argv[1]).write_text(data)" '
        f"{shlex.quote(str(out_file))}"
    )


def _write_project_settings(project_dir: Path, on_terminal) -> None:
    lionagi_dir = project_dir / ".lionagi"
    lionagi_dir.mkdir(parents=True, exist_ok=True)
    (lionagi_dir / "settings.yaml").write_text(yaml.dump({"notify": {"on_terminal": on_terminal}}))


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_run_flow_notify_fires_with_correct_legacy_payload(
    temp_db_path: Path, tmp_path: Path
):
    """`--notify` must receive the run's own invocation_id/kind/playbook/
    status/save_dir/cwd/exit_class/timestamps, sourced from the real
    guarded invocation-status transition -- not recomputed or guessed."""
    env = _make_env(tmp_path)
    invocation_id = str(uuid4())
    await _make_invocation(temp_db_path, invocation_id)
    save_dir = str(tmp_path / "saves")
    cwd = str(tmp_path / "repo")
    out_file = tmp_path / "captured.json"

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
            save_dir=save_dir,
            cwd=cwd,
            invocation_id=invocation_id,
            playbook_name=None,
            notify=_capture_command(out_file),
        )

    assert result == "ok result"
    assert terminal_status == "completed"
    payload = json.loads(out_file.read_text())
    assert payload["invocation_id"] == invocation_id
    assert payload["kind"] == "flow"
    assert payload["playbook"] is None
    assert payload["status"] == "completed"
    assert payload["save_dir"] == save_dir
    assert payload["cwd"] == cwd
    assert payload["exit_class"] == "success"
    assert payload["started_at"] <= payload["ended_at"]


async def test_run_flow_reports_kind_play_with_playbook_name(temp_db_path: Path, tmp_path: Path):
    env = _make_env(tmp_path)
    invocation_id = str(uuid4())
    await _make_invocation(temp_db_path, invocation_id)
    out_file = tmp_path / "captured.json"

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
        await _run_flow(
            "claude",
            "do the thing",
            invocation_id=invocation_id,
            playbook_name="ship",
            notify=_capture_command(out_file),
        )

    payload = json.loads(out_file.read_text())
    assert payload["kind"] == "play"
    assert payload["playbook"] == "ship"


async def test_run_flow_fires_real_hook_with_settings_configured_command(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """End-to-end: a settings-configured notify.on_terminal (no --notify
    flag) still fires via the process-wide settings bootstrap, receiving
    the new minimal envelope shape."""
    monkeypatch.setenv("HOME", str(tmp_path / "isolated_home"))
    env = _make_env(tmp_path)
    invocation_id = str(uuid4())
    await _make_invocation(temp_db_path, invocation_id)
    out_file = tmp_path / "captured.json"
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _write_project_settings(
        repo_dir,
        {
            "enabled": True,
            "adapter": {"kind": "exec", "argv": shlex.split(_capture_command(out_file))},
        },
    )

    from lionagi.state.lifecycle.notify_settings import register_settings_terminal_callback

    installed = register_settings_terminal_callback(project_dir=str(repo_dir))
    assert installed is True
    try:
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
    finally:
        from lionagi.state.lifecycle.callbacks import DEFAULT_TERMINAL_CALLBACKS

        DEFAULT_TERMINAL_CALLBACKS.unregister("notify.settings.on_terminal")

    assert result == "ok"
    assert terminal_status == "completed"
    payload = json.loads(out_file.read_text())
    assert payload["schema"] == "lionagi.run-terminal"
    assert payload["entity"] == {"kind": "invocation", "id": invocation_id}
    assert payload["terminal_status"] == "completed"


async def test_run_flow_notify_flag_overrides_settings_handler_for_this_run_only(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The P1 fix, end to end: when a process-wide settings handler is
    already bootstrapped, `--notify` on one run must replace it for that
    run's own invocation entity only -- not fire both for the SAME
    invocation event. A tracked run's teardown also independently finalizes
    its own session entity (a session-shutdown terminal transition unrelated
    to `--notify`'s scope), which the unscoped settings handler legitimately
    still receives -- that is a different entity, not a double-fire of the
    invocation event the override targets."""
    monkeypatch.setenv("HOME", str(tmp_path / "isolated_home"))
    env = _make_env(tmp_path)
    invocation_id = str(uuid4())
    await _make_invocation(temp_db_path, invocation_id)
    settings_out = tmp_path / "settings.json"
    override_out = tmp_path / "override.json"
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _write_project_settings(
        repo_dir,
        {
            "enabled": True,
            "adapter": {"kind": "exec", "argv": shlex.split(_capture_command(settings_out))},
        },
    )

    from lionagi.state.lifecycle.notify_settings import register_settings_terminal_callback

    installed = register_settings_terminal_callback(project_dir=str(repo_dir))
    assert installed is True
    try:
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
                notify=_capture_command(override_out),
            )
    finally:
        from lionagi.state.lifecycle.callbacks import DEFAULT_TERMINAL_CALLBACKS

        DEFAULT_TERMINAL_CALLBACKS.unregister("notify.settings.on_terminal")

    assert result == "ok"
    assert terminal_status == "completed"
    assert override_out.exists()  # --notify's legacy adapter fired
    override_payload = json.loads(override_out.read_text())
    assert override_payload["invocation_id"] == invocation_id

    # The settings handler still fires for the run's separate session-level
    # terminal transition (a different entity `--notify` never scoped to),
    # but it must never receive the invocation's own event -- that one was
    # replaced by the override for this run's scope.
    if settings_out.exists():
        settings_payload = json.loads(settings_out.read_text())
        assert settings_payload["entity"]["kind"] != "invocation"


async def test_run_flow_swallows_failing_hook_and_keeps_real_terminal_status(
    temp_db_path: Path, tmp_path: Path
):
    """A hook that exits nonzero must not change the run's own returned
    output/terminal_status, and must not raise out of _run_flow."""
    env = _make_env(tmp_path)
    invocation_id = str(uuid4())
    await _make_invocation(temp_db_path, invocation_id)
    failing_cmd = f'{shlex.quote(sys.executable)} -c "import sys; sys.exit(1)"'

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
            invocation_id=invocation_id,
            notify=failing_cmd,
        )

    assert result == "ok result"
    assert terminal_status == "completed"


async def test_run_flow_swallows_hook_timeout(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import lionagi.state.lifecycle.notify_settings as notify_settings_mod

    monkeypatch.setattr(notify_settings_mod, "HANDLER_BUDGET_SECONDS", 0.2)

    env = _make_env(tmp_path)
    invocation_id = str(uuid4())
    await _make_invocation(temp_db_path, invocation_id)
    slow_cmd = f'{shlex.quote(sys.executable)} -c "import time; time.sleep(5)"'

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
            invocation_id=invocation_id,
            notify=slow_cmd,
        )

    assert result == "ok result"
    assert terminal_status == "completed"


async def test_run_flow_no_hook_configured_is_a_noop(
    temp_db_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "isolated_home"))
    env = _make_env(tmp_path)
    invocation_id = str(uuid4())
    await _make_invocation(temp_db_path, invocation_id)

    with (
        patch(
            "lionagi.cli.orchestrate.flow.setup_orchestration",
            AsyncMock(return_value=env),
        ),
        patch(
            "lionagi.cli.orchestrate.flow._run_flow_inner",
            AsyncMock(return_value="ok result"),
        ),
        patch("asyncio.create_subprocess_exec") as spawn,
        patch("asyncio.create_subprocess_shell") as spawn_shell,
    ):
        result, terminal_status = await _run_flow(
            "claude",
            "do the thing",
            invocation_id=invocation_id,
        )

    assert result == "ok result"
    assert terminal_status == "completed"
    spawn.assert_not_called()
    spawn_shell.assert_not_called()


async def test_run_flow_notify_still_fires_when_invocation_finalize_raises(
    temp_db_path: Path, tmp_path: Path
):
    """Regression: when `_resolve_invocation_terminal_flow` (or the
    subsequent `update_status('invocation', ...)` write) raises, the guarded
    lifecycle transition never commits and so never pushes a terminal
    envelope through the registry for this invocation's entity -- but the
    `--notify` hook explicitly scoped to that entity must still fire,
    reporting the flow's own already-computed terminal status, not be
    silently dropped."""
    env = _make_env(tmp_path)
    invocation_id = str(uuid4())
    await _make_invocation(temp_db_path, invocation_id)
    out_file = tmp_path / "captured.json"

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
            "lionagi.cli.orchestrate.flow._resolve_invocation_terminal_flow",
            AsyncMock(side_effect=RuntimeError("db hiccup during invocation finalize")),
        ),
    ):
        result, terminal_status = await _run_flow(
            "claude",
            "do the thing",
            invocation_id=invocation_id,
            notify=_capture_command(out_file),
        )

    # The run's own outcome must be unaffected by the finalize failure.
    assert result == "ok result"
    assert terminal_status == "completed"

    assert out_file.exists(), "notify hook never fired despite invocation finalize raising"
    payload = json.loads(out_file.read_text())
    assert payload["invocation_id"] == invocation_id
    assert payload["status"] == "completed"


async def test_fallback_terminal_envelope_has_last_known_previous_status(
    temp_db_path: Path, tmp_path: Path
):
    """A failed finalize still emits a transition-shaped envelope, not an initial row."""
    from lionagi.state.lifecycle.callbacks import DEFAULT_TERMINAL_CALLBACKS

    env = _make_env(tmp_path)
    invocation_id = str(uuid4())
    await _make_invocation(temp_db_path, invocation_id)
    emitted = []
    callback_name = f"test.fallback.{invocation_id}"
    DEFAULT_TERMINAL_CALLBACKS.register(
        callback_name,
        emitted.append,
        kinds=["invocation"],
        ids=[invocation_id],
    )

    try:
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
                "lionagi.cli.orchestrate.flow._resolve_invocation_terminal_flow",
                AsyncMock(side_effect=RuntimeError("finalize failed")),
            ),
        ):
            await _run_flow("claude", "do the thing", invocation_id=invocation_id)
    finally:
        DEFAULT_TERMINAL_CALLBACKS.unregister(callback_name)

    assert len(emitted) == 1
    assert emitted[0].previous_status == "running"
    assert emitted[0].previous_status is not None


async def test_run_flow_fires_hook_without_invocation_id(temp_db_path: Path, tmp_path: Path):
    """A `--notify` run with no --invocation scopes to the session id
    instead, and the payload's own invocation_id field stays null; no
    invocation-status DB write is attempted."""
    env = _make_env(tmp_path)
    out_file = tmp_path / "captured.json"

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
            invocation_id=None,
            notify=_capture_command(out_file),
        )

    assert result == "ok result"
    assert terminal_status == "completed"
    payload = json.loads(out_file.read_text())
    assert payload["invocation_id"] is None
    assert payload["status"] == "completed"
    assert payload["kind"] == "flow"
