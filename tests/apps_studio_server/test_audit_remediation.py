# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from importlib import reload
from pathlib import Path
from unittest.mock import patch

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(monkeypatch, fake_db: Path | None = None) -> TestClient:
    """Reload app to pick up monkeypatched env vars."""
    import lionagi.studio.app as app_mod
    import lionagi.studio.services.stats as stats_mod

    if fake_db is not None:
        monkeypatch.setattr(stats_mod, "DEFAULT_DB_PATH", fake_db)
        monkeypatch.setattr(stats_mod, "_DB", str(fake_db))

    reload(app_mod)
    return TestClient(app_mod.app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# LIONAGI-AUDIT-001 — Artifact GET routes bypass bearer auth
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestArtifactAuthBypass:
    """Regression: artifact GET routes must require bearer token when configured.

    Before the fix, GET /api/artifacts/{id} and GET /api/artifacts/by-session/{sid}
    returned non-401 responses (404 or 200) even without an Authorization header,
    proving auth was bypassed.

    Attack scenario: attacker obtains studio URL, guesses or enumerates an artifact
    ID, and reads agent-produced content (model output, file excerpts, credentials)
    without any credential.
    """

    def test_get_artifact_no_token_returns_401(self, monkeypatch, tmp_path):
        """GET /api/artifacts/{id} without Authorization header → 401 when token set."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-artifact-secret")
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        # Any artifact id — we want to verify auth fires before reaching the service
        resp = client.get("/api/artifacts/some-artifact-id")
        assert resp.status_code == 401, (
            f"Expected 401 (auth rejected), got {resp.status_code}. "
            "Artifact GET route is bypassing bearer auth."
        )

    def test_get_artifact_wrong_token_returns_401(self, monkeypatch, tmp_path):
        """GET /api/artifacts/{id} with wrong token → 401."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "correct-secret")
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.get(
            "/api/artifacts/some-artifact-id",
            headers={"Authorization": "Bearer wrong-secret"},
        )
        assert resp.status_code == 401

    def test_get_artifact_correct_token_passes_auth(self, monkeypatch, tmp_path):
        """GET /api/artifacts/{id} with correct token must not return 401."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "correct-secret")
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.get(
            "/api/artifacts/nonexistent",
            headers={"Authorization": "Bearer correct-secret"},
        )
        # Auth passed — 404 is the correct service response for unknown artifact
        assert resp.status_code != 401

    def test_get_artifacts_by_session_no_token_returns_401(self, monkeypatch, tmp_path):
        """GET /api/artifacts/by-session/{sid} without Authorization → 401."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-secret")
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.get("/api/artifacts/by-session/some-session-id")
        assert resp.status_code == 401

    def test_health_still_open(self, monkeypatch, tmp_path):
        """/health remains accessible even with artifact auth enabled."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-secret")
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.get("/health")
        assert resp.status_code == 200

    def test_stats_still_open(self, monkeypatch, tmp_path):
        """/api/stats remains accessible (existing behaviour must not regress)."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-secret")
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.get("/api/stats")
        assert resp.status_code == 200

    def test_artifact_auth_disabled_when_no_token(self, monkeypatch, tmp_path):
        """Without LIONAGI_STUDIO_AUTH_TOKEN all routes are open."""
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        # Should reach service layer, not return 401
        resp = client.get("/api/artifacts/some-id")
        assert resp.status_code != 401


# ---------------------------------------------------------------------------
# LIONAGI-AUDIT-002 — Fire tasks must be tracked and cancelled on shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSchedulerFireTaskLifecycle:
    """Regression: _fire tasks must be tracked so stop() can cancel them.

    Before the fix, asyncio.create_task(self._fire(...)) was fire-and-forget.
    After shutdown self._task was cancelled but outstanding _fire tasks continued
    running, allowing orphaned subprocess waits after the scheduler stopped.
    """

    async def test_fire_tasks_tracked_and_removed_on_completion(self):
        """_tracked_fire stores tasks in _fire_tasks and removes them on completion.

        Regression for LIONAGI-AUDIT-002: fire-and-forget asyncio.create_task() calls
        produced no tracking set — this test verifies the set grows and shrinks correctly.
        """
        from lionagi.studio.scheduler.engine import SchedulerEngine

        engine = SchedulerEngine()

        fired = asyncio.Event()
        done = asyncio.Event()

        async def fake_fire(*args, **kwargs):
            fired.set()
            await done.wait()

        # Inject a tracked task directly via _tracked_fire
        with patch.object(engine, "_fire", side_effect=fake_fire):
            task = engine._tracked_fire({}, "run_id", trigger_context={})

        # Task is still running — should be in the set
        await fired.wait()
        assert len(engine._fire_tasks) == 1, "Task must be tracked while running"

        # Let it complete
        done.set()
        await asyncio.gather(task, return_exceptions=True)
        # Yield so the done callback fires
        for _ in range(5):
            await asyncio.sleep(0)

        assert len(engine._fire_tasks) == 0, "Task handle must be removed on completion"

    async def test_stop_cancels_outstanding_fire_tasks(self):
        """stop() must cancel and await all outstanding _fire tasks.

        Regression for LIONAGI-AUDIT-002 (studio-standards 2026-06-06).
        """
        from lionagi.studio.scheduler.engine import SchedulerEngine

        engine = SchedulerEngine()

        blocking = asyncio.Event()
        cancelled_flag = {"value": False}

        async def long_fire(*args, **kwargs):
            try:
                await blocking.wait()
            except asyncio.CancelledError:
                cancelled_flag["value"] = True
                raise

        # Inject a long-running fire task directly
        task = asyncio.create_task(long_fire())
        engine._fire_tasks.add(task)
        task.add_done_callback(engine._fire_tasks.discard)

        # Also set up the tick-loop task (so stop() can cancel it)
        async def noop_loop():
            await asyncio.sleep(9999)

        engine._task = asyncio.create_task(noop_loop())

        # stop() must cancel and await the fire task
        await engine.stop()

        assert cancelled_flag["value"] is True, "stop() did not cancel the outstanding _fire task"
        assert len(engine._fire_tasks) == 0, "Outstanding fire tasks remain after stop()"


# ---------------------------------------------------------------------------
# LIONAGI-AUDIT-003 — Schedule action kind validation
# ---------------------------------------------------------------------------


class TestBuildArgvValidation:
    """Regression: unknown/aliased action_kind must be validated fail-closed."""

    def test_unknown_kind_raises(self):
        """build_argv with an unknown action_kind must raise ValueError."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="Unknown action_kind"):
            build_argv({"action_kind": "nonexistent"}, {})

    def test_playbook_alias_normalized_to_play(self):
        """action_kind='playbook' (legacy CLI alias) normalizes to 'play'.

        Regression for LIONAGI-AUDIT-003: 'playbook' was not recognized by
        build_argv(), causing it to produce ['uv', 'run', 'li'] — an
        underspecified command — instead of ['uv', 'run', 'li', 'play', ...].
        """
        from lionagi.studio.scheduler.subprocess import build_argv

        argv = build_argv(
            {
                "action_kind": "playbook",
                "action_playbook": "audit",
                "action_model": None,
                "action_prompt": None,
                "action_agent": None,
                "action_project": None,
                "action_extra_args": [],
            },
            {},
        )
        assert "play" in argv, f"'play' not in argv: {argv}"
        assert "audit" in argv, f"playbook name not in argv: {argv}"

    def test_valid_kinds_do_not_raise(self):
        """All four ADR-0027 action kinds must be accepted."""
        from lionagi.studio.scheduler.subprocess import build_argv

        for kind in ("agent", "flow", "fanout", "play"):
            argv = build_argv(
                {
                    "action_kind": kind,
                    "action_model": "gpt-4",
                    "action_prompt": "do stuff",
                    "action_agent": None,
                    "action_playbook": None,
                    "action_project": None,
                    "action_extra_args": [],
                },
                {},
            )
            assert "uv" in argv

    def test_empty_string_kind_raises(self):
        """Empty string action_kind must be rejected."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="Unknown action_kind"):
            build_argv({"action_kind": ""}, {})


# ---------------------------------------------------------------------------
# Codex #1283 — orphaned subprocess on cancel + invalid-kind run recording
# ---------------------------------------------------------------------------


def _interval_schedule(**over):
    s = {
        "id": "sched-x",
        "name": "test-sched",
        "trigger_type": "interval",
        "interval_sec": 60,
        "action_kind": "agent",
        "action_model": "gpt-4",
        "action_prompt": "hi",
        "overlap_policy": "allow",
    }
    s.update(over)
    return s


class TestFireBuildFailureRecorded:
    """Codex #1283 P2: an invalid action_kind raised inside _fire (build_argv)
    before the schedule_run row existed, so the generic handler called
    update_status() on a missing row (LookupError) and left the invocation
    stuck `running`. The failure must be recorded deterministically instead."""

    async def test_invalid_kind_records_failed_run_and_invocation(self, monkeypatch, tmp_path):
        monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
        from lionagi.state.db import StateDB
        from lionagi.studio.scheduler.engine import SchedulerEngine

        engine = SchedulerEngine()
        run_id = "run-bad"
        # Seed a valid schedule row (schedules.action_kind has a CHECK, so the
        # bad kind can only reach _fire via a chain action override carrying the
        # existing schedule_id). schedule_runs.schedule_id FK -> schedules.
        async with StateDB() as db:
            await db.create_schedule(_interval_schedule())
        schedule = _interval_schedule(action_kind="totally-bogus")  # same id

        # Must NOT raise (no LookupError leaking out) and must return cleanly.
        await engine._fire(schedule, run_id, trigger_context={})

        async with StateDB() as db:
            run = await db.get_schedule_run(run_id)
            assert run is not None, "schedule_run row must exist for the bad fire"
            assert run["status"] == "failed"
            inv = await db.get_invocation(run["invocation_id"])
            assert inv is not None
            assert inv["status"] == "failed", "invocation must not be left running"
        # Not tracked as running after returning.
        assert schedule["id"] not in engine._running


class TestSpawnAndWaitCancellation:
    """Codex #1283 P1: cancelling spawn_and_wait must terminate the spawned
    child, not leave it detached."""

    async def test_cancel_terminates_child(self, monkeypatch):
        from lionagi.studio.scheduler import subprocess as sp

        class _FakeProc:
            def __init__(self):
                self.terminated = False
                self.killed = False
                self.returncode = -15

            async def communicate(self):
                await asyncio.Event().wait()  # block until cancelled

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

            async def wait(self):
                return self.returncode

        proc = _FakeProc()

        async def fake_exec(*a, **k):
            return proc

        monkeypatch.setattr(sp.asyncio, "create_subprocess_exec", fake_exec)

        task = asyncio.create_task(sp.spawn_and_wait(["sleep", "30"], "inv-1"))
        # Let it reach communicate() and block.
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proc.terminated is True, "child must be terminated on cancellation"


class TestFireCancellationRecorded:
    """Codex #1283 P1: when stop() cancels an in-flight _fire, the run and
    invocation must be recorded as cancelled, not left `running`."""

    async def test_cancel_during_spawn_records_cancelled(self, monkeypatch, tmp_path):
        monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
        from lionagi.state.db import StateDB
        from lionagi.studio.scheduler import subprocess as sp
        from lionagi.studio.scheduler.engine import SchedulerEngine

        started = asyncio.Event()

        async def blocking_spawn(argv, inv_id):
            started.set()
            await asyncio.Event().wait()  # block until the _fire task is cancelled

        # _fire imports spawn_and_wait from .subprocess at call time.
        monkeypatch.setattr(sp, "spawn_and_wait", blocking_spawn)

        async with StateDB() as db:
            await db.create_schedule(_interval_schedule())  # satisfy schedule_run FK

        engine = SchedulerEngine()
        run_id = "run-cancel"
        engine._tracked_fire(_interval_schedule(), run_id, trigger_context={})

        await started.wait()  # row created, now inside spawn
        await engine.stop()  # cancels + awaits the fire task

        async with StateDB() as db:
            run = await db.get_schedule_run(run_id)
            assert run is not None and run["status"] == "cancelled"
            inv = await db.get_invocation(run["invocation_id"])
            assert inv is not None and inv["status"] == "cancelled"
