# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the session-signals SSE endpoint and service layer.

Coverage targets:
  - GET /api/sessions/{id}/signals  (404 on unknown session, ordering, auth)
  - lionagi.studio.services.signals.get_signals_after  (empty, replay, ordering)
  - lionagi.state.db.StateDB.insert_session_signal / get_session_signals_after
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")

from lionagi.state.db import StateDB  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers shared with test_sessions_detail.py conventions
# ---------------------------------------------------------------------------


async def _seed_session(db_path: Path, session_id: str = "sig-sess-1") -> None:
    prog_id = f"{session_id}-prog"
    async with StateDB(db_path) as db:
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": session_id,
                "created_at": 100.0,
                "updated_at": 100.0,
                "progression_id": prog_id,
                "name": "Signal Test Session",
                "status": "running",
                "invocation_kind": "flow",
                "source_kind": "live",
            }
        )


# ---------------------------------------------------------------------------
# StateDB: insert_session_signal + get_session_signals_after
# ---------------------------------------------------------------------------


async def test_insert_signal_returns_sequential_seq(tmp_path):
    db_path = tmp_path / "state.db"
    await _seed_session(db_path)

    async with StateDB(db_path) as db:
        s1 = await db.insert_session_signal(
            session_id="sig-sess-1",
            kind="NodeStarted",
            op_id="op-a",
            ts=1000.0,
            payload={"name": "step1", "elapsed": 0.0},
        )
        s2 = await db.insert_session_signal(
            session_id="sig-sess-1",
            kind="NodeCompleted",
            op_id="op-a",
            ts=1001.0,
            payload={"name": "step1", "elapsed": 1.0},
        )

    assert s1 == 1
    assert s2 == 2


async def test_get_signals_after_returns_in_seq_order(tmp_path):
    db_path = tmp_path / "state.db"
    await _seed_session(db_path)

    async with StateDB(db_path) as db:
        for i in range(3):
            await db.insert_session_signal(
                session_id="sig-sess-1",
                kind="NodeQueued",
                op_id=f"op-{i}",
                ts=float(1000 + i),
                payload={"name": f"op-{i}"},
            )

        rows = await db.get_session_signals_after("sig-sess-1", 0)

    assert len(rows) == 3
    assert [r["seq"] for r in rows] == [1, 2, 3]
    assert [r["op_id"] for r in rows] == ["op-0", "op-1", "op-2"]


async def test_get_signals_after_filters_seq(tmp_path):
    db_path = tmp_path / "state.db"
    await _seed_session(db_path)

    async with StateDB(db_path) as db:
        for i in range(5):
            await db.insert_session_signal(
                session_id="sig-sess-1",
                kind="NodeStarted",
                op_id=f"op-{i}",
                ts=float(1000 + i),
                payload={},
            )

        rows = await db.get_session_signals_after("sig-sess-1", 3)

    assert len(rows) == 2
    assert rows[0]["seq"] == 4
    assert rows[1]["seq"] == 5


async def test_get_signals_after_empty_session(tmp_path):
    db_path = tmp_path / "state.db"
    await _seed_session(db_path)

    async with StateDB(db_path) as db:
        rows = await db.get_session_signals_after("sig-sess-1", 0)

    assert rows == []


async def test_get_signals_payload_round_trips(tmp_path):
    db_path = tmp_path / "state.db"
    await _seed_session(db_path)

    payload = {"name": "my-op", "elapsed": 1.23, "reason": "timeout"}

    async with StateDB(db_path) as db:
        await db.insert_session_signal(
            session_id="sig-sess-1",
            kind="NodeFailed",
            op_id="op-x",
            ts=5000.0,
            payload=payload,
        )
        rows = await db.get_session_signals_after("sig-sess-1", 0)

    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "NodeFailed"
    assert row["op_id"] == "op-x"
    assert row["ts"] == 5000.0
    assert row["payload"] == payload


# ---------------------------------------------------------------------------
# Studio service layer: signals.get_signals_after
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_signals_db(tmp_path, monkeypatch):
    import lionagi.studio.services.signals as svc

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(svc, "_DB", str(db_path))
    monkeypatch.setattr(svc, "DEFAULT_DB_PATH", db_path)
    return svc, db_path


async def test_service_get_signals_after_empty_when_db_absent(patched_signals_db):
    svc, db_path = patched_signals_db
    # DB file has not been created yet — should return [] gracefully.
    result = await svc.get_signals_after("any-id", 0)
    assert result == []


async def test_service_get_signals_after_empty_session(patched_signals_db):
    svc, db_path = patched_signals_db
    await _seed_session(db_path, "svc-sess")
    result = await svc.get_signals_after("svc-sess", 0)
    assert result == []


async def test_service_get_signals_after_returns_rows(patched_signals_db):
    svc, db_path = patched_signals_db
    await _seed_session(db_path, "svc-sess2")

    async with StateDB(db_path) as db:
        await db.insert_session_signal(
            session_id="svc-sess2",
            kind="RunStart",
            op_id="",
            ts=999.0,
            payload={},
        )
        await db.insert_session_signal(
            session_id="svc-sess2",
            kind="RunEnd",
            op_id="",
            ts=1001.0,
            payload={"result": "ok"},
        )

    rows = await svc.get_signals_after("svc-sess2", 0)
    assert len(rows) == 2
    assert rows[0]["kind"] == "RunStart"
    assert rows[1]["kind"] == "RunEnd"
    assert rows[1]["payload"] == {"result": "ok"}


async def test_service_get_signals_after_seq_filter(patched_signals_db):
    svc, db_path = patched_signals_db
    await _seed_session(db_path, "svc-sess3")

    async with StateDB(db_path) as db:
        for i in range(4):
            await db.insert_session_signal(
                session_id="svc-sess3",
                kind="NodeQueued",
                op_id=f"op-{i}",
                ts=float(1000 + i),
                payload={},
            )

    rows = await svc.get_signals_after("svc-sess3", 2)
    assert len(rows) == 2
    assert rows[0]["seq"] == 3
    assert rows[1]["seq"] == 4


# ---------------------------------------------------------------------------
# HTTP endpoint: GET /api/sessions/{id}/signals
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_app(tmp_path, monkeypatch):
    """Return (app, db_path, AsyncClient) with DB patched to tmp_path.

    Skips automatically when the studio/httpx extras are not installed.
    """
    pytest.importorskip("fastapi", reason="studio extra not installed")
    pytest.importorskip("httpx", reason="httpx not installed")

    import lionagi.studio.services.sessions as sessions_svc
    import lionagi.studio.services.signals as signals_svc

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(sessions_svc, "_DB", str(db_path))
    monkeypatch.setattr(sessions_svc, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(signals_svc, "_DB", str(db_path))
    monkeypatch.setattr(signals_svc, "DEFAULT_DB_PATH", db_path)

    from httpx import AsyncClient

    from lionagi.studio.app import app

    return app, db_path, AsyncClient(app=app, base_url="http://test")


async def test_signals_endpoint_404_for_unknown_session(patched_app):
    app, db_path, client = patched_app
    # Seed DB (empty — no sessions)
    await _seed_session(db_path, "not-this")
    async with client as ac:
        resp = await ac.get("/api/sessions/no-such-session/signals")
    assert resp.status_code == 404


async def test_signals_endpoint_streams_existing_events(patched_app):
    app, db_path, client = patched_app
    await _seed_session(db_path, "stream-sess")

    # Write a terminal status so the SSE generator closes quickly.
    async with StateDB(db_path) as db:
        await db.insert_session_signal(
            session_id="stream-sess",
            kind="NodeStarted",
            op_id="op-1",
            ts=500.0,
            payload={"name": "first"},
        )
        await db.insert_session_signal(
            session_id="stream-sess",
            kind="NodeCompleted",
            op_id="op-1",
            ts=501.0,
            payload={"name": "first", "elapsed": 1.0},
        )
    # Mark session terminal + old enough for is_session_stream_done() to fire.
    async with StateDB(db_path) as db:
        await db.update_status(
            "session",
            "stream-sess",
            new_status="completed",
            reason_code="run.completed.ok",
        )
    async with aiosqlite.connect(str(db_path)) as raw:
        await raw.execute("UPDATE sessions SET updated_at = 1.0 WHERE id = 'stream-sess'")
        await raw.commit()

    collected: list[dict] = []
    async with client as ac:
        async with ac.stream("GET", "/api/sessions/stream-sess/signals") as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                payload = json.loads(line[6:])
                collected.append(payload)
                if payload.get("type") == "done":
                    break

    # Should have received the two signal rows and the done sentinel.
    signal_rows = [e for e in collected if "kind" in e]
    assert len(signal_rows) == 2
    assert signal_rows[0]["kind"] == "NodeStarted"
    assert signal_rows[1]["kind"] == "NodeCompleted"
    assert signal_rows[0]["op_id"] == "op-1"
    done_frames = [e for e in collected if e.get("type") == "done"]
    assert len(done_frames) == 1


async def test_signals_endpoint_ordering_by_seq(patched_app):
    app, db_path, client = patched_app
    await _seed_session(db_path, "order-sess")

    kinds = ["NodeQueued", "NodeStarted", "NodeCompleted"]
    async with StateDB(db_path) as db:
        for i, kind in enumerate(kinds):
            await db.insert_session_signal(
                session_id="order-sess",
                kind=kind,
                op_id="op-a",
                ts=float(100 + i),
                payload={},
            )
    async with StateDB(db_path) as db:
        await db.update_status(
            "session",
            "order-sess",
            new_status="completed",
            reason_code="run.completed.ok",
        )
    async with aiosqlite.connect(str(db_path)) as raw:
        await raw.execute("UPDATE sessions SET updated_at = 1.0 WHERE id = 'order-sess'")
        await raw.commit()

    collected: list[dict] = []
    async with client as ac:
        async with ac.stream("GET", "/api/sessions/order-sess/signals") as resp:
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                ev = json.loads(line[6:])
                collected.append(ev)
                if ev.get("type") == "done":
                    break

    signal_rows = [e for e in collected if "kind" in e]
    assert [r["kind"] for r in signal_rows] == kinds
    assert [r["seq"] for r in signal_rows] == [1, 2, 3]
