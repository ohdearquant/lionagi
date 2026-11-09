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

from unittest.mock import AsyncMock

import pytest

# _resolve_action_cwd's action_project branch imports lionagi.studio.services.projects,
# which requires fastapi (the `studio` extra); skip gracefully in a bare-core install.
pytest.importorskip("fastapi", reason="studio extra not installed")


@pytest.mark.asyncio
async def test_resolve_action_cwd_uses_registered_project_path(tmp_path, monkeypatch):
    """action_project resolves to that project's stored path, even when
    os.getcwd() is a completely different directory (the daemon-started-
    elsewhere case this bug is about)."""
    from lionhub.orchestration.schedule.execution_root import _resolve_action_cwd

    project_dir = tmp_path / "registered-project"
    project_dir.mkdir()

    fake_get_project = AsyncMock(
        return_value={"name": "myproj", "path": str(project_dir), "source": "studio"}
    )
    monkeypatch.setattr(
        "lionhub.orchestration.project_store.get_project", fake_get_project, raising=False
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
    from lionhub.orchestration.schedule.execution_root import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    fake_get_project = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "lionhub.orchestration.project_store.get_project", fake_get_project, raising=False
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
    from lionhub.orchestration.schedule.execution_root import _resolve_action_cwd

    fake_get_project = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "lionhub.orchestration.project_store.get_project", fake_get_project, raising=False
    )

    env_dir = tmp_path / "env-only"
    env_dir.mkdir()
    monkeypatch.setenv("LIONAGI_SCHEDULER_CWD", str(env_dir))

    schedule = {"id": "sched-3", "action_project": None}
    result = await _resolve_action_cwd(schedule)

    assert result == str(env_dir)
    fake_get_project.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_action_cwd_refuses_when_project_path_nonexistent(monkeypatch, tmp_path):
    """A registered project whose stored path no longer exists on disk must
    not be trusted; with nothing else resolvable the resolver fails closed
    rather than inheriting the daemon's cwd (which would run the action under
    the daemon directory's identity)."""
    from lionhub.orchestration.schedule.execution_root import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    fake_get_project = AsyncMock(
        return_value={"name": "stale", "path": "/no/such/directory/at/all", "source": "studio"}
    )
    monkeypatch.setattr(
        "lionhub.orchestration.project_store.get_project", fake_get_project, raising=False
    )
    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    schedule = {"id": "sched-5", "action_project": "stale"}
    with pytest.raises(SchedulerCwdInheritRefusedError) as excinfo:
        await _resolve_action_cwd(schedule)

    assert excinfo.value.configured_root == "stale"


@pytest.mark.asyncio
async def test_resolve_action_cwd_stale_project_path_refuses_despite_env_fallback(
    monkeypatch, tmp_path
):
    """A registered-but-stale project path still fails closed when
    LIONAGI_SCHEDULER_CWD names a usable directory. The env directory does not
    rescue the run: it is a different directory than the one the schedule
    configured, and spawning there succeeds silently rather than failing, which
    is the substitution this refusal exists to prevent."""
    from lionhub.orchestration.schedule.execution_root import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    fake_get_project = AsyncMock(
        return_value={"name": "stale", "path": "/no/such/directory/at/all", "source": "studio"}
    )
    monkeypatch.setattr(
        "lionhub.orchestration.project_store.get_project", fake_get_project, raising=False
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
    from lionhub.orchestration.schedule.execution_root import _resolve_action_cwd

    env_dir = tmp_path / "ownerless-env"
    env_dir.mkdir()
    monkeypatch.setenv("LIONAGI_SCHEDULER_CWD", str(env_dir))

    schedule = {"id": "sched-ownerless", "action_cwd": None, "action_project": None}
    assert await _resolve_action_cwd(schedule) == str(env_dir)


@pytest.mark.asyncio
async def test_resolve_action_cwd_prefers_persisted_root_over_action_project(tmp_path, monkeypatch):
    """A stored action_cwd wins even when action_project resolves to a
    different, equally-real directory -- the persisted root is a snapshot,
    not re-derived from the live project registry on every fire."""
    from lionhub.orchestration.schedule.execution_root import _resolve_action_cwd

    root_dir = tmp_path / "persisted-root"
    root_dir.mkdir()
    project_dir = tmp_path / "registered-project"
    project_dir.mkdir()

    fake_get_project = AsyncMock(
        return_value={"name": "myproj", "path": str(project_dir), "source": "studio"}
    )
    monkeypatch.setattr(
        "lionhub.orchestration.project_store.get_project", fake_get_project, raising=False
    )

    schedule = {"id": "sched-root-1", "action_cwd": str(root_dir), "action_project": "myproj"}
    result = await _resolve_action_cwd(schedule)

    assert result == str(root_dir)
    fake_get_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_resolve_action_cwd_survives_daemon_restart_elsewhere(tmp_path, monkeypatch):
    """The whole point of ADR-0070 delta 1: a persisted action_cwd resolves
    correctly no matter where the daemon process itself was started from."""
    from lionhub.orchestration.schedule.execution_root import _resolve_action_cwd

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
    from lionhub.orchestration.schedule.execution_root import _resolve_action_cwd

    project_dir = tmp_path / "registered-project"
    project_dir.mkdir()
    fake_get_project = AsyncMock(
        return_value={"name": "myproj", "path": str(project_dir), "source": "studio"}
    )
    monkeypatch.setattr(
        "lionhub.orchestration.project_store.get_project", fake_get_project, raising=False
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
    from lionhub.orchestration.schedule.execution_root import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    fake_get_project = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "lionhub.orchestration.project_store.get_project", fake_get_project, raising=False
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
    from lionhub.orchestration.schedule.execution_root import (
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
    from lionhub.orchestration.schedule.execution_root import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    fake_get_project = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "lionhub.orchestration.project_store.get_project", fake_get_project, raising=False
    )
    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    schedule = {"id": "sched-relative", "action_cwd": ".", "action_project": None}
    with pytest.raises(SchedulerCwdInheritRefusedError):
        await _resolve_action_cwd(schedule)


@pytest.mark.asyncio
async def test_resolve_action_cwd_refuses_a_relative_registered_project_path(monkeypatch):
    """Registered project paths are not validated when they are written, so a
    relative one reaches the resolver and must be rejected here."""
    from lionhub.orchestration.schedule.execution_root import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    fake_get_project = AsyncMock(return_value={"name": "relproj", "path": ".", "source": "studio"})
    monkeypatch.setattr(
        "lionhub.orchestration.project_store.get_project", fake_get_project, raising=False
    )
    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    schedule = {"id": "sched-relproj", "action_cwd": None, "action_project": "relproj"}
    with pytest.raises(SchedulerCwdInheritRefusedError):
        await _resolve_action_cwd(schedule)


@pytest.mark.asyncio
async def test_resolve_action_cwd_ignores_a_relative_env_fallback(monkeypatch):
    """The operator-set default gets the same test as everything else: a
    relative value there is the daemon's cwd under another name."""
    from lionhub.orchestration.schedule.execution_root import _resolve_action_cwd

    monkeypatch.setenv("LIONAGI_SCHEDULER_CWD", ".")

    schedule = {"id": "sched-relenv", "action_cwd": None, "action_project": None}

    assert await _resolve_action_cwd(schedule) is None


@pytest.mark.asyncio
async def test_resolve_action_cwd_never_returns_the_empty_root_itself(tmp_path, monkeypatch):
    """``Path("")`` is ``Path(".")``, which *is* a directory. So an empty
    action_cwd must never reach the ``is_dir()`` check: passing it would
    return "" and spawn the action in the daemon's own cwd -- the silent
    substitution this resolver exists to refuse. This pins that trap, because
    the natural-looking cleanup (testing ``is not None`` there, to match the
    refusal gate below it) reintroduces exactly that fail-open."""
    from pathlib import Path

    from lionhub.orchestration.schedule.execution_root import _resolve_action_cwd

    assert Path("").is_dir(), "premise: an empty path resolves to the cwd"

    project_dir = tmp_path / "registered-project"
    project_dir.mkdir()
    fake_get_project = AsyncMock(
        return_value={"name": "myproj", "path": str(project_dir), "source": "studio"}
    )
    monkeypatch.setattr(
        "lionhub.orchestration.project_store.get_project", fake_get_project, raising=False
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
    from lionhub.orchestration.schedule.execution_root import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    fake_get_project = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "lionhub.orchestration.project_store.get_project", fake_get_project, raising=False
    )
    monkeypatch.delenv("LIONAGI_SCHEDULER_CWD", raising=False)

    schedule = {"id": "sched-empty-proj", "action_cwd": None, "action_project": ""}
    with pytest.raises(SchedulerCwdInheritRefusedError):
        await _resolve_action_cwd(schedule)

    fake_get_project.assert_not_awaited()


@pytest.mark.asyncio
async def test_refusal_names_configured_root_and_daemon_cwd(tmp_path, monkeypatch):
    """The refusal error names the configured-but-unavailable execution root
    and the daemon working directory it declined to inherit, so the operator
    can see exactly which directory could not be honored and where the action
    would otherwise have run."""
    from lionhub.orchestration.schedule.execution_root import (
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


@pytest.mark.asyncio
async def test_refusal_names_an_empty_execution_root_as_empty_not_as_the_project(
    monkeypatch, tmp_path
):
    """An empty action_cwd is the root that failed closed, so the refusal must
    name it. A truthiness fallback would report action_project instead, naming
    the wrong root in the diagnostic meant to explain the refusal."""
    from lionhub.orchestration.schedule.execution_root import (
        SchedulerCwdInheritRefusedError,
        _resolve_action_cwd,
    )

    fake_get_project = AsyncMock(return_value=None)
    monkeypatch.setattr(
        "lionhub.orchestration.project_store.get_project", fake_get_project, raising=False
    )
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    monkeypatch.setenv("LIONAGI_SCHEDULER_CWD", str(env_dir))

    schedule = {"id": "sched-empty-root", "action_cwd": "", "action_project": "some-project"}
    with pytest.raises(SchedulerCwdInheritRefusedError) as excinfo:
        await _resolve_action_cwd(schedule)

    assert excinfo.value.configured_root == ""
