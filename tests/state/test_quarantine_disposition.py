"""The no-signal operator disposition is a guarded lifecycle transition."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from lionagi.state.db import StateDB


async def _make_quarantined(db, kind):
    if kind == "session":
        await db.create_progression("p")
        await db.create_session(
            {
                "id": "s",
                "progression_id": "p",
                "status": "running",
                "started_at": 1.0,
                "node_metadata": {"pid": 123, "pid_create_time": 12.0},
            }
        )
        entity_id = "s"
        get = db.get_session
    else:
        await db.create_invocation(
            {
                "id": "i",
                "skill": "test",
                "started_at": 1.0,
                "ended_at": 2.0,
                "node_metadata": {"pid": 123, "pid_create_time": 12.0},
            }
        )
        entity_id = "i"
        get = db.get_invocation
    for number in range(3):
        row = await get(entity_id)
        result = await db.observe_unverifiable_pid(
            kind, entity_id, snapshot=row, pid=123, create_time=12.0, observed_at=10 + number
        )
        assert result["applied"]
    return entity_id, await get(entity_id), get


@pytest.mark.parametrize("kind", ["session", "invocation"])
@pytest.mark.parametrize("target", ["failed", "aborted", "cancelled"])
async def test_guarded_quarantine_disposition_records_reason_and_audit(tmp_path, kind, target):
    async with StateDB(tmp_path / "state.db") as db:
        entity_id, snapshot, get = await _make_quarantined(db, kind)
        outcome = await db._lifecycle_service().guarded_quarantine_disposition(
            entity_type=kind,
            entity_id=entity_id,
            snapshot=snapshot,
            target_status=target,
            operator_reason="operator investigated",
            identity_unverifiable=lambda _: True,
        )
        assert outcome.result == "applied"
        row = await get(entity_id)
        assert row["status"] == target
        assert row["status_reason_code"] == f"run.{target}.identity_unverified"
        assert (
            row["status_reason_summary"]
            == "identity unverified, operator disposition: operator investigated"
        )
        assert row["ended_at"] >= row["started_at"]
        if kind == "invocation":
            assert row["ended_at"] > 2.0
        if kind == "session":
            assert row["duration_ms"] >= 0
        history = await db.fetch_all(
            "SELECT * FROM status_transitions WHERE entity_type = ? AND entity_id = ? AND status = ?",
            (kind, entity_id, target),
        )
        assert len(history) == 1
        assert history[0]["source"] == "admin"
        assert "operator investigated" in str(history[0]["evidence_refs"])


@pytest.mark.parametrize("kind", ["session", "invocation"])
async def test_stale_identity_and_missing_quarantine_do_not_transition(tmp_path, kind):
    async with StateDB(tmp_path / "state.db") as db:
        entity_id, snapshot, get = await _make_quarantined(db, kind)
        merge = (
            db.merge_session_node_metadata
            if kind == "session"
            else db.merge_invocation_node_metadata
        )
        await merge(entity_id, {"pid": 124})
        outcome = await db._lifecycle_service().guarded_quarantine_disposition(
            entity_type=kind,
            entity_id=entity_id,
            snapshot=snapshot,
            target_status="cancelled",
            operator_reason="reviewed",
            identity_unverifiable=lambda _: True,
        )
        assert outcome.result == "conflict"
        assert (await get(entity_id))["status"] == "running"
        current = await get(entity_id)
        # Even a fresh snapshot cannot use the previous PID's quarantine.
        outcome = await db._lifecycle_service().guarded_quarantine_disposition(
            entity_type=kind,
            entity_id=entity_id,
            snapshot=current,
            target_status="cancelled",
            operator_reason="reviewed",
            identity_unverifiable=lambda _: True,
        )
        assert outcome.result == "conflict"
        assert (await get(entity_id))["status"] == "running"


async def test_generation_and_epoch_changes_refuse_old_resolution(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        entity_id, snapshot, get = await _make_quarantined(db, "session")
        await db.clear_unverifiable_pid("session", entity_id, snapshot=snapshot)
        outcome = await db._lifecycle_service().guarded_quarantine_disposition(
            entity_type="session",
            entity_id=entity_id,
            snapshot=snapshot,
            target_status="cancelled",
            operator_reason="reviewed",
            identity_unverifiable=lambda _: True,
        )
        assert outcome.result == "conflict"
        # Re-quarantine and change the generation while retaining old metadata.
        for number in range(3):
            row = await get(entity_id)
            await db.observe_unverifiable_pid(
                "session",
                entity_id,
                snapshot=row,
                pid=123,
                create_time=12.0,
                observed_at=20 + number,
            )
        snapshot = await get(entity_id)
        await db.update_session(entity_id, cli_worker_generation=str(uuid.uuid4()))
        outcome = await db._lifecycle_service().guarded_quarantine_disposition(
            entity_type="session",
            entity_id=entity_id,
            snapshot=snapshot,
            target_status="cancelled",
            operator_reason="reviewed",
            identity_unverifiable=lambda _: True,
        )
        assert outcome.result == "conflict"
        assert (await get(entity_id))["status"] == "running"


async def test_inconsistent_quarantine_marker_cannot_authorize_disposition(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        entity_id, snapshot, get = await _make_quarantined(db, "session")
        await db.merge_session_node_metadata(entity_id, {"unverifiable_count": 1})
        snapshot = await get(entity_id)
        outcome = await db._lifecycle_service().guarded_quarantine_disposition(
            entity_type="session",
            entity_id=entity_id,
            snapshot=snapshot,
            target_status="cancelled",
            operator_reason="reviewed",
            identity_unverifiable=lambda _: True,
        )
        assert outcome.result == "conflict"
        assert (await get(entity_id))["status"] == "running"


async def test_racing_resolutions_write_one_audit_record(tmp_path):
    path = tmp_path / "state.db"
    async with StateDB(path) as first, StateDB(path) as second:
        entity_id, snapshot, get = await _make_quarantined(first, "session")

        async def resolve(db):
            return await db._lifecycle_service().guarded_quarantine_disposition(
                entity_type="session",
                entity_id=entity_id,
                snapshot=snapshot,
                target_status="cancelled",
                operator_reason="reviewed",
                identity_unverifiable=lambda _: True,
            )

        results = await asyncio.gather(resolve(first), resolve(second))
        assert sorted(r.result for r in results) == ["applied", "conflict"]
        history = await first.fetch_all(
            "SELECT * FROM status_transitions WHERE entity_type = ? AND entity_id = ? AND status = ?",
            ("session", entity_id, "cancelled"),
        )
        assert len(history) == 1


@pytest.mark.parametrize("target,reason", [("completed", "reviewed"), ("cancelled", "   ")])
async def test_bad_operator_request_is_refused(tmp_path, target, reason):
    async with StateDB(tmp_path / "state.db") as db:
        entity_id, snapshot, get = await _make_quarantined(db, "session")
        with pytest.raises(ValueError):
            await db._lifecycle_service().guarded_quarantine_disposition(
                entity_type="session",
                entity_id=entity_id,
                snapshot=snapshot,
                target_status=target,
                operator_reason=reason,
                identity_unverifiable=lambda _: True,
            )
        assert (await get(entity_id))["status"] == "running"


@pytest.mark.parametrize("kind", ["session", "invocation"])
@pytest.mark.parametrize("verdict", ["ours", "not_ours", "dead", "zombie"])
async def test_disposition_refuses_when_identity_is_no_longer_unverifiable(
    tmp_path, monkeypatch, kind, verdict
):
    async with StateDB(tmp_path / "state.db") as db:
        entity_id, snapshot, get = await _make_quarantined(db, kind)
        before = await get(entity_id)
        history_before = await db.fetch_all(
            "SELECT * FROM status_transitions WHERE entity_type = ? AND entity_id = ?",
            (kind, entity_id),
        )
        probes = []
        monkeypatch.setattr("os.kill", lambda *args: pytest.fail("resolution sent a signal"))

        def probe(row):
            probes.append(row)
            return verdict == "unverifiable"

        outcome = await db._lifecycle_service().guarded_quarantine_disposition(
            entity_type=kind,
            entity_id=entity_id,
            snapshot=snapshot,
            target_status="cancelled",
            operator_reason="reviewed",
            identity_unverifiable=probe,
        )
        assert outcome.result == "conflict"
        assert len(probes) == 1
        assert probes[0]["id"] == entity_id
        assert probes[0]["node_metadata"] == before["node_metadata"]
        assert await get(entity_id) == before
        assert (
            await db.fetch_all(
                "SELECT * FROM status_transitions WHERE entity_type = ? AND entity_id = ?",
                (kind, entity_id),
            )
            == history_before
        )


@pytest.mark.parametrize("probe_result", [False, None, 1, "unverifiable"])
async def test_disposition_requires_exact_true_from_probe(tmp_path, probe_result):
    async with StateDB(tmp_path / "state.db") as db:
        entity_id, snapshot, get = await _make_quarantined(db, "session")
        before = await get(entity_id)
        outcome = await db._lifecycle_service().guarded_quarantine_disposition(
            entity_type="session",
            entity_id=entity_id,
            snapshot=snapshot,
            target_status="cancelled",
            operator_reason="reviewed",
            identity_unverifiable=lambda _: probe_result,
        )
        assert outcome.result == "conflict"
        assert await get(entity_id) == before


async def test_disposition_probe_exception_fails_closed(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        entity_id, snapshot, get = await _make_quarantined(db, "session")
        before = await get(entity_id)

        def probe(_):
            raise OSError("private probe detail")

        with pytest.raises(RuntimeError, match="probe failed") as caught:
            await db._lifecycle_service().guarded_quarantine_disposition(
                entity_type="session",
                entity_id=entity_id,
                snapshot=snapshot,
                target_status="cancelled",
                operator_reason="reviewed",
                identity_unverifiable=probe,
            )
        assert "private probe detail" not in str(caught.value)
        assert await get(entity_id) == before


async def test_disposition_does_not_probe_a_changed_identity(tmp_path):
    async with StateDB(tmp_path / "state.db") as db:
        entity_id, snapshot, get = await _make_quarantined(db, "session")
        await db.merge_session_node_metadata(entity_id, {"pid": 124})
        before = await get(entity_id)

        def probe(_):
            pytest.fail("stale identity should be refused before probing")

        outcome = await db._lifecycle_service().guarded_quarantine_disposition(
            entity_type="session",
            entity_id=entity_id,
            snapshot=snapshot,
            target_status="cancelled",
            operator_reason="reviewed",
            identity_unverifiable=probe,
        )
        assert outcome.result == "conflict"
        assert await get(entity_id) == before


async def test_probe_holds_sqlite_writer_reservation(tmp_path):
    import sqlite3

    path = tmp_path / "state.db"
    async with StateDB(path) as db:
        entity_id, snapshot, get = await _make_quarantined(db, "session")

        def probe(_):
            with sqlite3.connect(path, timeout=0.2) as other:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    other.execute(
                        "UPDATE sessions SET node_metadata = json_set(node_metadata, '$.pid', 124) "
                        "WHERE id = ?",
                        (entity_id,),
                    )
            return True

        outcome = await db._lifecycle_service().guarded_quarantine_disposition(
            entity_type="session",
            entity_id=entity_id,
            snapshot=snapshot,
            target_status="cancelled",
            operator_reason="reviewed",
            identity_unverifiable=probe,
        )
        assert outcome.result == "applied"
        row = await get(entity_id)
        assert row["node_metadata"]["pid"] == 123
        assert row["status"] == "cancelled"


@pytest.mark.parametrize("kind", ["session", "invocation"])
async def test_disposition_snapshot_stays_isolated_while_transition_waits(
    tmp_path, kind, monkeypatch
):
    async with StateDB(tmp_path / "state.db") as db:
        entity_id, snapshot, get = await _make_quarantined(db, kind)
        service = db._lifecycle_service()
        original_transition = service._transition
        entered, release = asyncio.Event(), asyncio.Event()

        async def paused_transition(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_transition(*args, **kwargs)

        monkeypatch.setattr(service, "_transition", paused_transition)
        task = asyncio.create_task(
            service.guarded_quarantine_disposition(
                entity_type=kind,
                entity_id=entity_id,
                snapshot=snapshot,
                target_status="failed",
                operator_reason="reviewed",
                identity_unverifiable=lambda _: True,
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            snapshot["node_metadata"]["pid"] = 999
        finally:
            release.set()
        result = await asyncio.wait_for(task, 5)
        assert result.result == "applied"
        assert (await get(entity_id))["status"] == "failed"
