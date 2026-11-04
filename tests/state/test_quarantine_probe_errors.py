"""An identity probe failure is distinct from a verified process."""

from __future__ import annotations

import uuid
from unittest.mock import Mock

import pytest

from lionagi.cli import kill as kill_cli
from lionagi.state.db import StateDB


async def _seed_quarantine(kind):
    entity_id = str(uuid.uuid4())
    async with StateDB() as db:
        if kind == "session":
            progression_id = str(uuid.uuid4())
            await db.create_progression(progression_id)
            await db.create_session(
                {
                    "id": entity_id,
                    "progression_id": progression_id,
                    "status": "running",
                    "started_at": 1.0,
                    "node_metadata": {"pid": 54321, "pid_create_time": 100.0},
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
                    "node_metadata": {"pid": 54321, "pid_create_time": 100.0},
                }
            )
            get = db.get_invocation
        for number in range(3):
            row = await get(entity_id)
            result = await db.observe_unverifiable_pid(
                kind, entity_id, snapshot=row, pid=54321, create_time=100.0, observed_at=number + 1
            )
            assert result["applied"]
        return entity_id


async def _snapshot(kind, entity_id):
    async with StateDB() as db:
        row = await (
            db.get_session(entity_id) if kind == "session" else db.get_invocation(entity_id)
        )
        history = await db.fetch_all(
            "SELECT * FROM status_transitions WHERE entity_id = ?", (entity_id,)
        )
        return row, history


@pytest.mark.parametrize("kind", ["session", "invocation"])
@pytest.mark.parametrize("probe_fails", [True, False])
async def test_resolution_reports_probe_failure_separately_without_mutation(
    kind, probe_fails, tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    entity_id = await _seed_quarantine(kind)
    before = await _snapshot(kind, entity_id)
    monkeypatch.setattr(kill_cli, "_pid_alive", lambda _pid: True)

    def probe(*_args, **_kwargs):
        if probe_fails:
            raise RuntimeError("private probe detail")
        return "ours"

    monkeypatch.setattr(kill_cli, "check_pid_identity", probe)
    signal = Mock(side_effect=AssertionError("signal must not be sent"))
    monkeypatch.setattr("lionagi.cli._kill_service.os.kill", signal)
    assert await kill_cli._resolve_quarantine(entity_id, target="failed", reason="reviewed") == 1
    assert await _snapshot(kind, entity_id) == before
    signal.assert_not_called()
    if probe_fails:
        assert "probe failed" in caplog.text
        assert "identity can now be verified" not in caplog.text
        assert "private probe detail" not in caplog.text
    else:
        assert "identity can now be verified" in caplog.text
        assert "probe failed" not in caplog.text


@pytest.mark.parametrize("kind", ["session", "invocation"])
async def test_lifecycle_probe_exception_is_not_misreported_as_conflict(
    kind, tmp_path, monkeypatch
):
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    entity_id = await _seed_quarantine(kind)
    before = await _snapshot(kind, entity_id)

    def probe(_row):
        raise RuntimeError("private probe detail")

    async with StateDB() as db:
        row = await (
            db.get_session(entity_id) if kind == "session" else db.get_invocation(entity_id)
        )
        with pytest.raises(RuntimeError, match="probe failed") as caught:
            await db._lifecycle_service().guarded_quarantine_disposition(
                entity_type=kind,
                entity_id=entity_id,
                snapshot=row,
                target_status="failed",
                operator_reason="reviewed",
                identity_unverifiable=probe,
            )
        assert "private probe detail" not in str(caught.value)
    assert await _snapshot(kind, entity_id) == before
