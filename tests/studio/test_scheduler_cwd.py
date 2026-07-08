# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Regression tests: scheduled subprocess spawns must not inherit the daemon's
own launch cwd. Covers spawn_and_wait's cwd passthrough and the
_resolve_action_cwd layered resolution (action_project -> LIONAGI_SCHEDULER_CWD
-> None+warning)."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# _resolve_action_cwd's action_project branch imports lionagi.studio.services.projects,
# which requires fastapi (the `studio` extra); skip gracefully in a bare-core install.
pytest.importorskip("fastapi", reason="studio extra not installed")

# ---------------------------------------------------------------------------
# spawn_and_wait: cwd passthrough to create_subprocess_exec
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_and_wait_passes_cwd_through(tmp_path):
    """cwd kwarg reaches asyncio.create_subprocess_exec unchanged."""
    from lionagi.studio.scheduler.subprocess import spawn_and_wait

    target_cwd = str(tmp_path)
    captured: dict = {}

    with patch("lionagi.studio.scheduler.subprocess.asyncio.create_subprocess_exec") as mock_exec:
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        async def _fake_exec(*args, **kwargs):
            captured.update(kwargs)
            return mock_proc

        mock_exec.side_effect = _fake_exec

        exit_code, _ = await spawn_and_wait(
            ["uv", "run", "li", "agent"], "inv-cwd-001", cwd=target_cwd
        )

    assert exit_code == 0
    assert captured.get("cwd") == target_cwd


@pytest.mark.asyncio
async def test_spawn_and_wait_default_cwd_is_none():
    """Omitting cwd preserves the pre-existing inherit-cwd behavior."""
    from lionagi.studio.scheduler.subprocess import spawn_and_wait

    captured: dict = {}

    with patch("lionagi.studio.scheduler.subprocess.asyncio.create_subprocess_exec") as mock_exec:
        mock_proc = MagicMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        async def _fake_exec(*args, **kwargs):
            captured.update(kwargs)
            return mock_proc

        mock_exec.side_effect = _fake_exec

        await spawn_and_wait(["uv", "run", "li", "agent"], "inv-cwd-002")

    assert captured.get("cwd") is None


# ---------------------------------------------------------------------------
# _resolve_action_cwd
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_action_cwd_uses_registered_project_path(tmp_path, monkeypatch):
    """action_project resolves to that project's stored path, even when
    os.getcwd() is a completely different directory (the daemon-started-
    elsewhere case this bug is about)."""
    from lionagi.studio.scheduler.engine import _resolve_action_cwd

    project_dir = tmp_path / "registered-project"
    project_dir.mkdir()

    fake_get_project = AsyncMock(
        return_value={"name": "myproj", "path": str(project_dir), "source": "studio"}
    )
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )

    other_cwd = tmp_path / "somewhere-else-entirely"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    schedule = {"id": "sched-1", "action_project": "myproj"}
    result = await _resolve_action_cwd(schedule)

    assert result == (str(project_dir), None)
    fake_get_project.assert_awaited_once_with("myproj")


@pytest.mark.asyncio
async def test_resolve_action_cwd_falls_back_to_env_when_project_unresolved(tmp_path, monkeypatch):
    """No project match (or none set) but LIONAGI_SCHEDULER_CWD points at a
    real directory -> that directory is used."""
    from lionagi.studio.scheduler.engine import _resolve_action_cwd

    fake_get_project = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )

    env_dir = tmp_path / "env-fallback"
    env_dir.mkdir()
    monkeypatch.setenv("LIONAGI_SCHEDULER_CWD", str(env_dir))

    schedule = {"id": "sched-2", "action_project": "unknown-project"}
    result = await _resolve_action_cwd(schedule)

    assert result == (str(env_dir), None)


@pytest.mark.asyncio
async def test_resolve_action_cwd_env_fallback_when_no_project_set(monkeypatch, tmp_path):
    """action_project unset entirely -> env fallback still applies, and
    get_project is never called."""
    from lionagi.studio.scheduler.engine import _resolve_action_cwd

    fake_get_project = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )

    env_dir = tmp_path / "env-only"
    env_dir.mkdir()
    monkeypatch.setenv("LIONAGI_SCHEDULER_CWD", str(env_dir))

    schedule = {"id": "sched-3", "action_project": None}
    result = await _resolve_action_cwd(schedule)

    assert result == (str(env_dir), None)
    fake_get_project.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_action_cwd_returns_none_and_warns_when_unresolved(monkeypatch, caplog):
    """Neither action_project nor LIONAGI_SCHEDULER_CWD resolve -> None,
    with a warning naming the schedule id and action_project."""
    from lionagi.studio.scheduler.engine import _resolve_action_cwd

    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    schedule = {"id": "sched-4", "action_project": None}
    with caplog.at_level(logging.WARNING, logger="lionagi.studio.scheduler.engine"):
        result = await _resolve_action_cwd(schedule)

    assert result == (None, None)
    assert any("sched-4" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_resolve_action_cwd_ignores_project_with_nonexistent_path(monkeypatch, tmp_path):
    """A registered project whose stored path no longer exists on disk must
    not be trusted; falls through to env/None, and the caller learns which
    path was stale so it can attribute a later spawn failure to it."""
    from lionagi.studio.scheduler.engine import _resolve_action_cwd

    fake_get_project = AsyncMock(
        return_value={"name": "stale", "path": "/no/such/directory/at/all", "source": "studio"}
    )
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )
    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    schedule = {"id": "sched-5", "action_project": "stale"}
    result = await _resolve_action_cwd(schedule)

    assert result == (None, "/no/such/directory/at/all")


@pytest.mark.asyncio
async def test_resolve_action_cwd_stale_project_path_logs_specific_warning(monkeypatch, caplog):
    """The stale-project-path warning names the schedule, the project, and
    the exact missing path -- not just the generic 'no resolvable cwd'."""
    from lionagi.studio.scheduler.engine import _resolve_action_cwd

    fake_get_project = AsyncMock(
        return_value={"name": "stale", "path": "/pruned/worktree/xyz", "source": "studio"}
    )
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )
    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    schedule = {"id": "sched-6", "action_project": "stale"}
    with caplog.at_level(logging.WARNING, logger="lionagi.studio.scheduler.engine"):
        await _resolve_action_cwd(schedule)

    assert any(
        "sched-6" in rec.getMessage() and "/pruned/worktree/xyz" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_resolve_action_cwd_stale_project_path_overridden_by_env_fallback(
    monkeypatch, tmp_path
):
    """A stale project path is not reported as the failure attribution when
    LIONAGI_SCHEDULER_CWD resolves a usable directory anyway -- the run isn't
    actually at risk of the missing-cwd failure mode in that case."""
    from lionagi.studio.scheduler.engine import _resolve_action_cwd

    fake_get_project = AsyncMock(
        return_value={"name": "stale", "path": "/no/such/directory/at/all", "source": "studio"}
    )
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )
    env_dir = tmp_path / "env-saves-the-day"
    env_dir.mkdir()
    monkeypatch.setenv("LIONAGI_SCHEDULER_CWD", str(env_dir))

    schedule = {"id": "sched-7", "action_project": "stale"}
    result = await _resolve_action_cwd(schedule)

    assert result == (str(env_dir), None)


# ---------------------------------------------------------------------------
# _fire() threads the resolved cwd into spawn_and_wait
# ---------------------------------------------------------------------------


def _minimal_schedule(**overrides) -> dict:
    base = {
        "id": "sched-fire-cwd",
        "name": "test-sched-cwd",
        "trigger_type": "cron",
        "cron_expr": "0 * * * *",
        "action_kind": "agent",
        "action_model": "gpt-4.1-mini",
        "action_prompt": "ping",
        "action_agent": None,
        "action_playbook": None,
        "action_project": None,
        "action_extra_args": [],
        "action_flow_yaml": None,
        "on_success": None,
        "on_fail": None,
        "overlap_policy": "skip",
        "missed_fire_policy": "skip",
    }
    base.update(overrides)
    return base


def _make_svc() -> AsyncMock:
    svc = AsyncMock()
    svc.get_schedule = AsyncMock(return_value=None)
    svc.list_schedules = AsyncMock(return_value=[])
    svc.update_schedule = AsyncMock()
    svc.create_schedule_run = AsyncMock()
    svc.update_schedule_run = AsyncMock()
    svc.create_invocation = AsyncMock()
    svc.update_invocation = AsyncMock()
    svc.update_status = AsyncMock()
    svc.list_sessions_for_invocation = AsyncMock(return_value=[])
    return svc


@pytest.mark.asyncio
async def test_fire_threads_resolved_cwd_into_spawn_and_wait(tmp_path, monkeypatch):
    """SchedulerEngine._fire resolves action_project to a real path and passes
    it as cwd= to spawn_and_wait, regardless of the process's own os.getcwd()."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    fake_get_project = AsyncMock(return_value={"name": "p1", "path": str(project_dir)})
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(action_project="p1")

    spawn_mock = AsyncMock(return_value=(0, ""))
    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch("lionagi.studio.scheduler.subprocess.spawn_and_wait", new=spawn_mock),
    ):
        await engine._fire(schedule, "run-cwd-001", trigger_context={"scheduled": True})

    spawn_mock.assert_awaited_once()
    _args, kwargs = spawn_mock.await_args
    assert kwargs.get("cwd") == str(project_dir)


# ---------------------------------------------------------------------------
# Status-reason attribution: a stale action_project path that later fails to
# spawn is recorded with a specific reason_code, not a generic non-zero exit.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_attributes_failure_to_missing_cwd(monkeypatch):
    """A schedule whose registered project path no longer exists, and whose
    process then exits non-zero (the daemon-inherited cwd wasn't a fit),
    writes RunReasons.FAILED_MISSING_CWD instead of the generic
    FAILED_EXIT_NONZERO -- so the failure is attributable without reading a
    stack trace."""
    from lionagi.state.reasons import RunReasons
    from lionagi.studio.scheduler.engine import SchedulerEngine

    fake_get_project = AsyncMock(return_value={"name": "gone", "path": "/no/such/directory/at/all"})
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )
    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule(action_project="gone")

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(1, "boom")),
        ),
    ):
        await engine._fire(schedule, "run-cwd-002", trigger_context={"scheduled": True})

    terminal_calls = [
        c
        for c in svc.update_status.await_args_list
        if c.args[:2] == ("schedule_run", "run-cwd-002")
        and c.kwargs.get("new_status") in ("completed", "failed")
    ]
    assert terminal_calls
    (call,) = terminal_calls
    assert call.kwargs["reason_code"] == RunReasons.FAILED_MISSING_CWD
    assert "/no/such/directory/at/all" in call.kwargs["reason_summary"]


@pytest.mark.asyncio
async def test_fire_plain_nonzero_exit_keeps_generic_reason(monkeypatch):
    """A schedule with no action_project (or a healthy one) that exits
    non-zero keeps the pre-existing generic FAILED_EXIT_NONZERO reason --
    this rewrite must not misattribute ordinary failures to a missing cwd."""
    from lionagi.state.reasons import RunReasons
    from lionagi.studio.scheduler.engine import SchedulerEngine

    svc = _make_svc()
    engine = SchedulerEngine(svc=svc)
    schedule = _minimal_schedule()  # action_project=None

    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch(
            "lionagi.studio.scheduler.subprocess.spawn_and_wait",
            new=AsyncMock(return_value=(1, "boom")),
        ),
    ):
        await engine._fire(schedule, "run-cwd-003", trigger_context={"scheduled": True})

    terminal_calls = [
        c
        for c in svc.update_status.await_args_list
        if c.args[:2] == ("schedule_run", "run-cwd-003")
        and c.kwargs.get("new_status") in ("completed", "failed")
    ]
    assert terminal_calls
    (call,) = terminal_calls
    assert call.kwargs["reason_code"] == RunReasons.FAILED_EXIT_NONZERO
