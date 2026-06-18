# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import signal
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
# Artifact GET routes bearer auth guard
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestArtifactAuthBypass:
    """Artifact GET routes must require bearer token when configured.

    Attack scenario: unauthenticated caller enumerates artifact IDs to read
    agent-produced content (model output, file excerpts, credentials).
    """

    def test_get_artifact_no_token_returns_401(self, monkeypatch, tmp_path):
        """GET /api/artifacts/{id} without Authorization returns 401 when token set."""
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
        """/api/stats requires auth when a token is configured."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-secret")
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.get("/api/stats")
        assert resp.status_code == 401

    def test_artifact_auth_disabled_when_no_token(self, monkeypatch, tmp_path):
        """Without LIONAGI_STUDIO_AUTH_TOKEN all routes are open."""
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        # Should reach service layer, not return 401
        resp = client.get("/api/artifacts/some-id")
        assert resp.status_code != 401


# ---------------------------------------------------------------------------
# Scheduler fire task lifecycle — tracking and cancellation on shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSchedulerFireTaskLifecycle:
    """_fire tasks must be tracked so stop() can cancel them on shutdown."""

    async def test_fire_tasks_tracked_and_removed_on_completion(self):
        """_tracked_fire stores tasks in _fire_tasks and removes them on completion."""
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
        """stop() must cancel and await all outstanding _fire tasks."""
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
# Schedule action kind validation
# ---------------------------------------------------------------------------


class TestBuildArgvValidation:
    """Regression: unknown/aliased action_kind must be validated fail-closed."""

    def test_unknown_kind_raises(self):
        """build_argv with an unknown action_kind must raise ValueError."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="Unknown action_kind"):
            build_argv({"action_kind": "nonexistent"}, {})

    def test_playbook_alias_normalized_to_play(self):
        """action_kind='playbook' (legacy CLI alias) normalizes to 'play'."""
        from lionagi.studio.scheduler.subprocess import build_argv

        argv, tmp_path = build_argv(
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
        assert tmp_path is None, "playbook alias must not create a tmp file"
        assert "play" in argv, f"'play' not in argv: {argv}"
        assert "audit" in argv, f"playbook name not in argv: {argv}"

    def test_valid_kinds_do_not_raise(self):
        """All four ADR-0027 action kinds must be accepted."""
        from lionagi.studio.scheduler.subprocess import build_argv

        for kind in ("agent", "flow", "fanout", "play"):
            argv, tmp_path = build_argv(
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
            assert tmp_path is None, f"kind={kind!r} must not create a tmp file"

    def test_empty_string_kind_raises(self):
        """Empty string action_kind must be rejected."""
        from lionagi.studio.scheduler.subprocess import build_argv

        with pytest.raises(ValueError, match="Unknown action_kind"):
            build_argv({"action_kind": ""}, {})


# ---------------------------------------------------------------------------
# Orphaned subprocess on cancel + invalid-kind run recording
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
    """An invalid action_kind raised inside _fire before the schedule_run row existed
    must be recorded deterministically instead of leaving the invocation stuck running."""

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
    """Cancelling spawn_and_wait must terminate the process GROUP, not just the direct
    child — `uv run li` forks the real worker, so signalling only the child orphans
    grandchildren."""

    async def test_cancel_terminates_child_and_group(self, monkeypatch):
        from lionagi.studio.scheduler import subprocess as sp

        class _FakeProc:
            def __init__(self):
                self.pid = 424242  # > 1 so the group-kill path engages
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
            # The fix must request its own session/process group.
            assert k.get("start_new_session") is True, (
                "subprocess must start_new_session so the group is killable"
            )
            return proc

        killpg_calls: list[tuple[int, int]] = []

        def fake_killpg(pgid, sig):
            killpg_calls.append((pgid, sig))

        monkeypatch.setattr(sp.asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(sp.os, "killpg", fake_killpg)

        task = asyncio.create_task(sp.spawn_and_wait(["sleep", "30"], "inv-1"))
        # Let it reach communicate() and block.
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert proc.terminated is True, "direct child must be terminated"
        assert killpg_calls, "the process GROUP must be signalled, not just the child"
        assert killpg_calls[0] == (proc.pid, signal.SIGTERM)

    async def test_cancel_no_killpg_platform(self, monkeypatch):
        """os.killpg is POSIX-only: on Windows cancellation must still terminate
        the direct child, not raise AttributeError from the cancel handler
        (which only suppresses ProcessLookupError/PermissionError)."""
        from lionagi.studio.scheduler import subprocess as sp

        class _FakeProc:
            def __init__(self):
                self.pid = 424243
                self.terminated = False

            async def communicate(self):
                await asyncio.Event().wait()

            def terminate(self):
                self.terminated = True

            def kill(self):
                pass

            async def wait(self):
                return -15

        proc = _FakeProc()

        async def fake_exec(*a, **k):
            return proc

        monkeypatch.setattr(sp.asyncio, "create_subprocess_exec", fake_exec)
        # Simulate Windows: os.killpg does not exist.
        monkeypatch.delattr(sp.os, "killpg", raising=False)

        task = asyncio.create_task(sp.spawn_and_wait(["sleep", "30"], "inv-2"))
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        # Must surface CancelledError, NOT AttributeError.
        with pytest.raises(asyncio.CancelledError):
            await task
        assert proc.terminated is True, "child must be terminated even without killpg"


class TestFireCancellationRecorded:
    """When stop() cancels an in-flight _fire, the run and invocation must be
    recorded as cancelled, not left running."""

    async def test_cancel_during_spawn_records_cancelled(self, monkeypatch, tmp_path):
        monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
        from lionagi.state.db import StateDB
        from lionagi.studio.scheduler import subprocess as sp
        from lionagi.studio.scheduler.engine import SchedulerEngine

        started = asyncio.Event()

        async def blocking_spawn(argv, inv_id, *, tmp_path=None):
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


class TestInvocationReasonAggregation:
    """Aggregating an aborted child must reflect the child's ACTUAL reason:
    CANCELLED_SIGINT when it was SIGINT'd (agent/flow teardown), but ABORTED_USER
    when an admin transition or `li state doctor` aborted it — `aborted` is not
    exclusively SIGINT in this codebase."""

    @staticmethod
    async def _seed_aborted_child(db, inv_id: str, sid: str, prog: str, reason_code: str) -> None:
        await db.create_invocation(
            {"id": inv_id, "skill": "show", "started_at": 0.0, "status": "running"}
        )
        await db.create_progression(prog)
        await db.create_session(
            {
                "id": sid,
                "progression_id": prog,
                "status": "running",
                "started_at": 0.0,
                "invocation_id": inv_id,
            }
        )
        # Transition to aborted with a concrete reason (what the SIGINT handler,
        # admin router, or doctor would record).
        await db.update_status(
            "session",
            sid,
            new_status="aborted",
            reason_code=reason_code,
            reason_summary="seed",
            source="executor",
            actor="test",
        )

    async def test_sigint_aborted_child_aggregates_to_cancelled_sigint(self, monkeypatch, tmp_path):
        monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
        from lionagi.state.db import StateDB
        from lionagi.state.reasons import RunReasons
        from lionagi.studio.scheduler.engine import _resolve_invocation_terminal

        async with StateDB() as db:
            await self._seed_aborted_child(
                db, "inv-sigint", "sess-1", "prog-1", RunReasons.CANCELLED_SIGINT
            )
            status, reason_code, *_ = await _resolve_invocation_terminal(
                db, "inv-sigint", fallback_status="completed"
            )

        assert status == "aborted"
        assert reason_code == RunReasons.CANCELLED_SIGINT

    async def test_admin_aborted_child_keeps_aborted_user(self, monkeypatch, tmp_path):
        """A child aborted by admin/doctor (ABORTED_USER) must NOT become SIGINT."""
        monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
        from lionagi.state.db import StateDB
        from lionagi.state.reasons import RunReasons
        from lionagi.studio.scheduler.engine import _resolve_invocation_terminal

        async with StateDB() as db:
            await self._seed_aborted_child(
                db, "inv-admin", "sess-2", "prog-2", RunReasons.ABORTED_USER
            )
            status, reason_code, *_ = await _resolve_invocation_terminal(
                db, "inv-admin", fallback_status="completed"
            )

        assert status == "aborted"
        assert reason_code == RunReasons.ABORTED_USER
        assert reason_code != RunReasons.CANCELLED_SIGINT

    async def test_fallback_aborted_keeps_aborted_user(self, monkeypatch, tmp_path):
        """No child reason to inspect → neutral ABORTED_USER, not an assumed SIGINT."""
        monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
        from lionagi.state.db import StateDB
        from lionagi.state.reasons import RunReasons
        from lionagi.studio.scheduler.engine import _resolve_invocation_terminal

        async with StateDB() as db:
            await db.create_invocation(
                {"id": "inv-fallback", "skill": "show", "started_at": 0.0, "status": "running"}
            )
            status, reason_code, *_ = await _resolve_invocation_terminal(
                db, "inv-fallback", fallback_status="aborted"
            )

        assert status == "aborted"
        assert reason_code == RunReasons.ABORTED_USER


# ---------------------------------------------------------------------------
# Codex round-2 Low — router-level PATCH validation (HTTP 400 for invalid
# flow_yaml transitions), covering the ValueError→HTTPException translation
# added to services/schedules.py.
# ---------------------------------------------------------------------------


class TestSchedulePatchRouterValidation:
    """Router PATCH /api/schedules/{id} must translate service ValueError → 400.

    The route handler calls ``sched_svc.update_schedule`` as a module-attribute
    lookup at call time, so monkeypatching the attribute is sufficient.  No
    app reload is needed — reloading ``app`` duplicates ``include_router`` calls
    and corrupts route ordering when tests run sequentially.
    """

    @staticmethod
    def _client_for_svc_mock(monkeypatch, mock_fn):
        """Patch sched_svc.update_schedule and return a TestClient."""
        from fastapi.testclient import TestClient

        import lionagi.studio.services.schedules as sched_svc
        from lionagi.studio.app import app

        monkeypatch.setattr(sched_svc, "update_schedule", mock_fn)
        return TestClient(app, raise_server_exceptions=False)

    def test_patch_flow_yaml_without_yaml_returns_400(self, monkeypatch):
        """PATCH action_kind=flow_yaml with no yaml body → HTTP 400."""

        async def _reject(_schedule_id, _fields):
            raise ValueError(
                "action_flow_yaml is required and must not be empty for action_kind='flow_yaml'"
            )

        client = self._client_for_svc_mock(monkeypatch, _reject)
        r = client.patch(
            "/api/schedules/sched-abc",
            json={"action_kind": "flow_yaml"},
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
        assert "action_flow_yaml" in r.json().get("detail", "")

    def test_patch_malformed_yaml_returns_400(self, monkeypatch):
        """PATCH with malformed flow_yaml spec → HTTP 400."""

        async def _reject(_schedule_id, _fields):
            raise ValueError("Invalid flow_yaml spec: missing required field 'steps'")

        client = self._client_for_svc_mock(monkeypatch, _reject)
        r = client.patch(
            "/api/schedules/sched-abc",
            json={"action_kind": "flow_yaml", "action_flow_yaml": "bad: yaml: [[["},
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
        assert "Invalid flow_yaml" in r.json().get("detail", "")

    def test_patch_not_found_returns_404(self, monkeypatch):
        """PATCH a non-existent schedule → HTTP 404 (service returns False)."""

        async def _not_found(_schedule_id, _fields):
            return False

        client = self._client_for_svc_mock(monkeypatch, _not_found)
        r = client.patch("/api/schedules/no-such-id", json={"name": "new-name"})
        assert r.status_code == 404, f"expected 404, got {r.status_code}: {r.text}"


# ---------------------------------------------------------------------------
# CWE-88 argument injection — router-level 400 responses
# ---------------------------------------------------------------------------


class TestScheduleArgvInjectionRouterValidation:
    """Router POST and PATCH /api/schedules must translate flag-injection
    ValueError → HTTP 400 for action_model and action_extra_args rejections.

    Mirrors TestSchedulePatchRouterValidation: the route handler already maps
    ValueError→400; these tests confirm that the new service-layer checks for
    action_model/action_extra_args surface as 400 responses through the router.
    """

    @staticmethod
    def _client_patch(monkeypatch, mock_fn):
        """Patch sched_svc.update_schedule and return a TestClient."""
        from fastapi.testclient import TestClient

        import lionagi.studio.services.schedules as sched_svc
        from lionagi.studio.app import app

        monkeypatch.setattr(sched_svc, "update_schedule", mock_fn)
        return TestClient(app, raise_server_exceptions=False)

    @staticmethod
    def _client_post(monkeypatch, mock_fn):
        """Patch sched_svc.create_schedule and return a TestClient."""
        from fastapi.testclient import TestClient

        import lionagi.studio.services.schedules as sched_svc
        from lionagi.studio.app import app

        monkeypatch.setattr(sched_svc, "create_schedule", mock_fn)
        return TestClient(app, raise_server_exceptions=False)

    # -- PATCH action_model injection --

    def test_patch_action_model_flag_returns_400(self, monkeypatch):
        """PATCH action_model='--bypass' → HTTP 400 with detail naming the field."""

        async def _reject(_schedule_id, _fields):
            raise ValueError("action_model '--bypass' starts with '-' and would inject a CLI flag")

        client = self._client_patch(monkeypatch, _reject)
        r = client.patch(
            "/api/schedules/sched-abc",
            json={"action_model": "--bypass"},
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
        assert "--bypass" in r.json().get("detail", "") or "action_model" in r.json().get(
            "detail", ""
        )

    def test_patch_action_model_yolo_returns_400(self, monkeypatch):
        """PATCH action_model='--yolo' → HTTP 400."""

        async def _reject(_schedule_id, _fields):
            raise ValueError("action_model '--yolo' starts with '-' and would inject a CLI flag")

        client = self._client_patch(monkeypatch, _reject)
        r = client.patch(
            "/api/schedules/sched-abc",
            json={"action_model": "--yolo"},
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"

    # -- PATCH action_extra_args injection --

    def test_patch_extra_args_flag_returns_400(self, monkeypatch):
        """PATCH action_extra_args=['--bypass'] → HTTP 400."""

        async def _reject(_schedule_id, _fields):
            raise ValueError(
                "action_extra_args element '--bypass' starts with '-' and would inject a CLI flag"
            )

        client = self._client_patch(monkeypatch, _reject)
        r = client.patch(
            "/api/schedules/sched-abc",
            json={"action_extra_args": ["--bypass"]},
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
        assert "--bypass" in r.json().get("detail", "") or "action_extra_args" in r.json().get(
            "detail", ""
        )

    def test_patch_extra_args_yolo_returns_400(self, monkeypatch):
        """PATCH action_extra_args=['--yolo'] → HTTP 400."""

        async def _reject(_schedule_id, _fields):
            raise ValueError(
                "action_extra_args element '--yolo' starts with '-' and would inject a CLI flag"
            )

        client = self._client_patch(monkeypatch, _reject)
        r = client.patch(
            "/api/schedules/sched-abc",
            json={"action_extra_args": ["--yolo"]},
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"

    # -- POST (create) action_model injection --

    def test_create_action_model_flag_returns_400(self, monkeypatch):
        """POST action_model='--bypass' → HTTP 400."""

        async def _reject(_data):
            raise ValueError("action_model '--bypass' starts with '-' and would inject a CLI flag")

        client = self._client_post(monkeypatch, _reject)
        r = client.post(
            "/api/schedules/",
            json={
                "name": "bad-model-sched",
                "trigger_type": "cron",
                "action_kind": "agent",
                "action_model": "--bypass",
                "action_prompt": "hello",
            },
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"

    def test_create_extra_args_flag_returns_400(self, monkeypatch):
        """POST action_extra_args=['--bypass'] → HTTP 400."""

        async def _reject(_data):
            raise ValueError(
                "action_extra_args element '--bypass' starts with '-' and would inject a CLI flag"
            )

        client = self._client_post(monkeypatch, _reject)
        r = client.post(
            "/api/schedules/",
            json={
                "name": "bad-extra-sched",
                "trigger_type": "cron",
                "action_kind": "agent",
                "action_model": "sonnet",
                "action_extra_args": ["--bypass"],
            },
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"


# ---------------------------------------------------------------------------
# CWE-88 argument injection — real-service router tests (round 2)
#
# These tests use the REAL service layer (no mock) with a temp SQLite DB to
# validate that flag-injection rejections propagate through the router as 400.
# ---------------------------------------------------------------------------


def _real_svc_client(monkeypatch, tmp_path: Path) -> TestClient:
    """Return a TestClient backed by the real service layer + temp DB."""
    from fastapi.testclient import TestClient

    import lionagi.state.db as state_db_mod
    import lionagi.studio.services.schedules as sched_svc_mod
    from lionagi.studio.app import app

    db_file = tmp_path / "test_state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_file)
    monkeypatch.setattr(sched_svc_mod, "DEFAULT_DB_PATH", db_file)
    return TestClient(app, raise_server_exceptions=False)


class TestScheduleArgvInjectionRealService:
    """Router → REAL service → temp DB: each flag-injection field must return 400."""

    def test_create_action_model_flag_real_svc_returns_400(self, monkeypatch, tmp_path) -> None:
        """POST action_model='--bypass' through real service → 400."""
        client = _real_svc_client(monkeypatch, tmp_path)
        r = client.post(
            "/api/schedules/",
            json={
                "name": "bad-model-real",
                "trigger_type": "cron",
                "action_kind": "agent",
                "action_model": "--bypass",
                "action_prompt": "hello",
            },
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
        detail = r.json().get("detail", "")
        assert "--bypass" in detail or "action_model" in detail, (
            f"Expected error mentioning --bypass or action_model, got: {detail!r}"
        )

    def test_create_extra_args_flag_real_svc_returns_400(self, monkeypatch, tmp_path) -> None:
        """POST action_extra_args=['--bypass'] through real service → 400."""
        client = _real_svc_client(monkeypatch, tmp_path)
        r = client.post(
            "/api/schedules/",
            json={
                "name": "bad-extra-real",
                "trigger_type": "cron",
                "action_kind": "agent",
                "action_model": "sonnet",
                "action_prompt": "hello",
                "action_extra_args": ["--bypass"],
            },
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
        detail = r.json().get("detail", "")
        assert "--bypass" in detail or "action_extra_args" in detail, (
            f"Expected error mentioning --bypass or action_extra_args, got: {detail!r}"
        )

    def test_create_action_agent_flag_real_svc_returns_400(self, monkeypatch, tmp_path) -> None:
        """POST action_agent='--bypass' through real service → 400."""
        client = _real_svc_client(monkeypatch, tmp_path)
        r = client.post(
            "/api/schedules/",
            json={
                "name": "bad-agent-real",
                "trigger_type": "cron",
                "action_kind": "agent",
                "action_model": "sonnet",
                "action_prompt": "hello",
                "action_agent": "--bypass",
            },
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
        detail = r.json().get("detail", "")
        assert "--bypass" in detail or "action_agent" in detail, (
            f"Expected error mentioning --bypass or action_agent, got: {detail!r}"
        )

    def test_create_action_project_flag_real_svc_returns_400(self, monkeypatch, tmp_path) -> None:
        """POST action_project='--bypass' through real service → 400."""
        client = _real_svc_client(monkeypatch, tmp_path)
        r = client.post(
            "/api/schedules/",
            json={
                "name": "bad-project-real",
                "trigger_type": "cron",
                "action_kind": "agent",
                "action_model": "sonnet",
                "action_prompt": "hello",
                "action_project": "--bypass",
            },
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
        detail = r.json().get("detail", "")
        assert "--bypass" in detail or "action_project" in detail, (
            f"Expected error mentioning --bypass or action_project, got: {detail!r}"
        )

    def test_create_action_playbook_flag_real_svc_returns_400(self, monkeypatch, tmp_path) -> None:
        """POST action_playbook='--bypass' through real service → 400."""
        client = _real_svc_client(monkeypatch, tmp_path)
        r = client.post(
            "/api/schedules/",
            json={
                "name": "bad-playbook-real",
                "trigger_type": "cron",
                "action_kind": "play",
                "action_model": "sonnet",
                "action_prompt": "hello",
                "action_playbook": "--bypass",
            },
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
        detail = r.json().get("detail", "")
        assert "--bypass" in detail or "action_playbook" in detail, (
            f"Expected error mentioning --bypass or action_playbook, got: {detail!r}"
        )

    def test_create_action_prompt_sentinel_real_svc_returns_400(
        self, monkeypatch, tmp_path
    ) -> None:
        """POST action_prompt='--' through real service → 400.

        The end-of-options token '--' is silently consumed by argparse; reject it.
        """
        client = _real_svc_client(monkeypatch, tmp_path)
        r = client.post(
            "/api/schedules/",
            json={
                "name": "bad-prompt-sentinel",
                "trigger_type": "cron",
                "action_kind": "agent",
                "action_model": "sonnet",
                "action_prompt": "--",
            },
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
        detail = r.json().get("detail", "")
        assert "'--'" in detail or "action_prompt" in detail or "end-of-options" in detail, (
            f"Expected error mentioning '--' or action_prompt, got: {detail!r}"
        )


# ---------------------------------------------------------------------------
# CWE-918 github_repo path manipulation — real-service router tests
# ---------------------------------------------------------------------------


class TestGithubRepoRealServiceValidation:
    """Router → REAL service → temp DB: invalid github_repo values must return 400.

    The test payloads exercise path-traversal sequences, extra slash segments,
    bare owner name (no slash), leading dash, percent-encoded dots, and empty
    string.  A single valid 'owner/repo.name-x_1' case confirms 201 acceptance.
    """

    # -- POST (create) --

    def test_create_path_traversal_returns_400(self, monkeypatch, tmp_path) -> None:
        """POST github_repo='../../other-endpoint' → 400."""
        client = _real_svc_client(monkeypatch, tmp_path)
        r = client.post(
            "/api/schedules/",
            json={
                "name": "gh-traversal",
                "trigger_type": "github_poll",
                "action_kind": "agent",
                "action_model": "sonnet",
                "github_repo": "../../other-endpoint",
            },
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
        detail = r.json().get("detail", "")
        assert "owner/name" in detail or "github_repo" in detail, (
            f"Expected error mentioning owner/name or github_repo, got: {detail!r}"
        )

    def test_create_extra_slash_returns_400(self, monkeypatch, tmp_path) -> None:
        """POST github_repo='owner/name/extra' → 400."""
        client = _real_svc_client(monkeypatch, tmp_path)
        r = client.post(
            "/api/schedules/",
            json={
                "name": "gh-extra-slash",
                "trigger_type": "github_poll",
                "action_kind": "agent",
                "action_model": "sonnet",
                "github_repo": "owner/name/extra",
            },
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"

    def test_create_no_slash_returns_400(self, monkeypatch, tmp_path) -> None:
        """POST github_repo='owner' (no slash) → 400."""
        client = _real_svc_client(monkeypatch, tmp_path)
        r = client.post(
            "/api/schedules/",
            json={
                "name": "gh-no-slash",
                "trigger_type": "github_poll",
                "action_kind": "agent",
                "action_model": "sonnet",
                "github_repo": "owner",
            },
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"

    def test_create_leading_dash_returns_400(self, monkeypatch, tmp_path) -> None:
        """POST github_repo='-owner/repo' (leading dash) → 400."""
        client = _real_svc_client(monkeypatch, tmp_path)
        r = client.post(
            "/api/schedules/",
            json={
                "name": "gh-leading-dash",
                "trigger_type": "github_poll",
                "action_kind": "agent",
                "action_model": "sonnet",
                "github_repo": "-owner/repo",
            },
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"

    def test_create_percent_encoded_returns_400(self, monkeypatch, tmp_path) -> None:
        """POST github_repo='%2e%2e/repo' (percent-encoded dots) → 400."""
        client = _real_svc_client(monkeypatch, tmp_path)
        r = client.post(
            "/api/schedules/",
            json={
                "name": "gh-pct-encoded",
                "trigger_type": "github_poll",
                "action_kind": "agent",
                "action_model": "sonnet",
                "github_repo": "%2e%2e/repo",
            },
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"

    def test_create_valid_repo_accepted(self, monkeypatch, tmp_path) -> None:
        """POST github_repo='owner/repo.name-x_1' → 201 (valid format accepted)."""
        client = _real_svc_client(monkeypatch, tmp_path)
        r = client.post(
            "/api/schedules/",
            json={
                "name": "gh-valid-repo",
                "trigger_type": "github_poll",
                "action_kind": "agent",
                "action_model": "sonnet",
                "github_repo": "owner/repo.name-x_1",
            },
        )
        assert r.status_code == 201, f"expected 201, got {r.status_code}: {r.text}"

    # -- PATCH (update) --

    def test_patch_path_traversal_returns_400(self, monkeypatch, tmp_path) -> None:
        """PATCH github_repo='../../other-endpoint' on existing schedule → 400."""
        client = _real_svc_client(monkeypatch, tmp_path)
        # First create a valid schedule to patch
        create_r = client.post(
            "/api/schedules/",
            json={
                "name": "gh-patch-base",
                "trigger_type": "cron",
                "action_kind": "agent",
                "action_model": "sonnet",
            },
        )
        assert create_r.status_code == 201, f"setup create failed: {create_r.text}"
        sched_id = create_r.json()["id"]

        r = client.patch(
            f"/api/schedules/{sched_id}",
            json={"github_repo": "../../other-endpoint"},
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
        detail = r.json().get("detail", "")
        assert "owner/name" in detail or "github_repo" in detail, (
            f"Expected error mentioning owner/name or github_repo, got: {detail!r}"
        )

    def test_patch_extra_slash_returns_400(self, monkeypatch, tmp_path) -> None:
        """PATCH github_repo='owner/name/extra' on existing schedule → 400."""
        client = _real_svc_client(monkeypatch, tmp_path)
        create_r = client.post(
            "/api/schedules/",
            json={
                "name": "gh-patch-base2",
                "trigger_type": "cron",
                "action_kind": "agent",
                "action_model": "sonnet",
            },
        )
        assert create_r.status_code == 201, f"setup create failed: {create_r.text}"
        sched_id = create_r.json()["id"]

        r = client.patch(
            f"/api/schedules/{sched_id}",
            json={"github_repo": "owner/name/extra"},
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"

    # -- .github repo: positive tests (real service) --

    def test_create_dot_github_repo_accepted(self, monkeypatch, tmp_path) -> None:
        """POST github_repo='github/.github' → 201 (repo starting with '.' is valid)."""
        client = _real_svc_client(monkeypatch, tmp_path)
        r = client.post(
            "/api/schedules/",
            json={
                "name": "gh-dot-github",
                "trigger_type": "github_poll",
                "action_kind": "agent",
                "action_model": "sonnet",
                "github_repo": "github/.github",
            },
        )
        assert r.status_code == 201, f"expected 201, got {r.status_code}: {r.text}"

    def test_patch_dot_github_repo_accepted(self, monkeypatch, tmp_path) -> None:
        """PATCH github_repo='github/.github' on existing schedule → 200."""
        client = _real_svc_client(monkeypatch, tmp_path)
        create_r = client.post(
            "/api/schedules/",
            json={
                "name": "gh-patch-dot-base",
                "trigger_type": "cron",
                "action_kind": "agent",
                "action_model": "sonnet",
            },
        )
        assert create_r.status_code == 201, f"setup create failed: {create_r.text}"
        sched_id = create_r.json()["id"]

        r = client.patch(
            f"/api/schedules/{sched_id}",
            json={"github_repo": "github/.github"},
        )
        assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"

    # -- length bounds: positive tests (real service) --

    def test_create_1000_char_owner_returns_400(self, monkeypatch, tmp_path) -> None:
        """POST github_repo with 1000-char owner → 400."""
        client = _real_svc_client(monkeypatch, tmp_path)
        r = client.post(
            "/api/schedules/",
            json={
                "name": "gh-long-owner",
                "trigger_type": "github_poll",
                "action_kind": "agent",
                "action_model": "sonnet",
                "github_repo": "a" * 1000 + "/repo",
            },
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"

    def test_create_1000_char_repo_returns_400(self, monkeypatch, tmp_path) -> None:
        """POST github_repo with 1000-char repo name → 400."""
        client = _real_svc_client(monkeypatch, tmp_path)
        r = client.post(
            "/api/schedules/",
            json={
                "name": "gh-long-repo",
                "trigger_type": "github_poll",
                "action_kind": "agent",
                "action_model": "sonnet",
                "github_repo": "owner/" + "a" * 1000,
            },
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"

    def test_patch_1000_char_owner_returns_400(self, monkeypatch, tmp_path) -> None:
        """PATCH github_repo with 1000-char owner → 400."""
        client = _real_svc_client(monkeypatch, tmp_path)
        create_r = client.post(
            "/api/schedules/",
            json={
                "name": "gh-patch-long-owner-base",
                "trigger_type": "cron",
                "action_kind": "agent",
                "action_model": "sonnet",
            },
        )
        assert create_r.status_code == 201, f"setup create failed: {create_r.text}"
        sched_id = create_r.json()["id"]

        r = client.patch(
            f"/api/schedules/{sched_id}",
            json={"github_repo": "a" * 1000 + "/repo"},
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"

    def test_patch_1000_char_repo_returns_400(self, monkeypatch, tmp_path) -> None:
        """PATCH github_repo with 1000-char repo name → 400."""
        client = _real_svc_client(monkeypatch, tmp_path)
        create_r = client.post(
            "/api/schedules/",
            json={
                "name": "gh-patch-long-repo-base",
                "trigger_type": "cron",
                "action_kind": "agent",
                "action_model": "sonnet",
            },
        )
        assert create_r.status_code == 201, f"setup create failed: {create_r.text}"
        sched_id = create_r.json()["id"]

        r = client.patch(
            f"/api/schedules/{sched_id}",
            json={"github_repo": "owner/" + "a" * 1000},
        )
        assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"

    # -- PATCH preserves bad stored value (MAJOR #3) --

    def test_patch_unrelated_field_rejects_bad_stored_github_repo(
        self, monkeypatch, tmp_path
    ) -> None:
        """PATCH of an unrelated field must return 400 when stored github_repo is invalid.

        Seed a schedule directly via DB with a bad github_repo, then send an
        unrelated PATCH (description only).  The effective-dict revalidation must
        catch the stale-invalid value and return 400, not 200.
        """
        import aiosqlite

        from tests.apps_studio_server._helpers import run_async

        # 1. Build the temp DB and wire the app to it.
        db_file = tmp_path / "test_state.db"
        import lionagi.state.db as state_db_mod
        import lionagi.studio.services.schedules as sched_svc_mod
        from lionagi.studio.app import app

        monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_file)
        monkeypatch.setattr(sched_svc_mod, "DEFAULT_DB_PATH", db_file)

        # 2. Use the real service to create the schema (create a throw-away schedule).
        from fastapi.testclient import TestClient

        client = TestClient(app, raise_server_exceptions=False)
        setup_r = client.post(
            "/api/schedules/",
            json={
                "name": "schema-seed",
                "trigger_type": "cron",
                "action_kind": "agent",
                "action_model": "sonnet",
            },
        )
        assert setup_r.status_code == 201, f"schema seed failed: {setup_r.text}"

        # 3. Directly insert a schedule with a bad github_repo into the temp DB.
        bad_id = "badstored1"
        import time

        now = time.time()

        async def _insert():
            async with aiosqlite.connect(db_file) as conn:
                await conn.execute(
                    """
                    INSERT INTO schedules
                      (id, name, trigger_type, action_kind, github_repo,
                       enabled, missed_fire_policy, overlap_policy,
                       created_at, updated_at)
                    VALUES (?,?,?,?,?,1,'skip','skip',?,?)
                    """,
                    (bad_id, "bad-stored-gh", "cron", "agent", "../../x", now, now),
                )
                await conn.commit()

        run_async(_insert())

        # 4. PATCH an unrelated field -- effective github_repo is still "../../x".
        r = client.patch(
            f"/api/schedules/{bad_id}",
            json={"description": "innocuous update"},
        )
        assert r.status_code == 400, (
            f"expected 400 for unrelated PATCH with bad stored github_repo, "
            f"got {r.status_code}: {r.text}"
        )
        detail = r.json().get("detail", "")
        assert "github_repo" in detail or "owner/name" in detail or "traversal" in detail, (
            f"Expected error mentioning github_repo/owner/name/traversal, got: {detail!r}"
        )
