# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Regression tests: scheduled subprocess spawns must not inherit the daemon's
own launch cwd. Covers spawn_and_wait's cwd passthrough and the
_resolve_action_cwd layered resolution (action_cwd -> action_project ->
LIONAGI_SCHEDULER_CWD -> fail-closed-or-inherit, ADR-0070 delta 1).

The fall-through tier is identity-aware: a schedule carrying an explicit
execution root (action_cwd or action_project) whose configured directories
have all gone stale fails closed (SchedulerCwdInheritRefusedError) rather than
silently inheriting the daemon's cwd and running under the daemon directory's
identity. A schedule with no execution root at all (a pre-migration row)
retains the legacy inherit-and-warn behavior."""

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

    assert result == str(project_dir)
    fake_get_project.assert_awaited_once_with("myproj")


@pytest.mark.asyncio
async def test_resolve_action_cwd_refuses_env_fallback_when_project_unresolved(
    tmp_path, monkeypatch
):
    """A schedule that names a project which does not resolve carries an
    explicit execution root, so it fails closed even though
    LIONAGI_SCHEDULER_CWD names a real directory: that directory is not the
    root this schedule configured, and running there would substitute it."""
    from lionagi.studio.scheduler.engine import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    fake_get_project = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )

    env_dir = tmp_path / "env-fallback"
    env_dir.mkdir()
    monkeypatch.setenv("LIONAGI_SCHEDULER_CWD", str(env_dir))

    schedule = {"id": "sched-2", "action_project": "unknown-project"}
    with pytest.raises(SchedulerCwdInheritRefusedError) as excinfo:
        await _resolve_action_cwd(schedule)

    assert excinfo.value.configured_root == "unknown-project"
    assert str(env_dir) not in str(excinfo.value)


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

    assert result == str(env_dir)
    fake_get_project.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_action_cwd_returns_none_and_warns_when_ownerless(monkeypatch, caplog):
    """An ownerless row (no action_cwd, no action_project) with nothing else
    resolvable -> None (legacy inherit), with a warning naming the schedule id.
    No execution root was configured, so there is no identity to protect."""
    from lionagi.studio.scheduler.engine import _resolve_action_cwd

    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    schedule = {"id": "sched-4", "action_project": None}
    with caplog.at_level(logging.WARNING, logger="lionagi.studio.scheduler.engine"):
        result = await _resolve_action_cwd(schedule)

    assert result is None
    assert any("sched-4" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_resolve_action_cwd_refuses_when_project_path_nonexistent(monkeypatch, tmp_path):
    """A registered project whose stored path no longer exists on disk must
    not be trusted; with nothing else resolvable the resolver fails closed
    rather than inheriting the daemon's cwd (which would run the action under
    the daemon directory's identity)."""
    from lionagi.studio.scheduler.engine import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    fake_get_project = AsyncMock(
        return_value={"name": "stale", "path": "/no/such/directory/at/all", "source": "studio"}
    )
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )
    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    schedule = {"id": "sched-5", "action_project": "stale"}
    with pytest.raises(SchedulerCwdInheritRefusedError) as excinfo:
        await _resolve_action_cwd(schedule)

    assert excinfo.value.configured_root == "stale"


@pytest.mark.asyncio
async def test_resolve_action_cwd_stale_project_path_logs_specific_warning(monkeypatch, caplog):
    """The stale-project-path warning names the schedule, the project, and
    the exact missing path -- not just the generic 'no resolvable cwd'."""
    from lionagi.studio.scheduler.engine import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    fake_get_project = AsyncMock(
        return_value={"name": "stale", "path": "/pruned/worktree/xyz", "source": "studio"}
    )
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )
    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    schedule = {"id": "sched-6", "action_project": "stale"}
    with caplog.at_level(logging.WARNING, logger="lionagi.studio.scheduler.engine"):
        with pytest.raises(SchedulerCwdInheritRefusedError):
            await _resolve_action_cwd(schedule)

    assert any(
        "sched-6" in rec.getMessage() and "/pruned/worktree/xyz" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_resolve_action_cwd_stale_project_path_refuses_despite_env_fallback(
    monkeypatch, tmp_path
):
    """A registered-but-stale project path still fails closed when
    LIONAGI_SCHEDULER_CWD names a usable directory. The env directory does not
    rescue the run: it is a different directory than the one the schedule
    configured, and spawning there succeeds silently rather than failing, which
    is the substitution this refusal exists to prevent."""
    from lionagi.studio.scheduler.engine import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    fake_get_project = AsyncMock(
        return_value={"name": "stale", "path": "/no/such/directory/at/all", "source": "studio"}
    )
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )
    env_dir = tmp_path / "env-does-not-save-the-day"
    env_dir.mkdir()
    monkeypatch.setenv("LIONAGI_SCHEDULER_CWD", str(env_dir))

    schedule = {"id": "sched-7", "action_project": "stale"}
    with pytest.raises(SchedulerCwdInheritRefusedError) as excinfo:
        await _resolve_action_cwd(schedule)

    assert excinfo.value.schedule_id == "sched-7"


@pytest.mark.asyncio
async def test_resolve_action_cwd_env_fallback_still_serves_ownerless_rows(monkeypatch, tmp_path):
    """The refusal must not swallow the env fallback for pre-migration rows:
    with no execution root configured at all there is nothing to substitute,
    so an operator-set directory is still honored."""
    from lionagi.studio.scheduler.engine import _resolve_action_cwd

    env_dir = tmp_path / "ownerless-env"
    env_dir.mkdir()
    monkeypatch.setenv("LIONAGI_SCHEDULER_CWD", str(env_dir))

    schedule = {"id": "sched-ownerless", "action_cwd": None, "action_project": None}
    assert await _resolve_action_cwd(schedule) == str(env_dir)


# ---------------------------------------------------------------------------
# _resolve_action_cwd: action_cwd (persisted execution root) outranks
# action_project (ADR-0070 delta 1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_action_cwd_prefers_persisted_root_over_action_project(tmp_path, monkeypatch):
    """A stored action_cwd wins even when action_project resolves to a
    different, equally-real directory -- the persisted root is a snapshot,
    not re-derived from the live project registry on every fire."""
    from lionagi.studio.scheduler.engine import _resolve_action_cwd

    root_dir = tmp_path / "persisted-root"
    root_dir.mkdir()
    project_dir = tmp_path / "registered-project"
    project_dir.mkdir()

    fake_get_project = AsyncMock(
        return_value={"name": "myproj", "path": str(project_dir), "source": "studio"}
    )
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )

    schedule = {"id": "sched-root-1", "action_cwd": str(root_dir), "action_project": "myproj"}
    result = await _resolve_action_cwd(schedule)

    assert result == str(root_dir)
    fake_get_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_action_cwd_survives_daemon_restart_elsewhere(tmp_path, monkeypatch):
    """The whole point of ADR-0070 delta 1: a persisted action_cwd resolves
    correctly no matter where the daemon process itself was started from."""
    from lionagi.studio.scheduler.engine import _resolve_action_cwd

    root_dir = tmp_path / "stable-root"
    root_dir.mkdir()
    daemon_started_here = tmp_path / "daemon-started-elsewhere"
    daemon_started_here.mkdir()
    monkeypatch.chdir(daemon_started_here)

    schedule = {"id": "sched-root-2", "action_cwd": str(root_dir), "action_project": None}
    result = await _resolve_action_cwd(schedule)

    assert result == str(root_dir)


@pytest.mark.asyncio
async def test_resolve_action_cwd_falls_back_from_stale_persisted_root_to_action_project(
    tmp_path, monkeypatch
):
    """A persisted action_cwd that no longer exists on disk (e.g. a pruned
    worktree) falls through to action_project rather than being trusted."""
    from lionagi.studio.scheduler.engine import _resolve_action_cwd

    project_dir = tmp_path / "registered-project"
    project_dir.mkdir()
    fake_get_project = AsyncMock(
        return_value={"name": "myproj", "path": str(project_dir), "source": "studio"}
    )
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )

    schedule = {
        "id": "sched-root-3",
        "action_cwd": "/pruned/execution/root",
        "action_project": "myproj",
    }
    result = await _resolve_action_cwd(schedule)

    assert result == str(project_dir)


@pytest.mark.asyncio
async def test_resolve_action_cwd_refuses_when_stale_persisted_root_and_nothing_else(
    monkeypatch,
):
    """A stale action_cwd with nothing else to fall back on fails closed: the
    schedule carries an execution root that cannot be honored, so inheriting
    the daemon's cwd (and its identity) is refused rather than performed."""
    from lionagi.studio.scheduler.engine import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    fake_get_project = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )
    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    schedule = {
        "id": "sched-root-4",
        "action_cwd": "/pruned/execution/root",
        "action_project": None,
    }
    with pytest.raises(SchedulerCwdInheritRefusedError) as excinfo:
        await _resolve_action_cwd(schedule)

    assert excinfo.value.configured_root == "/pruned/execution/root"
    assert "/pruned/execution/root" in str(excinfo.value)


@pytest.mark.asyncio
async def test_resolve_action_cwd_refuses_empty_string_action_cwd(monkeypatch):
    """A present-but-empty action_cwd is an execution root that carries no
    usable value; it must fail closed, not slip into the ownerless inherit
    branch. The refusal gate keys on ``is not None``, not truthiness."""
    from lionagi.studio.scheduler.engine import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    schedule = {"id": "sched-empty-cwd", "action_cwd": "", "action_project": None}
    with pytest.raises(SchedulerCwdInheritRefusedError):
        await _resolve_action_cwd(schedule)


@pytest.mark.asyncio
async def test_resolve_action_cwd_refuses_a_relative_persisted_root(monkeypatch):
    """A relative execution root resolves against the daemon's own cwd, so
    honoring one performs exactly the substitution this resolver refuses.
    "." is the case that always succeeds an existence check."""
    from lionagi.studio.scheduler.engine import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    fake_get_project = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )
    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    schedule = {"id": "sched-relative", "action_cwd": ".", "action_project": None}
    with pytest.raises(SchedulerCwdInheritRefusedError):
        await _resolve_action_cwd(schedule)


@pytest.mark.asyncio
async def test_resolve_action_cwd_refuses_a_relative_registered_project_path(monkeypatch):
    """Registered project paths are not validated when they are written, so a
    relative one reaches the resolver and must be rejected here."""
    from lionagi.studio.scheduler.engine import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    fake_get_project = AsyncMock(return_value={"name": "relproj", "path": ".", "source": "studio"})
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )
    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    schedule = {"id": "sched-relproj", "action_cwd": None, "action_project": "relproj"}
    with pytest.raises(SchedulerCwdInheritRefusedError):
        await _resolve_action_cwd(schedule)


@pytest.mark.asyncio
async def test_resolve_action_cwd_ignores_a_relative_env_fallback(monkeypatch):
    """The operator-set default gets the same test as everything else: a
    relative value there is the daemon's cwd under another name."""
    from lionagi.studio.scheduler.engine import _resolve_action_cwd

    monkeypatch.setenv("LIONAGI_SCHEDULER_CWD", ".")

    schedule = {"id": "sched-relenv", "action_cwd": None, "action_project": None}

    assert await _resolve_action_cwd(schedule) is None


@pytest.mark.asyncio
async def test_resolve_action_cwd_empty_root_falls_through_to_project_and_warns(
    tmp_path, monkeypatch, caplog
):
    """An empty action_cwd is an unusable execution root, so it falls through
    to action_project exactly like a pruned one -- and says so. Without the
    warning it would be the only unusable root that resolves silently."""
    from lionagi.studio.scheduler.engine import _resolve_action_cwd

    project_dir = tmp_path / "registered-project"
    project_dir.mkdir()
    fake_get_project = AsyncMock(
        return_value={"name": "myproj", "path": str(project_dir), "source": "studio"}
    )
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )
    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    schedule = {"id": "sched-empty-root", "action_cwd": "", "action_project": "myproj"}
    with caplog.at_level(logging.WARNING, logger="lionagi.studio.scheduler.engine"):
        result = await _resolve_action_cwd(schedule)

    assert result == str(project_dir)
    assert any(
        "sched-empty-root" in rec.getMessage() and "empty" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_resolve_action_cwd_never_returns_the_empty_root_itself(tmp_path, monkeypatch):
    """``Path("")`` is ``Path(".")``, which *is* a directory. So an empty
    action_cwd must never reach the ``is_dir()`` check: passing it would
    return "" and spawn the action in the daemon's own cwd -- the silent
    substitution this resolver exists to refuse. This pins that trap, because
    the natural-looking cleanup (testing ``is not None`` there, to match the
    refusal gate below it) reintroduces exactly that fail-open."""
    from pathlib import Path

    from lionagi.studio.scheduler.engine import _resolve_action_cwd

    assert Path("").is_dir(), "premise: an empty path resolves to the cwd"

    project_dir = tmp_path / "registered-project"
    project_dir.mkdir()
    fake_get_project = AsyncMock(
        return_value={"name": "myproj", "path": str(project_dir), "source": "studio"}
    )
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )
    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    schedule = {"id": "sched-empty-root-2", "action_cwd": "", "action_project": "myproj"}
    result = await _resolve_action_cwd(schedule)

    assert result != ""
    assert result == str(project_dir)


@pytest.mark.asyncio
async def test_resolve_action_cwd_refuses_empty_string_action_project(monkeypatch):
    """A present-but-empty action_project fails closed for the same reason: a
    supplied (non-None) execution-root field that resolves to nothing must not
    inherit the daemon's cwd. get_project is never consulted for an empty id."""
    from lionagi.studio.scheduler.engine import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    fake_get_project = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )
    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    schedule = {"id": "sched-empty-proj", "action_cwd": None, "action_project": ""}
    with pytest.raises(SchedulerCwdInheritRefusedError):
        await _resolve_action_cwd(schedule)

    fake_get_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_action_cwd_pre_migration_row_warns_deprecated(monkeypatch, caplog):
    """A pre-migration row (action_cwd never set) still falls through to the
    legacy daemon-cwd-inherit behavior, but the warning names it explicitly
    as a pre-migration/deprecated case rather than the generic message."""
    from lionagi.studio.scheduler.engine import _resolve_action_cwd

    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    schedule = {"id": "sched-root-5", "action_cwd": None, "action_project": None}
    with caplog.at_level(logging.WARNING, logger="lionagi.studio.scheduler.engine"):
        result = await _resolve_action_cwd(schedule)

    assert result is None
    assert any(
        "sched-root-5" in rec.getMessage() and "pre-migration" in rec.getMessage()
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# The refusal names both the configured root and the daemon directory it
# declined to substitute, so the failure is diagnosable from the message.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refusal_names_configured_root_and_daemon_cwd(tmp_path, monkeypatch):
    """The refusal error names the configured-but-unavailable execution root
    and the daemon working directory it declined to inherit, so the operator
    can see exactly which directory could not be honored and where the action
    would otherwise have run."""
    from lionagi.studio.scheduler.engine import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    daemon_dir = tmp_path / "daemon-home"
    daemon_dir.mkdir()
    monkeypatch.chdir(daemon_dir)
    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    schedule = {
        "id": "sched-identity",
        "action_cwd": "/pruned/execution/root",
        "action_project": None,
    }
    with pytest.raises(SchedulerCwdInheritRefusedError) as excinfo:
        await _resolve_action_cwd(schedule)

    assert excinfo.value.configured_root == "/pruned/execution/root"
    assert excinfo.value.daemon_cwd == str(daemon_dir)
    message = str(excinfo.value)
    assert "/pruned/execution/root" in message
    assert str(daemon_dir) in message


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
    svc.get_invocation = AsyncMock(return_value=None)
    svc.compute_files_overlap = AsyncMock(return_value={"count": 0, "top": []})
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
# Fail-closed attribution: a schedule carrying an execution root whose
# configured directories are all gone is refused before spawn (identity
# protection), recorded with FAILED_CWD_INHERIT_REFUSED, and never spawned.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_refuses_owner_carrying_cwd_inherit(monkeypatch):
    """A schedule whose registered project path no longer exists (and nothing
    else resolves) is refused before spawn: it carries an execution root, so
    inheriting the daemon's cwd would run it under the daemon directory's
    identity. The run is recorded with RunReasons.FAILED_CWD_INHERIT_REFUSED and
    spawn_and_wait is never called."""
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

    spawn_mock = AsyncMock(return_value=(1, "boom"))
    with (
        patch(
            "lionagi.studio.scheduler.subprocess.build_argv",
            return_value=(["uv", "run", "li", "agent", "ping"], None),
        ),
        patch("lionagi.studio.scheduler.subprocess.spawn_and_wait", new=spawn_mock),
    ):
        await engine._fire(schedule, "run-cwd-002", trigger_context={"scheduled": True})

    spawn_mock.assert_not_awaited()

    terminal_calls = [
        c
        for c in svc.update_status.await_args_list
        if c.args[:2] == ("schedule_run", "run-cwd-002")
        and c.kwargs.get("new_status") in ("completed", "failed")
    ]
    assert terminal_calls
    (call,) = terminal_calls
    assert call.kwargs["reason_code"] == RunReasons.FAILED_CWD_INHERIT_REFUSED


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


# ---------------------------------------------------------------------------
# SchedulerEngine._backfill_action_cwd — one-shot startup migration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_fills_action_cwd_from_resolvable_action_project(tmp_path, monkeypatch):
    """A pre-migration row with action_cwd unset and a resolvable
    action_project gets action_cwd snapshotted from the project's path."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    fake_get_project = AsyncMock(return_value={"name": "p1", "path": str(project_dir)})
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )

    svc = _make_svc()
    svc.list_schedules = AsyncMock(
        return_value=[_minimal_schedule(id="sched-bf-1", action_cwd=None, action_project="p1")]
    )
    engine = SchedulerEngine(svc=svc)

    await engine._backfill_action_cwd()

    svc.update_schedule.assert_awaited_once_with("sched-bf-1", action_cwd=str(project_dir))


@pytest.mark.asyncio
async def test_backfill_skips_rows_that_already_have_action_cwd(monkeypatch):
    """Idempotency: a row that already has action_cwd is left untouched, and
    the project registry is never consulted for it."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    fake_get_project = AsyncMock()
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )

    svc = _make_svc()
    svc.list_schedules = AsyncMock(
        return_value=[
            _minimal_schedule(id="sched-bf-2", action_cwd="/already/set", action_project="p1")
        ]
    )
    engine = SchedulerEngine(svc=svc)

    await engine._backfill_action_cwd()

    svc.update_schedule.assert_not_called()
    fake_get_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_skips_rows_whose_action_cwd_is_an_empty_string(monkeypatch, tmp_path):
    """An empty action_cwd is a root the schedule supplied, not an unset one.
    The resolver fails closed on it rather than substituting a directory, so
    the backfill must not hand that row a path by a side door either."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    project_dir = tmp_path / "resolvable"
    project_dir.mkdir()
    fake_get_project = AsyncMock(return_value={"name": "p1", "path": str(project_dir)})
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )

    svc = _make_svc()
    svc.list_schedules = AsyncMock(
        return_value=[_minimal_schedule(id="sched-bf-empty", action_cwd="", action_project="p1")]
    )
    engine = SchedulerEngine(svc=svc)

    await engine._backfill_action_cwd()

    svc.update_schedule.assert_not_called()
    fake_get_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_does_not_persist_a_relative_project_path(monkeypatch):
    """Backfill writes the value it derives into the row as that schedule's
    persisted execution root. A relative path means "wherever the daemon
    started", so persisting one snapshots a root that can never resolve."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    fake_get_project = AsyncMock(return_value={"name": "relproj", "path": "."})
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )

    svc = _make_svc()
    svc.list_schedules = AsyncMock(
        return_value=[
            _minimal_schedule(id="sched-bf-rel", action_cwd=None, action_project="relproj")
        ]
    )
    engine = SchedulerEngine(svc=svc)

    await engine._backfill_action_cwd()

    svc.update_schedule.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_skips_rows_with_no_action_project(monkeypatch):
    """A pre-migration row with no action_project at all has nothing to
    backfill from and is left with action_cwd unset."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    fake_get_project = AsyncMock()
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )

    svc = _make_svc()
    svc.list_schedules = AsyncMock(
        return_value=[_minimal_schedule(id="sched-bf-3", action_cwd=None, action_project=None)]
    )
    engine = SchedulerEngine(svc=svc)

    await engine._backfill_action_cwd()

    svc.update_schedule.assert_not_called()
    fake_get_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_leaves_action_cwd_unset_when_project_path_missing(monkeypatch):
    """action_project is set but doesn't resolve to a usable directory:
    action_cwd stays unset rather than being backfilled with a bad value."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    fake_get_project = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )

    svc = _make_svc()
    svc.list_schedules = AsyncMock(
        return_value=[
            _minimal_schedule(id="sched-bf-4", action_cwd=None, action_project="unregistered")
        ]
    )
    engine = SchedulerEngine(svc=svc)

    await engine._backfill_action_cwd()

    svc.update_schedule.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_is_idempotent_across_repeated_startups(tmp_path, monkeypatch):
    """Running the backfill twice in a row (simulating two daemon starts)
    only writes once -- the second pass sees action_cwd already populated."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    fake_get_project = AsyncMock(return_value={"name": "p1", "path": str(project_dir)})
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )

    svc = _make_svc()
    schedule_row = _minimal_schedule(id="sched-bf-5", action_cwd=None, action_project="p1")
    svc.list_schedules = AsyncMock(return_value=[schedule_row])
    engine = SchedulerEngine(svc=svc)

    await engine._backfill_action_cwd()
    svc.update_schedule.assert_awaited_once_with("sched-bf-5", action_cwd=str(project_dir))

    # Simulate the row now carrying its backfilled action_cwd on the second pass.
    schedule_row["action_cwd"] = str(project_dir)
    svc.update_schedule.reset_mock()
    await engine._backfill_action_cwd()
    svc.update_schedule.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_one_bad_row_does_not_block_others(tmp_path, monkeypatch):
    """A get_project failure for one schedule must not prevent backfilling
    the rest of the list."""
    from lionagi.studio.scheduler.engine import SchedulerEngine

    project_dir = tmp_path / "proj-ok"
    project_dir.mkdir()

    async def _fake_get_project(name):
        if name == "boom":
            raise RuntimeError("registry lookup blew up")
        return {"name": name, "path": str(project_dir)}

    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", _fake_get_project, raising=False
    )

    svc = _make_svc()
    svc.list_schedules = AsyncMock(
        return_value=[
            _minimal_schedule(id="sched-bf-bad", action_cwd=None, action_project="boom"),
            _minimal_schedule(id="sched-bf-good", action_cwd=None, action_project="ok"),
        ]
    )
    engine = SchedulerEngine(svc=svc)

    await engine._backfill_action_cwd()

    svc.update_schedule.assert_awaited_once_with("sched-bf-good", action_cwd=str(project_dir))


@pytest.mark.asyncio
async def test_refusal_names_an_empty_execution_root_as_empty_not_as_the_project(
    monkeypatch, tmp_path
):
    """An empty action_cwd is the root that failed closed, so the refusal must
    name it. A truthiness fallback would report action_project instead, naming
    the wrong root in the diagnostic meant to explain the refusal."""
    from lionagi.studio.scheduler.engine import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    fake_get_project = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "lionagi.studio.services.projects.get_project", fake_get_project, raising=False
    )
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    monkeypatch.setenv("LIONAGI_SCHEDULER_CWD", str(env_dir))

    schedule = {"id": "sched-empty-root", "action_cwd": "", "action_project": "some-project"}
    with pytest.raises(SchedulerCwdInheritRefusedError) as excinfo:
        await _resolve_action_cwd(schedule)

    assert excinfo.value.configured_root == ""
