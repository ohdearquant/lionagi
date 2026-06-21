# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for POST /api/launches — on-demand run launch endpoint."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(monkeypatch, fake_db: Path | None = None) -> TestClient:
    from importlib import reload

    import lionagi.studio.app as app_mod
    import lionagi.studio.services.stats as stats_mod

    if fake_db is not None:
        monkeypatch.setattr(stats_mod, "DEFAULT_DB_PATH", fake_db)
        monkeypatch.setattr(stats_mod, "_DB", str(fake_db))

    reload(app_mod)
    return TestClient(app_mod.app, raise_server_exceptions=False)


def _stub_db_and_spawn(monkeypatch):
    """Patch StateDB.create_invocation and spawn so no real I/O happens."""
    mock_db = AsyncMock()
    mock_db.create_invocation = AsyncMock()
    mock_db.update_invocation = AsyncMock()
    mock_db.update_status = AsyncMock()

    db_ctx = MagicMock()
    db_ctx.__aenter__ = AsyncMock(return_value=mock_db)
    db_ctx.__aexit__ = AsyncMock(return_value=False)

    monkeypatch.setattr(
        "lionagi.studio.services.launches.StateDB",
        lambda *a, **kw: db_ctx,
    )

    def _consume_create_task(coro, **kw):
        # Close the coroutine to prevent 'never awaited' ResourceWarning.
        coro.close()
        return MagicMock()

    monkeypatch.setattr(
        "lionagi.studio.services.launches.asyncio.create_task",
        _consume_create_task,
    )
    return mock_db


@pytest.fixture(autouse=True)
def _fresh_launch_state():
    """Reset module-global launch state to prevent slot and task-ref leaks between tests."""
    import lionagi.studio.services.launches as svc

    svc._launch_semaphore = None
    svc._detached_tasks.clear()
    svc._user_cancelled.clear()
    yield
    svc._launch_semaphore = None
    svc._detached_tasks.clear()
    svc._user_cancelled.clear()


# ---------------------------------------------------------------------------
# Basic happy-path
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLaunchHappyPath:
    def test_launch_agent_returns_202(self, tmp_path, monkeypatch):
        """POST /api/launches with action_kind=agent must return 202."""
        _stub_db_and_spawn(monkeypatch)
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={"action_kind": "agent", "action_model": "sonnet", "action_prompt": "hello"},
        )
        assert resp.status_code == 202, resp.text
        data = resp.json()
        assert "invocation_id" in data
        assert data["action_kind"] == "agent"

    def test_launch_flow_returns_202(self, tmp_path, monkeypatch):
        """POST /api/launches with action_kind=flow must return 202."""
        _stub_db_and_spawn(monkeypatch)
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={"action_kind": "flow", "action_model": "sonnet", "action_prompt": "hi"},
        )
        assert resp.status_code == 202, resp.text

    def test_launch_fanout_returns_202(self, tmp_path, monkeypatch):
        """POST /api/launches with action_kind=fanout must return 202."""
        _stub_db_and_spawn(monkeypatch)
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={"action_kind": "fanout", "action_model": "sonnet", "action_prompt": "go"},
        )
        assert resp.status_code == 202, resp.text

    def test_launch_play_returns_202(self, tmp_path, monkeypatch):
        """POST /api/launches with action_kind=play must return 202."""
        _stub_db_and_spawn(monkeypatch)
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={"action_kind": "play", "action_playbook": "my-playbook"},
        )
        assert resp.status_code == 202, resp.text

    def test_response_contains_invocation_id_and_kind(self, tmp_path, monkeypatch):
        """Response body must include invocation_id and action_kind."""
        _stub_db_and_spawn(monkeypatch)
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={"action_kind": "agent", "action_model": "sonnet"},
        )
        assert resp.status_code == 202
        data = resp.json()
        assert isinstance(data["invocation_id"], str)
        assert len(data["invocation_id"]) == 12
        assert data["action_kind"] == "agent"


# ---------------------------------------------------------------------------
# Invalid action_kind
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLaunchInvalidKind:
    def test_unknown_kind_returns_422(self, tmp_path, monkeypatch):
        """Unknown action_kind must return 422."""
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post("/api/launches", json={"action_kind": "magic"})
        assert resp.status_code == 422, resp.text

    def test_flow_yaml_kind_rejected(self, tmp_path, monkeypatch):
        """flow_yaml is not supported for on-demand launches — must return 422."""
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={"action_kind": "flow_yaml", "action_flow_yaml": "prompt: hi\n"},
        )
        assert resp.status_code == 422, resp.text

    def test_missing_action_kind_returns_422(self, tmp_path, monkeypatch):
        """Missing action_kind must return 422 (Pydantic required field)."""
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post("/api/launches", json={"action_model": "sonnet"})
        assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Injection rejection — validation goes through build_argv
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLaunchInjectionRejection:
    def test_model_flag_injection_rejected(self, tmp_path, monkeypatch):
        """action_model starting with '-' must be rejected with 422."""
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={"action_kind": "agent", "action_model": "--bypass"},
        )
        assert resp.status_code == 422, resp.text

    def test_agent_flag_injection_rejected(self, tmp_path, monkeypatch):
        """action_agent starting with '-' must be rejected with 422."""
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={"action_kind": "agent", "action_agent": "--bypass"},
        )
        assert resp.status_code == 422, resp.text

    def test_project_flag_injection_rejected(self, tmp_path, monkeypatch):
        """action_project starting with '-' must be rejected with 422."""
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={"action_kind": "agent", "action_project": "--yolo"},
        )
        assert resp.status_code == 422, resp.text

    def test_playbook_flag_injection_rejected(self, tmp_path, monkeypatch):
        """action_playbook starting with '-' must be rejected with 422."""
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={"action_kind": "play", "action_playbook": "--bypass"},
        )
        assert resp.status_code == 422, resp.text

    def test_extra_args_flag_injection_rejected(self, tmp_path, monkeypatch):
        """action_extra_args containing a flag must be rejected with 422."""
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={"action_kind": "agent", "action_extra_args": ["--bypass"]},
        )
        assert resp.status_code == 422, resp.text

    def test_prompt_sentinel_rejected(self, tmp_path, monkeypatch):
        """action_prompt == '--' must be rejected with 422."""
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={"action_kind": "agent", "action_prompt": "--"},
        )
        assert resp.status_code == 422, resp.text

    def test_model_invalid_chars_rejected(self, tmp_path, monkeypatch):
        """action_model with semicolons must be rejected with 422."""
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={"action_kind": "agent", "action_model": "gpt; rm -rf /"},
        )
        assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Auth coverage — launch endpoint covered by bearer middleware
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLaunchAuth:
    def test_launch_requires_bearer_when_token_set(self, monkeypatch, tmp_path):
        """POST /api/launches must return 401 without auth when token is set."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "test-launch-secret")
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={"action_kind": "agent"},
        )
        assert resp.status_code == 401, f"Expected 401 (no auth header), got {resp.status_code}"

    def test_launch_wrong_token_returns_401(self, monkeypatch, tmp_path):
        """POST /api/launches with wrong token must return 401."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "correct-secret")
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={"action_kind": "agent"},
            headers={"Authorization": "Bearer wrong-secret"},
        )
        assert resp.status_code == 401

    def test_launch_correct_token_not_401(self, monkeypatch, tmp_path):
        """POST /api/launches with correct token must not return 401."""
        monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "correct-secret")
        _stub_db_and_spawn(monkeypatch)
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={"action_kind": "agent", "action_model": "sonnet"},
            headers={"Authorization": "Bearer correct-secret"},
        )
        assert resp.status_code != 401

    def test_launch_open_when_no_token_configured(self, monkeypatch, tmp_path):
        """Without LIONAGI_STUDIO_AUTH_TOKEN, launch is accessible."""
        monkeypatch.delenv("LIONAGI_STUDIO_AUTH_TOKEN", raising=False)
        _stub_db_and_spawn(monkeypatch)
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={"action_kind": "agent", "action_model": "sonnet"},
        )
        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# argv goes through build_argv (validated shared path)
# ---------------------------------------------------------------------------


class TestLaunchArgvPath:
    """launch() must call build_argv with the correct schedule-dict shape."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_launch_calls_build_argv(self):
        """launch() delegates argv construction to build_argv."""
        captured = {}

        def _fake_build_argv(schedule, ctx):
            captured["schedule"] = schedule
            captured["ctx"] = ctx
            return (["uv", "run", "li", "agent", "--", "sonnet", "hi"], None)

        with (
            patch("lionagi.studio.services.launches.build_argv", side_effect=_fake_build_argv),
            patch("lionagi.studio.services.launches.StateDB") as MockDB,
            patch("lionagi.studio.services.launches.asyncio.create_task"),
        ):
            mock_db = AsyncMock()
            mock_db.create_invocation = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            self._run(
                __import__("lionagi.studio.services.launches", fromlist=["launch"]).launch(
                    {"action_kind": "agent", "action_model": "sonnet", "action_prompt": "hi"}
                )
            )

        assert "schedule" in captured
        assert captured["schedule"]["action_kind"] == "agent"
        assert captured["schedule"]["action_model"] == "sonnet"
        assert captured["schedule"]["action_prompt"] == "hi"
        assert captured["ctx"] == {}

    def test_launch_returns_invocation_id_on_agent(self):
        """launch() must return a 12-char hex invocation_id for agent kind."""

        def _consume(coro, **kw):
            coro.close()
            return MagicMock()

        with (
            patch("lionagi.studio.services.launches.StateDB") as MockDB,
            patch("lionagi.studio.services.launches.asyncio.create_task", side_effect=_consume),
        ):
            mock_db = AsyncMock()
            mock_db.create_invocation = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)

            result = self._run(
                __import__("lionagi.studio.services.launches", fromlist=["launch"]).launch(
                    {"action_kind": "agent", "action_model": "sonnet", "action_prompt": "hello"}
                )
            )

        assert "invocation_id" in result
        assert len(result["invocation_id"]) == 12

    def test_validate_request_rejects_flag_model(self):
        """_validate_request must reject action_model starting with '-'."""
        from lionagi.studio.services.launches import _validate_request

        with pytest.raises(ValueError, match="starts with '-'"):
            _validate_request({"action_kind": "agent", "action_model": "--bypass"})

    def test_validate_request_rejects_unknown_kind(self):
        """_validate_request must reject unknown action_kind."""
        from lionagi.studio.services.launches import _validate_request

        with pytest.raises(ValueError, match="not supported"):
            _validate_request({"action_kind": "unknown"})

    def test_validate_request_rejects_flow_yaml_kind(self):
        """_validate_request must reject flow_yaml action_kind."""
        from lionagi.studio.services.launches import _validate_request

        with pytest.raises(ValueError, match="not supported"):
            _validate_request({"action_kind": "flow_yaml"})

    def test_validate_request_accepts_valid_agent(self):
        """_validate_request must not raise for a clean agent request."""
        from lionagi.studio.services.launches import _validate_request

        _validate_request(
            {"action_kind": "agent", "action_model": "sonnet", "action_prompt": "hello"}
        )

    def test_validate_request_accepts_valid_play(self):
        """_validate_request must not raise for a clean play request."""
        from lionagi.studio.services.launches import _validate_request

        _validate_request({"action_kind": "play", "action_playbook": "my-playbook"})


# ---------------------------------------------------------------------------
# _spawn_detached terminal update uses registered reason codes
# ---------------------------------------------------------------------------


class TestSpawnDetachedTerminalUpdate:
    """The post-exit invocation update must use codes the reason registry accepts."""

    def _capture_update(self, exit_code=0, spawn_exc=None):
        from lionagi.studio.services import launches

        captured = {}

        async def _fake_spawn(argv, inv_id, *, tmp_path=None):
            if spawn_exc is not None:
                raise spawn_exc
            return (exit_code, "")

        mock_db = AsyncMock()

        async def _capture_status(entity_type, entity_id, **kw):
            captured["entity_type"] = entity_type
            captured.update(kw)

        mock_db.update_status = _capture_status

        with patch("lionagi.studio.services.launches.StateDB") as MockDB:
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch(
                "lionagi.studio.scheduler.subprocess.spawn_and_wait",
                side_effect=_fake_spawn,
            ):
                asyncio.run(launches._spawn_detached(["uv", "run", "li"], "inv1", tmp_path=None))
        return captured

    @pytest.mark.parametrize(
        ("exit_code", "spawn_exc", "want_status"),
        [
            (0, None, "completed"),
            (3, None, "failed"),
            (None, RuntimeError("spawn blew up"), "failed"),
        ],
    )
    def test_reason_code_is_registered(self, exit_code, spawn_exc, want_status):
        """Every terminal path must emit a reason_code the registry validates."""
        from lionagi.state.reasons import validate_reason_code

        captured = self._capture_update(exit_code=exit_code, spawn_exc=spawn_exc)
        assert captured["new_status"] == want_status
        validate_reason_code(captured["reason_code"])


# ---------------------------------------------------------------------------
# MAJOR 1 — Admission cap: 429 when in-flight launches >= MAX_LAUNCHES
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestLaunchAdmissionCap:
    """POST /api/launches must return 429 when the in-flight cap is saturated."""

    def test_429_when_slots_exhausted(self, tmp_path, monkeypatch):
        """When all semaphore slots are held, the next request returns 429."""
        import lionagi.studio.services.launches as svc

        _stub_db_and_spawn(monkeypatch)
        # A zero-value semaphore is indistinguishable from "every slot held".
        svc._launch_semaphore = asyncio.Semaphore(0)

        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")
        resp = client.post(
            "/api/launches",
            json={"action_kind": "agent", "action_model": "sonnet", "action_prompt": "hi"},
        )
        assert resp.status_code == 429, resp.text
        assert "Maximum concurrent launches" in resp.json()["detail"]

    def test_no_row_or_task_when_saturated(self, tmp_path, monkeypatch):
        """When the cap is saturated, neither a DB row nor a task is created."""
        import lionagi.studio.services.launches as svc

        mock_db = _stub_db_and_spawn(monkeypatch)
        spawn_calls = []

        def _track_spawn(coro, **kw):
            spawn_calls.append(coro)
            coro.close()
            return MagicMock()

        monkeypatch.setattr(
            "lionagi.studio.services.launches.asyncio.create_task",
            _track_spawn,
        )
        svc._launch_semaphore = asyncio.Semaphore(0)

        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")
        resp = client.post(
            "/api/launches",
            json={"action_kind": "agent", "action_model": "sonnet"},
        )
        assert resp.status_code == 429
        assert spawn_calls == [], "create_task must not be called when cap is saturated"
        mock_db.create_invocation.assert_not_called()

    def test_burst_admission_capped_before_any_task_runs(self, tmp_path, monkeypatch):
        """With cap 1, the second POST gets 429 before the first task starts (slots taken at admission)."""
        import lionagi.studio.services.launches as svc

        _stub_db_and_spawn(monkeypatch)
        monkeypatch.setattr(svc.config, "MAX_LAUNCHES", 1)

        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")
        body = {"action_kind": "agent", "action_model": "sonnet", "action_prompt": "hi"}
        first = client.post("/api/launches", json=body)
        second = client.post("/api/launches", json=body)
        assert first.status_code == 202, first.text
        assert second.status_code == 429, second.text


# ---------------------------------------------------------------------------
# MAJOR 2 — Shutdown drains _detached_tasks + writes cancelled row
# ---------------------------------------------------------------------------


class TestShutdownDrains:
    """shutdown_launches() must cancel in-flight tasks and await completion."""

    def test_shutdown_cancels_tasks(self):
        """shutdown_launches() cancels all tasks in _detached_tasks."""
        import lionagi.studio.services.launches as svc

        cancelled = []

        async def _run():
            # Clear any leftover tasks from previous tests in other event loops.
            svc._detached_tasks.clear()

            # Plant a fake long-running task.
            async def _long():
                try:
                    await asyncio.sleep(999)
                except asyncio.CancelledError:
                    cancelled.append(True)
                    raise

            task = asyncio.ensure_future(_long())
            svc._detached_tasks.add(task)
            task.add_done_callback(svc._detached_tasks.discard)
            # Yield so _long() reaches its first await before we cancel.
            await asyncio.sleep(0)

            await svc.shutdown_launches()

        asyncio.run(_run())
        assert cancelled == [True], "Task must have been cancelled"
        assert len(svc._detached_tasks) == 0, "_detached_tasks must be empty after shutdown"

    def test_shutdown_no_tasks_is_noop(self):
        """shutdown_launches() with no tasks must not raise."""
        import lionagi.studio.services.launches as svc

        svc._detached_tasks.clear()
        asyncio.run(svc.shutdown_launches())  # must not raise

    def test_spawn_detached_cancelled_writes_terminal_row(self):
        """When _spawn_detached is cancelled, it writes a cancelled row before re-raising."""
        import lionagi.studio.services.launches as svc
        from lionagi.state.reasons import RunReasons, validate_reason_code

        captured = {}

        mock_db = AsyncMock()

        async def _capture_status(entity_type, entity_id, **kw):
            captured["new_status"] = kw.get("new_status")
            captured["reason_code"] = kw.get("reason_code")

        mock_db.update_status = _capture_status
        mock_db.update_invocation = AsyncMock()

        async def _blocking_spawn(argv, inv_id, *, tmp_path=None):
            # Block until cancelled so CancelledError propagates naturally.
            await asyncio.sleep(999)
            return (0, "")

        async def _run():
            async def _inner():
                with patch("lionagi.studio.services.launches.StateDB") as MockDB:
                    MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
                    MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
                    with patch(
                        "lionagi.studio.scheduler.subprocess.spawn_and_wait",
                        side_effect=_blocking_spawn,
                    ):
                        await svc._spawn_detached(["uv", "run", "li"], "inv-cancel", tmp_path=None)

            task = asyncio.ensure_future(_inner())
            # Yield twice: once to enter _inner(), once to enter _blocking_spawn().
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run())
        assert captured.get("new_status") == "cancelled"
        validate_reason_code(captured.get("reason_code"))
        assert captured["reason_code"] == RunReasons.CANCELLED_SYSTEM


# ---------------------------------------------------------------------------
# MAJOR 3 — build_argv called BEFORE create_invocation
# ---------------------------------------------------------------------------


class TestBuildArgvBeforeCreate:
    """build_argv must be called before create_invocation; if it raises, no row is created."""

    def test_build_argv_failure_leaves_no_invocation_row(self):
        """If build_argv raises ValueError, create_invocation must not be called."""
        import lionagi.studio.services.launches as svc

        create_calls = []

        mock_db = AsyncMock()
        mock_db.create_invocation = AsyncMock(side_effect=lambda d: create_calls.append(d))

        async def _run():
            with (
                patch("lionagi.studio.services.launches.StateDB") as MockDB,
                patch(
                    "lionagi.studio.services.launches.build_argv",
                    side_effect=ValueError("argv build failed"),
                ),
            ):
                MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
                MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
                with pytest.raises(ValueError, match="argv build failed"):
                    await svc.launch({"action_kind": "agent", "action_model": "sonnet"})

        asyncio.run(_run())
        assert create_calls == [], "create_invocation must NOT be called if build_argv raises"

    def test_build_argv_failure_returns_422_via_router(self, tmp_path, monkeypatch):
        """If build_argv raises ValueError, the endpoint must return 422 with no DB write."""
        create_calls = []

        mock_db = AsyncMock()
        mock_db.create_invocation = AsyncMock(side_effect=lambda d: create_calls.append(d))
        db_ctx = MagicMock()
        db_ctx.__aenter__ = AsyncMock(return_value=mock_db)
        db_ctx.__aexit__ = AsyncMock(return_value=False)
        monkeypatch.setattr("lionagi.studio.services.launches.StateDB", lambda *a, **kw: db_ctx)
        monkeypatch.setattr(
            "lionagi.studio.services.launches.build_argv",
            lambda *a, **kw: (_ for _ in ()).throw(ValueError("forced argv failure")),
        )

        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")
        resp = client.post(
            "/api/launches",
            json={"action_kind": "agent", "action_model": "sonnet"},
        )
        assert resp.status_code == 422, resp.text
        assert create_calls == [], "create_invocation must NOT be called if build_argv raises"


# ---------------------------------------------------------------------------
# Engine kind — launch a saved engine definition
# ---------------------------------------------------------------------------


def _stub_engine_def(monkeypatch, defn: dict | None, *, by_name_only: bool = False):
    """Patch the engine-defs lookups that _resolve_engine_def calls."""
    import lionagi.studio.services.engine_defs as ed

    monkeypatch.setattr(
        ed, "get_engine_def", AsyncMock(return_value=None if by_name_only else defn)
    )
    monkeypatch.setattr(ed, "get_engine_def_by_name", AsyncMock(return_value=defn))


_ENGINE_DEF = {
    "id": "abc123abc123",
    "name": "my-engine",
    "kind": "research",
    "model": "gpt-4.1-mini",
    "max_depth": 3,
    "max_agents": None,
    "options": {"test_cmd": "pytest"},
}


@pytest.mark.integration
class TestLaunchEngineKind:
    def test_launch_engine_returns_202(self, tmp_path, monkeypatch):
        """POST /api/launches with action_kind=engine fires a saved definition."""
        _stub_db_and_spawn(monkeypatch)
        _stub_engine_def(monkeypatch, dict(_ENGINE_DEF))
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={
                "action_kind": "engine",
                "action_engine_def": "abc123abc123",
                "action_prompt": "find recent papers on GQA",
            },
        )
        assert resp.status_code == 202, resp.text
        data = resp.json()
        assert "invocation_id" in data
        assert data["action_kind"] == "engine"

    def test_launch_engine_resolves_by_name(self, tmp_path, monkeypatch):
        """A definition not found by id is resolved by name."""
        _stub_db_and_spawn(monkeypatch)
        _stub_engine_def(monkeypatch, dict(_ENGINE_DEF), by_name_only=True)
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={
                "action_kind": "engine",
                "action_engine_def": "my-engine",
                "action_prompt": "plan the migration",
            },
        )
        assert resp.status_code == 202, resp.text

    def test_launch_engine_unknown_def_422(self, tmp_path, monkeypatch):
        """An unresolvable definition reference returns 422, not 500."""
        _stub_db_and_spawn(monkeypatch)
        _stub_engine_def(monkeypatch, None)
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={
                "action_kind": "engine",
                "action_engine_def": "nonexistent",
                "action_prompt": "hello",
            },
        )
        assert resp.status_code == 422, resp.text
        assert "not found" in resp.json()["detail"]

    def test_launch_engine_missing_def_422(self, tmp_path, monkeypatch):
        """action_engine_def is required for engine launches."""
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={"action_kind": "engine", "action_prompt": "hello"},
        )
        assert resp.status_code == 422, resp.text

    def test_launch_engine_missing_prompt_422(self, tmp_path, monkeypatch):
        """action_prompt (the engine spec) is required for engine launches."""
        _stub_engine_def(monkeypatch, dict(_ENGINE_DEF))
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={"action_kind": "engine", "action_engine_def": "my-engine"},
        )
        assert resp.status_code == 422, resp.text

    def test_launch_engine_def_flag_injection_rejected(self, tmp_path, monkeypatch):
        """action_engine_def starting with '-' must be rejected with 422."""
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={
                "action_kind": "engine",
                "action_engine_def": "--bypass",
                "action_prompt": "hello",
            },
        )
        assert resp.status_code == 422, resp.text


class TestEngineScheduleAssembly:
    """launch() must assemble the schedule dict from the saved definition."""

    def _launch_and_capture(self, monkeypatch, defn, request):
        import lionagi.studio.services.launches as svc

        _stub_engine_def(monkeypatch, defn)
        captured = {}

        def _fake_build_argv(schedule, ctx):
            captured["schedule"] = schedule
            return (["uv", "run", "li", "engine", "run", "--", "research", "spec"], None)

        def _consume(coro, **kw):
            coro.close()
            return MagicMock()

        with (
            patch("lionagi.studio.services.launches.build_argv", side_effect=_fake_build_argv),
            patch("lionagi.studio.services.launches.StateDB") as MockDB,
            patch("lionagi.studio.services.launches.asyncio.create_task", side_effect=_consume),
        ):
            mock_db = AsyncMock()
            mock_db.create_invocation = AsyncMock()
            MockDB.return_value.__aenter__ = AsyncMock(return_value=mock_db)
            MockDB.return_value.__aexit__ = AsyncMock(return_value=False)
            asyncio.run(svc.launch(request))
        return captured["schedule"]

    def test_def_fields_flow_into_schedule(self, monkeypatch):
        """Definition kind, model, limits, and options reach build_argv."""
        defn = {
            "id": "def123def123",
            "name": "deep-coder",
            "kind": "coding",
            "model": "gpt-4o",
            "max_depth": 2,
            "max_agents": 4,
            "options": {"test_cmd": "uv run pytest", "export_dir": "out"},
        }
        schedule = self._launch_and_capture(
            monkeypatch,
            defn,
            {
                "action_kind": "engine",
                "action_engine_def": "deep-coder",
                "action_prompt": "build a web crawler",
            },
        )
        assert schedule["action_kind"] == "engine"
        assert schedule["action_agent"] == "coding"
        assert schedule["action_model"] == "gpt-4o"
        assert schedule["action_prompt"] == "build a web crawler"
        assert schedule["action_engine_options"] == {
            "test_cmd": "uv run pytest",
            "export_dir": "out",
            "max_depth": 2,
            "max_agents": 4,
        }

    def test_request_model_overrides_def_model(self, monkeypatch):
        """An explicit action_model in the request wins over the saved default."""
        defn = dict(_ENGINE_DEF)
        schedule = self._launch_and_capture(
            monkeypatch,
            defn,
            {
                "action_kind": "engine",
                "action_engine_def": "my-engine",
                "action_model": "claude-sonnet-4-5",
                "action_prompt": "survey attention papers",
            },
        )
        assert schedule["action_model"] == "claude-sonnet-4-5"


# ---------------------------------------------------------------------------
# build_argv engine kind — argv shape (flags first, '--', then positionals)
# ---------------------------------------------------------------------------


class TestBuildArgvEngineKind:
    def test_basic_shape(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        schedule = {
            "action_kind": "engine",
            "action_model": "gpt-4o",
            "action_prompt": "analyse the dataset",
            "action_agent": "research",
            "action_engine_options": {},
        }
        argv, tmp = build_argv(schedule, {})
        assert tmp is None
        assert argv[:5] == ["uv", "run", "li", "engine", "run"]
        sep_idx = argv.index("--")
        assert argv[sep_idx + 1] == "research"
        assert argv[sep_idx + 2] == "analyse the dataset"
        flags_part = argv[5:sep_idx]
        assert "--model" in flags_part
        assert flags_part[flags_part.index("--model") + 1] == "gpt-4o"

    def test_exact_argv_with_all_options(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        schedule = {
            "action_kind": "engine",
            "action_model": "claude-sonnet-4-5",
            "action_prompt": "build me a web crawler",
            "action_agent": "coding",
            "action_engine_options": {
                "max_depth": 2,
                "max_agents": 4,
                "test_cmd": "uv run pytest",
                "export_dir": None,
            },
        }
        argv, _ = build_argv(schedule, {})
        assert argv == [
            "uv",
            "run",
            "li",
            "engine",
            "run",
            "--model",
            "claude-sonnet-4-5",
            "--max-depth",
            "2",
            "--max-agents",
            "4",
            "--test-cmd",
            "uv run pytest",
            "--",
            "coding",
            "build me a web crawler",
        ]

    def test_no_flags_when_empty(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        schedule = {
            "action_kind": "engine",
            "action_model": "",
            "action_prompt": "plan a project",
            "action_agent": "planning",
            "action_engine_options": {},
        }
        argv, _ = build_argv(schedule, {})
        assert argv == ["uv", "run", "li", "engine", "run", "--", "planning", "plan a project"]

    def test_missing_engine_kind_rejected(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        schedule = {
            "action_kind": "engine",
            "action_model": "",
            "action_prompt": "plan a project",
            "action_agent": None,
            "action_engine_options": {},
        }
        with pytest.raises(ValueError, match="engine kind"):
            build_argv(schedule, {})

    def test_missing_prompt_rejected(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        schedule = {
            "action_kind": "engine",
            "action_model": "",
            "action_prompt": "",
            "action_agent": "planning",
            "action_engine_options": {},
        }
        with pytest.raises(ValueError, match="engine spec"):
            build_argv(schedule, {})

    @pytest.mark.parametrize(
        "opts",
        [
            {"test_cmd": "--bypass"},
            {"export_dir": "-o"},
            {"test_cmd": "pytest; rm -rf /"},
            {"max_depth": 0},
            {"max_depth": 101},
            {"max_agents": "five"},
            {"unknown_key": "x"},
        ],
    )
    def test_unsafe_engine_options_rejected(self, opts):
        from lionagi.studio.scheduler.subprocess import build_argv

        schedule = {
            "action_kind": "engine",
            "action_model": "",
            "action_prompt": "do the thing",
            "action_agent": "coding",
            "action_engine_options": opts,
        }
        with pytest.raises(ValueError):
            build_argv(schedule, {})

    def test_extra_args_suppressed_for_engine(self):
        """action_extra_args must not be appended to engine argv."""
        from lionagi.studio.scheduler.subprocess import build_argv

        schedule = {
            "action_kind": "engine",
            "action_model": "",
            "action_prompt": "do the thing",
            "action_agent": "research",
            "action_engine_options": {},
            "action_extra_args": ["positional-token"],
        }
        argv, _ = build_argv(schedule, {})
        assert "positional-token" not in argv


class TestCodingKindRequiresTestCmd:
    """The engine CLI exits nonzero for 'coding' without --test-cmd; both
    build_argv and the launch path must reject it before any row is created."""

    def test_build_argv_coding_without_test_cmd_rejected(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        schedule = {
            "action_kind": "engine",
            "action_model": "",
            "action_prompt": "build a parser",
            "action_agent": "coding",
            "action_engine_options": {"export_dir": "out"},
        }
        with pytest.raises(ValueError, match="test_cmd"):
            build_argv(schedule, {})

    def test_launch_stored_coding_def_without_test_cmd_422_no_row(self, tmp_path, monkeypatch):
        """A stored coding definition lacking test_cmd (e.g. written before
        validation existed) must produce 422 and no invocation row."""
        mock_db = _stub_db_and_spawn(monkeypatch)
        _stub_engine_def(
            monkeypatch,
            {
                "id": "bad123bad123",
                "name": "bad-coder",
                "kind": "coding",
                "model": None,
                "max_depth": None,
                "max_agents": None,
                "options": None,
            },
        )
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={
                "action_kind": "engine",
                "action_engine_def": "bad-coder",
                "action_prompt": "build a parser",
            },
        )
        assert resp.status_code == 422, resp.text
        assert "test_cmd" in resp.json()["detail"]
        mock_db.create_invocation.assert_not_called()

    def test_build_argv_coding_whitespace_test_cmd_rejected(self):
        from lionagi.studio.scheduler.subprocess import build_argv

        schedule = {
            "action_kind": "engine",
            "action_model": "",
            "action_prompt": "build a parser",
            "action_agent": "coding",
            "action_engine_options": {"test_cmd": "   "},
        }
        with pytest.raises(ValueError, match="test_cmd"):
            build_argv(schedule, {})

    def test_launch_stored_coding_def_whitespace_test_cmd_422_no_row(self, tmp_path, monkeypatch):
        mock_db = _stub_db_and_spawn(monkeypatch)
        _stub_engine_def(
            monkeypatch,
            {
                "id": "ws1234ws1234",
                "name": "ws-coder",
                "kind": "coding",
                "model": None,
                "max_depth": None,
                "max_agents": None,
                "options": {"test_cmd": "   "},
            },
        )
        client = _make_client(monkeypatch, fake_db=tmp_path / "state.db")

        resp = client.post(
            "/api/launches",
            json={
                "action_kind": "engine",
                "action_engine_def": "ws-coder",
                "action_prompt": "build a parser",
            },
        )
        assert resp.status_code == 422, resp.text
        assert "test_cmd" in resp.json()["detail"]
        mock_db.create_invocation.assert_not_called()


# ---------------------------------------------------------------------------
# cancel_launch — POST /api/invocations/{id}/cancel
# ---------------------------------------------------------------------------


class TestCancelLaunch:
    """cancel_launch() cancels an in-flight task and raises 404 for unknown ids."""

    def test_cancel_returns_cancelling(self):
        """cancel_launch returns {invocation_id, status:'cancelling'} for an in-flight task."""
        import contextlib

        import lionagi.studio.services.launches as svc

        async def _run():
            svc._detached_tasks.clear()
            svc._user_cancelled.clear()
            task = asyncio.create_task(asyncio.sleep(3600), name="launch-TESTINV")
            svc._detached_tasks.add(task)
            result = await svc.cancel_launch("TESTINV")
            assert result == {"invocation_id": "TESTINV", "status": "cancelling"}
            with contextlib.suppress(asyncio.CancelledError):
                await task
            assert task.cancelled()
            svc._detached_tasks.discard(task)
            svc._user_cancelled.discard("TESTINV")

        asyncio.run(_run())

    def test_cancel_missing_raises_404(self):
        """cancel_launch raises HTTPException(404) for an unknown invocation_id."""
        from fastapi import HTTPException

        import lionagi.studio.services.launches as svc

        async def _run():
            svc._detached_tasks.clear()
            with pytest.raises(HTTPException) as exc_info:
                await svc.cancel_launch("missing")
            assert exc_info.value.status_code == 404

        asyncio.run(_run())
