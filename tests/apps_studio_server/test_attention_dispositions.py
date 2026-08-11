# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Studio needs-attention discharge lifecycle (dispositions)."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from lionagi.state.db import StateDB  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _patch_db(monkeypatch: pytest.MonkeyPatch, db_path: Path) -> None:
    import lionagi.state.db as state_db_mod

    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)


def _make_client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))
    from lionagi.studio.app import app

    return TestClient(
        app,
        base_url="http://127.0.0.1:8765",
        headers={"Content-Type": "application/json"},
    )


async def _init_db(db_path: Path) -> None:
    async with StateDB(db_path):
        pass  # opens + applies schema (creates the attention tables too)


# ---------------------------------------------------------------------------
# Unit-level: service functions directly (no HTTP)
# ---------------------------------------------------------------------------


def test_upsert_creates_pending_row(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi.studio.services import attention as attention_mod

    row = _run(
        attention_mod.upsert_disposition(
            "run:abc123",
            state="acknowledged",
            source_status="failed",
            actor="operator",
        )
    )
    assert row["item_id"] == "run:abc123"
    assert row["state"] == "acknowledged"
    assert row["source_status"] == "failed"
    assert row["actor"] == "operator"
    assert row["note"] is None
    assert row["expires_at"] is None
    assert row["created_at"] == row["updated_at"]


def test_expected_requires_note_and_expiry(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from fastapi import HTTPException

    from lionagi.studio.services import attention as attention_mod

    with pytest.raises(HTTPException):
        _run(attention_mod.upsert_disposition("run:x", state="expected", source_status="failed"))
    with pytest.raises(HTTPException):
        _run(
            attention_mod.upsert_disposition(
                "run:x", state="expected", source_status="failed", note="  "
            )
        )
    with pytest.raises(HTTPException):
        _run(
            attention_mod.upsert_disposition(
                "run:x",
                state="expected",
                source_status="failed",
                note="deploy window",
            )
        )
    row = _run(
        attention_mod.upsert_disposition(
            "run:x",
            state="expected",
            source_status="failed",
            note="deploy window",
            expires_at=time.time() + 3600,
        )
    )
    assert row["state"] == "expected"
    assert row["note"] == "deploy window"


def test_snoozed_requires_expiry(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from fastapi import HTTPException

    from lionagi.studio.services import attention as attention_mod

    with pytest.raises(HTTPException):
        _run(attention_mod.upsert_disposition("sched:s1", state="snoozed", source_status="failed"))
    row = _run(
        attention_mod.upsert_disposition(
            "sched:s1",
            state="snoozed",
            source_status="failed",
            expires_at=time.time() + 60,
        )
    )
    assert row["state"] == "snoozed"


def test_put_is_idempotent_under_retry(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi.studio.services import attention as attention_mod

    first = _run(
        attention_mod.upsert_disposition(
            "run:retry1", state="resolved", source_status="failed", actor="operator"
        )
    )
    second = _run(
        attention_mod.upsert_disposition(
            "run:retry1", state="resolved", source_status="failed", actor="operator"
        )
    )
    assert first["item_id"] == second["item_id"]
    assert second["state"] == "resolved"
    assert second["created_at"] == first["created_at"], (
        "create-or-replace keeps original created_at"
    )

    listed = _run(attention_mod.list_dispositions())
    assert len(listed) == 1
    assert listed["run:retry1"]["state"] == "resolved"


def test_replace_changes_state_and_appends_history(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi.studio.services import attention as attention_mod

    _run(
        attention_mod.upsert_disposition(
            "run:r2", state="acknowledged", source_status="failed", actor="operator"
        )
    )
    replaced = _run(
        attention_mod.upsert_disposition(
            "run:r2", state="resolved", source_status="completed", actor="operator"
        )
    )
    assert replaced["state"] == "resolved"
    assert replaced["source_status"] == "completed"

    history = _run(attention_mod.disposition_history("run:r2"))
    assert [h["new_state"] for h in history] == ["acknowledged", "resolved"]
    assert history[0]["prior_state"] is None
    assert history[1]["prior_state"] == "acknowledged"


def test_history_order_survives_equal_timestamps(tmp_path, monkeypatch):
    """Two writes landing at the exact same wall-clock time (a real
    possibility -- time.time() resolution is coarser than SQLite's
    lock-serialized write throughput) must still read back in append order.
    Ordering by created_at alone can tie and let a reader observe them
    reversed; the server-side sequence column can't tie."""
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi.studio.services import attention as attention_mod

    frozen = time.time()
    monkeypatch.setattr(attention_mod.time, "time", lambda: frozen)

    _run(
        attention_mod.upsert_disposition(
            "run:tie", state="acknowledged", source_status="failed", actor="operator"
        )
    )
    _run(
        attention_mod.upsert_disposition(
            "run:tie", state="resolved", source_status="completed", actor="operator"
        )
    )
    _run(attention_mod.delete_disposition("run:tie", actor="operator"))

    history = _run(attention_mod.disposition_history("run:tie"))
    assert len({h["created_at"] for h in history}) == 1, "fixture must actually tie timestamps"
    assert [h["new_state"] for h in history] == ["acknowledged", "resolved", "open"]


def test_delete_undoes_and_is_a_noop_when_nothing_discharged(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi.studio.services import attention as attention_mod

    _run(
        attention_mod.upsert_disposition(
            "run:r3", state="resolved", source_status="failed", actor="operator"
        )
    )
    result = _run(attention_mod.delete_disposition("run:r3", actor="operator"))
    assert result == {"item_id": "run:r3", "deleted": True}

    listed = _run(attention_mod.list_dispositions())
    assert "run:r3" not in listed

    # Second delete is a no-op — no duplicate 'open' history row.
    result2 = _run(attention_mod.delete_disposition("run:r3", actor="operator"))
    assert result2 == {"item_id": "run:r3", "deleted": False}

    history = _run(attention_mod.disposition_history("run:r3"))
    assert [h["new_state"] for h in history] == ["resolved", "open"]
    assert history[-1]["prior_state"] == "resolved"


def test_delete_fences_a_replayed_put_from_resurrecting_the_disposition(tmp_path, monkeypatch):
    """A DELETE must leave behind something a later PUT has to beat: without
    it, a network-delayed retry of the pre-delete PUT can arrive after the
    undo and recreate the row with its old (already-undone) expires_at."""
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from fastapi import HTTPException

    from lionagi.studio.services import attention as attention_mod

    original = _run(
        attention_mod.upsert_disposition(
            "run:replay",
            state="snoozed",
            source_status="failed",
            expires_at=time.time() + 3600,
            actor="operator",
        )
    )
    stale_revision = original["revision"]

    _run(attention_mod.delete_disposition("run:replay", actor="operator"))
    assert "run:replay" not in _run(attention_mod.list_dispositions())

    # The delayed duplicate of the original PUT, carrying the pre-delete
    # revision, must not resurrect the snooze.
    with pytest.raises(HTTPException) as exc_info:
        _run(
            attention_mod.upsert_disposition(
                "run:replay",
                state="snoozed",
                source_status="failed",
                expires_at=time.time() + 3600,
                actor="operator",
                revision=stale_revision,
            )
        )
    assert exc_info.value.status_code == 409
    assert "run:replay" not in _run(attention_mod.list_dispositions())

    # So must a caller that carries no revision at all -- once item_id has a
    # recorded prior operation, omitting revision is no safer than a stale one.
    with pytest.raises(HTTPException) as exc_info2:
        _run(
            attention_mod.upsert_disposition(
                "run:replay",
                state="acknowledged",
                source_status="failed",
                actor="operator",
            )
        )
    assert exc_info2.value.status_code == 409

    # A caller that reads the current (post-delete) revision and beats it
    # is a legitimate fresh action, and succeeds.
    recreated = _run(
        attention_mod.upsert_disposition(
            "run:replay",
            state="acknowledged",
            source_status="failed",
            actor="operator",
            revision=stale_revision + 1,
        )
    )
    assert recreated["state"] == "acknowledged"


def test_active_row_put_with_stale_revision_is_last_writer_wins_not_fenced(tmp_path, monkeypatch):
    """Contract, not a bug: a stale-revision PUT against a row that is still
    ACTIVE applies unconditionally and overwrites newer fields -- the
    revision fence only guards recreating a row a DELETE already removed.
    Fencing active-row updates too would reject an operator's own retried
    PUT, which is itself a "stale revision" from the server's point of
    view, breaking the idempotent-retry contract upsert_disposition
    promises."""
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi.studio.services import attention as attention_mod

    first = _run(
        attention_mod.upsert_disposition(
            "run:lww", state="acknowledged", source_status="failed", actor="operator"
        )
    )
    assert first["revision"] == 1

    second = _run(
        attention_mod.upsert_disposition(
            "run:lww",
            state="snoozed",
            source_status="failed",
            actor="operator",
            expires_at=time.time() + 3600,
            revision=first["revision"],
        )
    )
    assert second["state"] == "snoozed"
    assert second["revision"] == 2

    # A PUT carrying the now-stale revision 1 (e.g. a delayed retry of the
    # first PUT) is not rejected: it overwrites the revision-2 fields.
    third = _run(
        attention_mod.upsert_disposition(
            "run:lww",
            state="acknowledged",
            source_status="failed",
            actor="operator",
            revision=first["revision"],
        )
    )
    assert third["state"] == "acknowledged"
    assert third["revision"] == 3

    listed = _run(attention_mod.list_dispositions())
    assert listed["run:lww"]["state"] == "acknowledged"


def test_http_put_after_delete_rejects_stale_revision(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(db_path, monkeypatch)

    put1 = client.put(
        "/api/attention/dispositions/run:httpreplay",
        json={
            "state": "snoozed",
            "source_status": "failed",
            "expires_at": time.time() + 3600,
        },
    )
    assert put1.status_code == 200, put1.text
    stale_revision = put1.json()["revision"]

    deleted = client.delete("/api/attention/dispositions/run:httpreplay")
    assert deleted.status_code == 200

    replay = client.put(
        "/api/attention/dispositions/run:httpreplay",
        json={
            "state": "snoozed",
            "source_status": "failed",
            "expires_at": time.time() + 3600,
            "revision": stale_revision,
        },
    )
    assert replay.status_code == 409

    listed = client.get("/api/attention/dispositions/")
    assert "run:httpreplay" not in listed.json()["dispositions"]


def test_list_excludes_lapsed_snoozed_and_expected_but_keeps_others(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi.studio.services import attention as attention_mod

    now = time.time()
    _run(
        attention_mod.upsert_disposition(
            "run:lapsed-snooze",
            state="snoozed",
            source_status="failed",
            expires_at=now - 10,
        )
    )
    _run(
        attention_mod.upsert_disposition(
            "run:active-snooze",
            state="snoozed",
            source_status="failed",
            expires_at=now + 3600,
        )
    )
    _run(
        attention_mod.upsert_disposition(
            "run:lapsed-expected",
            state="expected",
            source_status="failed",
            note="deploy",
            expires_at=now - 10,
        )
    )
    _run(attention_mod.upsert_disposition("run:ack", state="acknowledged", source_status="failed"))
    _run(
        attention_mod.upsert_disposition(
            "run:resolved", state="resolved", source_status="completed"
        )
    )

    listed = _run(attention_mod.list_dispositions())
    assert set(listed) == {"run:active-snooze", "run:ack", "run:resolved"}


def test_new_occurrence_after_resolution_is_a_fresh_item_id(tmp_path, monkeypatch):
    """A resolved run:<id> item can never mask a different, later run — the
    two carry different item ids by construction, so resolving one leaves
    the other entirely untouched in the dispositions store."""
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi.studio.services import attention as attention_mod

    _run(
        attention_mod.upsert_disposition(
            "run:old-failure", state="resolved", source_status="failed"
        )
    )
    listed = _run(attention_mod.list_dispositions())
    assert "run:new-failure" not in listed
    assert listed["run:old-failure"]["state"] == "resolved"


def test_concurrent_writes_to_one_item_yield_one_disposition_and_ordered_history(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi.studio.services import attention as attention_mod

    async def _go():
        await asyncio.gather(
            attention_mod.upsert_disposition(
                "run:race", state="acknowledged", source_status="failed", actor="a"
            ),
            attention_mod.upsert_disposition(
                "run:race", state="resolved", source_status="failed", actor="b"
            ),
        )

    _run(_go())

    listed = _run(attention_mod.list_dispositions())
    assert len(listed) == 1
    assert listed["run:race"]["state"] in ("acknowledged", "resolved")

    history = _run(attention_mod.disposition_history("run:race"))
    assert len(history) == 2
    # Whichever write landed second must have seen the first as prior_state —
    # a lost write would instead show two independent prior_state=None rows.
    assert history[1]["prior_state"] == history[0]["new_state"]
    assert history[-1]["new_state"] == listed["run:race"]["state"]


def test_reopening_store_does_not_fail_and_keeps_rows(tmp_path, monkeypatch):
    """Migration idempotence: applying the schema on a store that already
    has the attention tables (a daemon restart) must not raise, and must
    leave existing dispositions untouched."""
    db_path = tmp_path / "state.db"
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from lionagi.studio.services import attention as attention_mod

    _run(
        attention_mod.upsert_disposition("run:persisted", state="resolved", source_status="failed")
    )

    # Simulate a daemon restart: open the same store again (create_all is
    # idempotent — CREATE TABLE IF NOT EXISTS — same as every other table).
    _run(_init_db(db_path))
    _run(_init_db(db_path))

    listed = _run(attention_mod.list_dispositions())
    assert listed["run:persisted"]["state"] == "resolved"


def _create_pre_revision_attention_db(db_path: Path) -> None:
    """Build the exact shape ``feat(studio): durable discharge lifecycle``
    (604dfd6fc) produced: ``attention_dispositions`` and
    ``attention_disposition_history`` already exist, but without
    ``revision``/``sequence`` -- those columns and the
    ``attention_disposition_revisions`` ledger were only added later, and
    that later commit never bumped ``schema_meta.version`` off '2' either.
    metadata.create_all() only creates *missing* tables, so opening a store
    already in this shape with current code creates the (new)
    attention_disposition_revisions table but silently leaves the two
    pre-existing tables without their new columns."""
    import sqlite3

    from lionagi.state.db import _SCHEMA_PATH

    with sqlite3.connect(db_path) as conn:
        conn.executescript(_SCHEMA_PATH.read_text())
        conn.execute("DROP TABLE attention_disposition_revisions")
        conn.execute("DROP INDEX idx_attention_disposition_history_sequence")
        conn.execute("ALTER TABLE attention_disposition_history DROP COLUMN sequence")
        conn.execute("ALTER TABLE attention_dispositions DROP COLUMN revision")
        conn.execute("UPDATE schema_meta SET value = '2' WHERE key = 'version'")

        # run:active -- three PUTs before the upgrade. Its true revision
        # count (3) must survive the backfill, not collapse to a flat 1.
        conn.execute(
            "INSERT INTO attention_dispositions "
            "(item_id, state, note, created_at, updated_at, expires_at, actor, source_status) "
            "VALUES ('run:active', 'resolved', NULL, 1.0, 3.0, NULL, 'operator', 'failed')"
        )
        for i, (prior, new, ts) in enumerate(
            [
                (None, "acknowledged", 1.0),
                ("acknowledged", "snoozed", 2.0),
                ("snoozed", "resolved", 3.0),
            ]
        ):
            conn.execute(
                "INSERT INTO attention_disposition_history "
                "(id, item_id, prior_state, new_state, note, actor, source_status, created_at) "
                "VALUES (?, 'run:active', ?, ?, NULL, 'operator', 'failed', ?)",
                (f"hist-active-{i}", prior, new, ts),
            )

        # run:deleted -- acknowledged then undone (DELETE) before the
        # upgrade. No active row, but a PUT that predates the DELETE must
        # still be fenced against it after the upgrade.
        conn.execute(
            "INSERT INTO attention_disposition_history "
            "(id, item_id, prior_state, new_state, note, actor, source_status, created_at) "
            "VALUES ('hist-deleted-0', 'run:deleted', NULL, 'acknowledged', NULL, "
            "'operator', 'failed', 10.0)"
        )
        conn.execute(
            "INSERT INTO attention_disposition_history "
            "(id, item_id, prior_state, new_state, note, actor, source_status, created_at) "
            "VALUES ('hist-deleted-1', 'run:deleted', 'acknowledged', 'open', NULL, "
            "'operator', 'failed', 11.0)"
        )
        conn.commit()


def test_attention_dispositions_upgrade_from_pre_revision_store(tmp_path, monkeypatch):
    """Opening a store already in the pre-revision/sequence shape must
    migrate it, not just re-stamp it: ordered history reads without
    ``OperationalError``, revision backfills from the true history count,
    a DELETE-then-replayed-PUT is fenced (409), and the fence for an item
    already deleted *before* the upgrade survives the upgrade too."""
    db_path = tmp_path / "state.db"
    _create_pre_revision_attention_db(db_path)
    _patch_db(monkeypatch, db_path)
    _run(_init_db(db_path))

    from fastapi import HTTPException

    from lionagi.studio.services import attention as attention_mod

    # (c) ordered history reads without OperationalError; (e) old rows read
    # back in their original (created_at, id) order via the backfilled
    # sequence.
    history = _run(attention_mod.disposition_history("run:active"))
    assert [h["new_state"] for h in history] == ["acknowledged", "snoozed", "resolved"]

    # Revision backfilled from the true history count (3 PUTs), not a flat 1.
    listed = _run(attention_mod.list_dispositions())
    assert listed["run:active"]["revision"] == 3

    # (d) a PUT continues the ledger from the backfilled count.
    again = _run(
        attention_mod.upsert_disposition(
            "run:active",
            state="acknowledged",
            source_status="failed",
            actor="operator",
            revision=3,
        )
    )
    assert again["revision"] == 4

    # (d) DELETE then a replayed PUT is fenced (409).
    _run(attention_mod.delete_disposition("run:active", actor="operator"))
    with pytest.raises(HTTPException) as exc_info:
        _run(
            attention_mod.upsert_disposition(
                "run:active",
                state="acknowledged",
                source_status="failed",
                actor="operator",
                revision=4,
            )
        )
    assert exc_info.value.status_code == 409

    # The fence for an item deleted *before* the upgrade must also survive:
    # a replay of the pre-delete PUT (stale revision) is rejected...
    with pytest.raises(HTTPException) as exc_info2:
        _run(
            attention_mod.upsert_disposition(
                "run:deleted",
                state="acknowledged",
                source_status="failed",
                actor="operator",
                revision=1,
            )
        )
    assert exc_info2.value.status_code == 409

    # ...while a caller that beats the backfilled ledger succeeds.
    recreated = _run(
        attention_mod.upsert_disposition(
            "run:deleted",
            state="acknowledged",
            source_status="failed",
            actor="operator",
            revision=2,
        )
    )
    assert recreated["state"] == "acknowledged"


# ---------------------------------------------------------------------------
# HTTP-level: routes
# ---------------------------------------------------------------------------


def test_http_put_get_delete_roundtrip(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(db_path, monkeypatch)

    resp = client.put(
        "/api/attention/dispositions/run:http1",
        json={"state": "acknowledged", "source_status": "failed", "actor": "alice"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "acknowledged"
    assert body["actor"] == "alice"

    listed = client.get("/api/attention/dispositions/")
    assert listed.status_code == 200
    assert "run:http1" in listed.json()["dispositions"]

    history = client.get("/api/attention/dispositions/run:http1/history")
    assert history.status_code == 200
    assert [h["new_state"] for h in history.json()["history"]] == ["acknowledged"]

    deleted = client.delete("/api/attention/dispositions/run:http1")
    assert deleted.status_code == 200
    assert deleted.json() == {"item_id": "run:http1", "deleted": True}

    listed_after = client.get("/api/attention/dispositions/")
    assert "run:http1" not in listed_after.json()["dispositions"]


def test_http_put_rejects_expected_without_note(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(db_path, monkeypatch)

    resp = client.put(
        "/api/attention/dispositions/run:http2",
        json={"state": "expected", "source_status": "failed", "expires_at": time.time() + 60},
    )
    assert resp.status_code == 422


def test_http_put_rejects_unknown_state(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(db_path, monkeypatch)

    resp = client.put(
        "/api/attention/dispositions/run:http3",
        json={"state": "ignored", "source_status": "failed"},
    )
    assert resp.status_code == 422


def test_http_delete_of_unknown_item_is_a_noop(tmp_path, monkeypatch):
    db_path = tmp_path / "state.db"
    client = _make_client(db_path, monkeypatch)

    resp = client.delete("/api/attention/dispositions/run:never-existed")
    assert resp.status_code == 200
    assert resp.json() == {"item_id": "run:never-existed", "deleted": False}
