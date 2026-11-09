"""Hostname failures during quarantine rechecks report a probe failure."""

from __future__ import annotations

import socket
import uuid
from unittest.mock import Mock

import pytest

from lionagi.cli import kill as kill_cli
from lionagi.config import settings
from lionagi.state.db import StateDB


@pytest.mark.parametrize("kind", ["session", "invocation"])
@pytest.mark.parametrize(
    "successful_host_checks", [pytest.param(0, id="first"), pytest.param(1, id="locked")]
)
async def test_hostname_failure_is_reported_without_state_change(
    kind, successful_host_checks, monkeypatch, tmp_path
):
    path = tmp_path / "state.db"
    monkeypatch.setattr(
        "lionagi.state.db.settings", settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    entity_id = str(uuid.uuid4())
    async with StateDB(path) as db:
        if kind == "session":
            progression_id = str(uuid.uuid4())
            await db.create_progression(progression_id)
            await db.create_session(
                {
                    "id": entity_id,
                    "progression_id": progression_id,
                    "status": "running",
                    "started_at": 1.0,
                    "node_metadata": {"pid": 123, "pid_create_time": 42.0, "pid_host": "local"},
                }
            )
            get = db.get_session
        else:
            await db.create_invocation(
                {
                    "id": entity_id,
                    "skill": "test",
                    "status": "running",
                    "started_at": 1.0,
                    "node_metadata": {"pid": 123, "pid_create_time": 42.0, "pid_host": "local"},
                }
            )
            get = db.get_invocation
        for observed_at in (1, 2, 3):
            row = await get(entity_id)
            result = await db.observe_unverifiable_pid(
                kind, entity_id, snapshot=row, pid=123, create_time=42.0, observed_at=observed_at
            )
            assert result["applied"]
        before = await get(entity_id)
        history_before = await db.fetch_all(
            "SELECT * FROM status_transitions WHERE entity_id = ?", (entity_id,)
        )

    attempts = 0

    def hostname():
        nonlocal attempts
        attempts += 1
        if attempts <= successful_host_checks:
            return "local"
        raise OSError("private hostname probe detail")

    errors = []
    signal = Mock(side_effect=AssertionError("must not signal"))
    monkeypatch.setattr(kill_cli, "log_error", errors.append)
    monkeypatch.setattr("lionhub.orchestration.kill.os.kill", signal)
    with monkeypatch.context() as scoped:
        scoped.setattr(socket, "gethostname", hostname)
        assert (
            await kill_cli._resolve_quarantine(entity_id, target="failed", reason="reviewed") == 1
        )

    assert attempts == successful_host_checks + 1
    assert errors == ["quarantine identity probe failed; no update made"]
    assert "private hostname probe detail" not in str(errors)
    signal.assert_not_called()
    async with StateDB(path) as db:
        after = await (
            db.get_session(entity_id) if kind == "session" else db.get_invocation(entity_id)
        )
        history_after = await db.fetch_all(
            "SELECT * FROM status_transitions WHERE entity_id = ?", (entity_id,)
        )
    assert after == before
    assert history_after == history_before
