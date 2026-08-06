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

    return TestClient(app, base_url="http://127.0.0.1:8765")


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
