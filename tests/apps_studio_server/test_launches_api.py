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
