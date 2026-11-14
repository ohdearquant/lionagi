"""Probe-failure exceptions must not retain the original private error."""

from __future__ import annotations

import socket
import uuid

import pytest

from lionagi.state._quarantine_identity import QuarantineProbeError, is_current_quarantine
from lionagi.state.db import StateDB


@pytest.mark.parametrize("kind", ["session", "invocation"])
@pytest.mark.parametrize("failure", ["callback", "hostname"])
async def test_probe_failure_has_no_private_exception_graph(kind, failure, tmp_path, monkeypatch):
    async with StateDB(tmp_path / "state.db") as db:
        entity_id = str(uuid.uuid4())
        metadata = {"pid": 123, "pid_create_time": 42.0, "pid_host": "local"}
        if kind == "session":
            progression_id = str(uuid.uuid4())
            await db.create_progression(progression_id)
            await db.create_session(
                {
                    "id": entity_id,
                    "progression_id": progression_id,
                    "status": "running",
                    "started_at": 1.0,
                    "node_metadata": metadata,
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
                    "node_metadata": metadata,
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

        def hostname():
            if failure == "hostname":
                raise OSError("private hostname exception")
            return "local"

        def probe(_row):
            raise RuntimeError("private callback exception")

        with monkeypatch.context() as scoped:
            scoped.setattr(socket, "gethostname", hostname)
            with pytest.raises(QuarantineProbeError, match="probe failed") as captured:
                await db._lifecycle_service().guarded_quarantine_disposition(
                    entity_type=kind,
                    entity_id=entity_id,
                    snapshot=before,
                    target_status="failed",
                    operator_reason="reviewed",
                    identity_unverifiable=probe,
                )

        error = captured.value
        assert error.__cause__ is None
        assert error.__context__ is None
        tb = error.__traceback__
        while tb is not None:
            for value in tb.tb_frame.f_locals.values():
                if isinstance(value, BaseException) and value is not error:
                    assert "private" not in str(value)
            tb = tb.tb_next

        assert await get(entity_id) == before
        assert (
            await db.fetch_all("SELECT * FROM status_transitions WHERE entity_id = ?", (entity_id,))
            == history_before
        )


def test_direct_hostname_failure_uses_public_error_without_exception_context(monkeypatch):
    row = {
        "status": "running",
        "node_metadata": {
            "pid": 123,
            "pid_host": "local",
            "unverifiable_identity": "[123,null,null]",
            "unverifiable_count": 3,
            "unverifiable_epoch": 0,
            "unverifiable_quarantined": True,
        },
    }

    def fail_hostname():
        raise OSError("private hostname exception")

    monkeypatch.setattr(socket, "gethostname", fail_hostname)
    with pytest.raises(QuarantineProbeError, match="probe failed") as captured:
        is_current_quarantine(row, raise_on_probe_error=True)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "private" not in str(captured.value)
