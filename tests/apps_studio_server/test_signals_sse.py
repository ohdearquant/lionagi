# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the session-signals SSE endpoint and service layer.

Coverage targets:
  - GET /api/sessions/{id}/signals  (404 on unknown session, ordering, auth)
  - lionagi.studio.services.signals.get_signals_after  (empty, replay, ordering)
  - lionagi.state.db.StateDB.insert_session_signal / get_session_signals_after
"""

from __future__ import annotations

import asyncio
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
    httpx >= 0.28 removed the ``app=`` shorthand from AsyncClient; use
    ASGITransport explicitly.
    """
    pytest.importorskip("fastapi", reason="studio extra not installed")
    httpx = pytest.importorskip("httpx", reason="httpx not installed")

    import lionagi.studio.services.sessions as sessions_svc
    import lionagi.studio.services.signals as signals_svc

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(sessions_svc, "_DB", str(db_path))
    monkeypatch.setattr(sessions_svc, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(signals_svc, "_DB", str(db_path))
    monkeypatch.setattr(signals_svc, "DEFAULT_DB_PATH", db_path)

    from lionagi.studio.app import app

    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return app, db_path, client


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


# ---------------------------------------------------------------------------
# Production bind-site integration: observer.bind_db_persistence via the
# CLI persist layer.  These tests prove the ignition wire exists and that
# signals flow end-to-end from emit() → session_signals table → SSE service.
# ---------------------------------------------------------------------------


async def test_bind_db_persistence_production_path(tmp_path):
    """End-to-end: bind via the production call pattern, emit real Signal rows.

    Replicates exactly what setup_agent_persist / setup_orchestration_persist
    do: open a StateDB, create a session row, call
    observer.bind_db_persistence(session_id, db=db) with the live handle, then
    emit signals via the observer.  Asserts rows land in session_signals and are
    readable via get_session_signals_after().
    """
    from lionagi.session.observer import SessionObserver
    from lionagi.session.session import Session
    from lionagi.session.signal import NodeCompleted, NodeStarted, RunStart

    db_path = tmp_path / "state.db"
    session_id = "prod-bind-sess-1"

    async with StateDB(db_path) as db:
        # Replicate the session-row creation done by setup_agent_persist.
        prog_id = f"{session_id}-prog"
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": session_id,
                "created_at": 100.0,
                "progression_id": prog_id,
                "name": "prod-bind-test",
                "status": "running",
                "invocation_kind": "agent",
            }
        )

        # Production bind call: observer + the already-open db handle.
        session = Session(name="prod-test")
        observer: SessionObserver = session.observer
        observer.bind_db_persistence(session_id, db=db)

        # Emit signals the way the CLI runtime would.
        await observer.emit(RunStart())
        await observer.emit(NodeStarted(op_id="op-x", name="step1"))
        await observer.emit(NodeCompleted(op_id="op-x", name="step1", elapsed=0.5))

        rows = await db.get_session_signals_after(session_id, 0)

    assert len(rows) == 3
    assert rows[0]["kind"] == "RunStart"
    assert rows[1]["kind"] == "NodeStarted"
    assert rows[2]["kind"] == "NodeCompleted"
    assert rows[1]["op_id"] == "op-x"
    assert rows[2]["payload"]["elapsed"] == 0.5
    # seq must be monotone.
    assert [r["seq"] for r in rows] == [1, 2, 3]


async def test_bind_db_persistence_unbind_stops_writes(tmp_path):
    """After unbind_db_persistence(), further emit() calls write no new rows."""
    from lionagi.session.session import Session
    from lionagi.session.signal import NodeCompleted, NodeStarted

    db_path = tmp_path / "state.db"
    session_id = "prod-bind-sess-2"

    async with StateDB(db_path) as db:
        prog_id = f"{session_id}-prog"
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": session_id,
                "created_at": 100.0,
                "progression_id": prog_id,
                "name": "unbind-test",
                "status": "running",
                "invocation_kind": "agent",
            }
        )

        observer = Session(name="unbind-test").observer
        observer.bind_db_persistence(session_id, db=db)

        await observer.emit(NodeStarted(op_id="op-a", name="step"))
        rows_before = await db.get_session_signals_after(session_id, 0)
        assert len(rows_before) == 1

        # Teardown: unbind then emit.
        observer.unbind_db_persistence()
        await observer.emit(NodeCompleted(op_id="op-a", name="step", elapsed=1.0))

        rows_after = await db.get_session_signals_after(session_id, 0)

    # The second signal must not have been persisted.
    assert len(rows_after) == 1
    assert rows_after[0]["kind"] == "NodeStarted"


async def test_setup_agent_persist_wires_signal_bind(tmp_path, monkeypatch):
    """setup_agent_persist() calls bind_db_persistence so signals reach the DB.

    Patches StateDB in lionagi.state.db to redirect the default DB path to
    tmp_path, then calls the real setup_agent_persist function with a bare
    Branch and verifies that emitting a Signal on the resulting session observer
    writes a row to session_signals.
    """
    import lionagi.state.db as db_mod

    _real_StateDB = db_mod.StateDB
    _real_DEFAULT = db_mod.DEFAULT_DB_PATH

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", db_path)

    # Wrap StateDB so the default path resolves to our tmp file.
    class _PatchedStateDB(_real_StateDB):
        def __init__(self, path=None):
            super().__init__(path if path is not None else db_path)

    monkeypatch.setattr(db_mod, "StateDB", _PatchedStateDB)
    # Also patch the import inside _persist.py (function-local import).
    import lionagi.cli._persist as persist_mod

    monkeypatch.setattr(persist_mod, "StateDB", _PatchedStateDB, raising=False)

    from lionagi.cli._persist import setup_agent_persist, teardown_persist
    from lionagi.session.signal import RunStart

    try:
        from lionagi import Branch
    except Exception:
        pytest.skip("lionagi.Branch not importable in this environment")

    branch = Branch()
    ctx = await setup_agent_persist(branch)
    assert ctx is not None, "setup_agent_persist returned None — persistence setup failed"

    session_id = ctx["session_id"]
    session_obj = ctx["session"]

    # Emit a signal via the session observer — the production path.
    await session_obj.observer.emit(RunStart())

    # Read back from the DB using the same connection (still open in ctx["db"]).
    rows = await ctx["db"].get_session_signals_after(session_id, 0)
    assert len(rows) == 1
    assert rows[0]["kind"] == "RunStart"

    # Teardown should unbind without error.
    await teardown_persist(ctx, status="completed")


# ---------------------------------------------------------------------------
# MAJOR-1: Concurrent emit must not drop rows (regression for BEGIN IMMEDIATE
# on shared connection without per-instance asyncio lock).
# ---------------------------------------------------------------------------


async def test_concurrent_emit_all_rows_present(tmp_path):
    """50 concurrent NodeStarted emits through the BOUND observer all land in DB.

    Before the _write_lock fix, concurrent coroutines on the same aiosqlite
    connection raced on BEGIN IMMEDIATE and produced 'cannot start a transaction
    within a transaction' errors — all swallowed, leaving only ~1 row.
    """
    from lionagi.session.observer import SessionObserver
    from lionagi.session.session import Session
    from lionagi.session.signal import NodeStarted

    db_path = tmp_path / "state.db"
    session_id = "concurrent-emit-sess"
    n = 50

    async with StateDB(db_path) as db:
        prog_id = f"{session_id}-prog"
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": session_id,
                "created_at": 100.0,
                "progression_id": prog_id,
                "name": "concurrent-test",
                "status": "running",
                "invocation_kind": "agent",
            }
        )

        session = Session(name="concurrent-test")
        observer: SessionObserver = session.observer
        observer.bind_db_persistence(session_id, db=db)

        # Emit all signals concurrently to exercise the lock path.
        await asyncio.gather(
            *[observer.emit(NodeStarted(op_id=f"op-{i}", name=f"step-{i}")) for i in range(n)]
        )

        rows = await db.get_session_signals_after(session_id, 0)

    assert len(rows) == n, f"Expected {n} rows, got {len(rows)} — concurrent emit dropped rows"
    # seq values must be contiguous 1..n (no gaps from dropped transactions).
    seqs = sorted(r["seq"] for r in rows)
    assert seqs == list(range(1, n + 1)), f"seq gaps detected: {seqs}"


# ---------------------------------------------------------------------------
# MAJOR-2: Payload safety — non-serialisable objects and large payloads.
# ---------------------------------------------------------------------------


async def test_payload_sanitizer_node_escalated_arbitrary_request(tmp_path):
    """NodeEscalated with escalation_request=object() must persist (not be dropped).

    Before the safe_fallback fix, the payload builder called _json_dumps on
    escalation_request=object() without safe_fallback, raising TypeError.
    The exception was swallowed → zero rows persisted.
    """
    from lionagi.session.observer import SessionObserver, _sanitize_signal_payload
    from lionagi.session.session import Session
    from lionagi.session.signal import NodeEscalated

    sig = NodeEscalated(
        op_id="op-esc",
        name="escstep",
        reason="no higher tier",
        route="give_up",
        escalation_request=object(),  # not JSON serialisable
    )

    # Direct sanitizer should not raise and must return a non-empty dict.
    payload = _sanitize_signal_payload(sig)
    assert isinstance(payload, dict)
    # escalation_request must be present as a string (repr fallback).
    assert "escalation_request" in payload
    assert isinstance(payload["escalation_request"], str)

    # End-to-end: emitting through a DB-bound observer must persist a row.
    db_path = tmp_path / "state.db"
    session_id = "esc-payload-sess"
    async with StateDB(db_path) as db:
        prog_id = f"{session_id}-prog"
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": session_id,
                "created_at": 100.0,
                "progression_id": prog_id,
                "name": "esc-test",
                "status": "running",
                "invocation_kind": "agent",
            }
        )
        session = Session(name="esc-test")
        observer: SessionObserver = session.observer
        observer.bind_db_persistence(session_id, db=db)
        await observer.emit(sig)
        rows = await db.get_session_signals_after(session_id, 0)

    assert len(rows) == 1, "NodeEscalated with non-serialisable request was dropped"
    assert rows[0]["kind"] == "NodeEscalated"


async def test_payload_sanitizer_hook_signal_non_json_kwargs(tmp_path):
    """HookSignal with non-JSON kwargs (dict[str, Any]) must persist a sanitized row."""
    from lionagi.hooks.bus import HookPoint, HookSignal
    from lionagi.session.observer import _sanitize_signal_payload
    from lionagi.session.session import Session

    sig = HookSignal(
        point=HookPoint.MESSAGE_ADD,
        kwargs={"fn": lambda x: x, "obj": object()},  # both non-JSON-safe
    )

    payload = _sanitize_signal_payload(sig)
    assert isinstance(payload, dict)
    # kwargs must be present in some form (repr or string fallback).
    assert "kwargs" in payload

    # End-to-end.
    db_path = tmp_path / "state.db"
    session_id = "hook-payload-sess"
    async with StateDB(db_path) as db:
        prog_id = f"{session_id}-prog"
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": session_id,
                "created_at": 100.0,
                "progression_id": prog_id,
                "name": "hook-test",
                "status": "running",
                "invocation_kind": "agent",
            }
        )
        session = Session(name="hook-test")
        session.observer.bind_db_persistence(session_id, db=db)
        await session.observer.emit(sig)
        rows = await db.get_session_signals_after(session_id, 0)

    assert len(rows) == 1, "HookSignal with non-JSON kwargs was dropped"


async def test_payload_sanitizer_large_payload_truncated(tmp_path):
    """A payload exceeding _PAYLOAD_BYTE_CAP is stored with truncated=true marker."""
    from lionagi.session.observer import _PAYLOAD_BYTE_CAP, _sanitize_signal_payload
    from lionagi.session.signal import NodeCompleted

    # Create a signal whose name field is huge (well over the 16KB cap).
    big_name = "x" * (_PAYLOAD_BYTE_CAP + 1000)
    sig = NodeCompleted(op_id="op-big", name=big_name, elapsed=0.1)

    payload = _sanitize_signal_payload(sig)
    # Must be truncated.
    assert payload.get("truncated") is True
    assert "original_bytes" in payload
    assert payload["original_bytes"] > _PAYLOAD_BYTE_CAP

    # The truncated payload itself must be JSON-safe (no TypeError on insert).
    db_path = tmp_path / "state.db"
    session_id = "large-payload-sess"
    async with StateDB(db_path) as db:
        prog_id = f"{session_id}-prog"
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": session_id,
                "created_at": 100.0,
                "progression_id": prog_id,
                "name": "large-test",
                "status": "running",
                "invocation_kind": "agent",
            }
        )
        seq = await db.insert_session_signal(
            session_id=session_id,
            kind="NodeCompleted",
            op_id="op-big",
            ts=1000.0,
            payload=payload,
        )
        rows = await db.get_session_signals_after(session_id, 0)

    assert len(rows) == 1
    assert rows[0]["payload"].get("truncated") is True
    assert seq == 1


async def test_payload_sanitizer_message_added_stores_ref_not_body(tmp_path):
    """MessageAdded must persist a compact message_ref, not the full message body."""
    from unittest.mock import MagicMock

    from lionagi.session.observer import _sanitize_signal_payload
    from lionagi.session.signal import MessageAdded

    # Build a mock message with a large content body.
    msg = MagicMock()
    msg.id = "msg-abc123"
    msg.role = "assistant"
    msg.sender = "branch-1"
    msg.recipient = None
    sig = MessageAdded(data=msg)

    payload = _sanitize_signal_payload(sig)

    # Must have message_ref with id and role, NOT the full body.
    assert "message_ref" in payload
    ref = payload["message_ref"]
    assert ref["id"] == "msg-abc123"
    assert ref["role"] == "assistant"
    assert ref["sender"] == "branch-1"
    # The full message content must not appear.
    assert "content" not in payload
    assert "data" not in payload


# ---------------------------------------------------------------------------
# MINOR: Bearer auth rejection for /api/sessions/{id}/signals
# ---------------------------------------------------------------------------


def test_signals_endpoint_requires_bearer_auth(tmp_path, monkeypatch):
    """GET /api/sessions/{id}/signals returns 401 when auth token is set and absent.

    Uses the synchronous TestClient (same pattern as test_audit_remediation.py)
    so the test does not need an async streaming context.
    """
    pytest.importorskip("fastapi", reason="studio extra not installed")
    from fastapi.testclient import TestClient

    import lionagi.studio.services.sessions as sessions_svc
    import lionagi.studio.services.signals as signals_svc

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(sessions_svc, "_DB", str(db_path))
    monkeypatch.setattr(sessions_svc, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(signals_svc, "_DB", str(db_path))
    monkeypatch.setattr(signals_svc, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setenv("LIONAGI_STUDIO_AUTH_TOKEN", "secret-token")

    from lionagi.studio.app import app

    client = TestClient(app, raise_server_exceptions=False)

    # No Authorization header → 401.
    resp = client.get("/api/sessions/any-session-id/signals")
    assert resp.status_code == 401, (
        f"Expected 401 when auth token is set and no header provided, got {resp.status_code}"
    )

    # Wrong token → 401.
    resp_wrong = client.get(
        "/api/sessions/any-session-id/signals",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp_wrong.status_code == 401

    # Correct token → auth passes (404 or 200, not 401).
    resp_ok = client.get(
        "/api/sessions/any-session-id/signals",
        headers={"Authorization": "Bearer secret-token"},
    )
    assert resp_ok.status_code != 401, (
        f"Correct bearer token was rejected with {resp_ok.status_code}"
    )


# ---------------------------------------------------------------------------
# MINOR: Generator cancellation — client disconnect stops the SSE generator.
# ---------------------------------------------------------------------------


async def test_signals_generator_cancellation_on_disconnect(tmp_path, monkeypatch):
    """SSE generator exits cleanly when cancelled (simulates client disconnect).

    We directly instantiate the ``generate()`` async-generator from the signals
    router, schedule it as an asyncio task, cancel it after it starts running,
    and assert it raises ``CancelledError`` — not any unhandled exception.
    This approach avoids the httpx streaming-backpressure issue where
    ``async with client.stream(...)`` waits for the server-side generator to
    drain before the client context-manager exits.
    """
    pytest.importorskip("fastapi", reason="studio extra not installed")
    pytest.importorskip("httpx", reason="httpx not installed")

    import lionagi.studio.services.sessions as sessions_svc
    import lionagi.studio.services.signals as signals_svc

    db_path = tmp_path / "state.db"
    session_id = "cancel-test-sess"

    # Seed a RUNNING session — the generator will keep polling indefinitely.
    async with StateDB(db_path) as db:
        prog_id = f"{session_id}-prog"
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": session_id,
                "created_at": 100.0,
                "progression_id": prog_id,
                "name": "cancel-test",
                "status": "running",
                "invocation_kind": "agent",
            }
        )

    monkeypatch.setattr(sessions_svc, "_DB", str(db_path))
    monkeypatch.setattr(sessions_svc, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(signals_svc, "_DB", str(db_path))
    monkeypatch.setattr(signals_svc, "DEFAULT_DB_PATH", db_path)

    # Import the router's generate factory directly.
    from lionagi.studio.routers.signals import stream_signals

    # The endpoint function builds a StreamingResponse whose body is the
    # generate() async-generator.  We call it directly to get the generator.
    response = await stream_signals(session_id)
    generator = response.body_iterator  # the generate() async gen

    async def _drain_one():
        """Consume a single SSE frame from the generator."""
        async for chunk in generator:
            return chunk  # consume the first yielded frame then return

    # Drive the generator one step so it enters asyncio.sleep(0.5) inside.
    # Then cancel the drain task to simulate mid-stream disconnect.
    task = asyncio.create_task(_drain_one())

    # Wait briefly for the generator to start running (enter the while loop).
    await asyncio.sleep(0.05)

    task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass  # expected — task was cancelled

    # The generator must be cancellable without propagating unexpected errors.
    # Explicitly close the generator to release its resources.
    try:
        await generator.aclose()
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"generator.aclose() raised unexpected {type(exc).__name__}: {exc}")
