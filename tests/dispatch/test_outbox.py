# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the durable dispatch outbox core (ADR-0092 slice 1): CAS transitions,
backoff, dead_letter, dedup_key, ack flow, argv-safety, and direct-DB writes
with no daemon running.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("aiosqlite", reason="aiosqlite not installed")

from lionagi.dispatch import (
    ack_dispatch,
    backoff_seconds,
    deliver_due_dispatches,
    enqueue_dispatch,
    get_dispatch,
    list_dispatches,
    purge_dispatch,
    retry_dispatch,
)
from lionagi.state.db import StateDB

_SUCCESS_TEMPLATE = [sys.executable, "-c", "import sys; sys.exit(0)"]
_FAIL_TEMPLATE = [sys.executable, "-c", "import sys; sys.exit(1)"]


def _capture_argv_script(out_path: Path) -> list[str]:
    """A notify template that writes its argv (repr) to out_path, then exits 0."""
    return [
        sys.executable,
        "-c",
        f"import sys, pathlib; pathlib.Path({str(out_path)!r}).write_text(repr(sys.argv[1:])); sys.exit(0)",
        "{deliver_to}",
        "{payload}",
    ]


# ── backoff ───────────────────────────────────────────────────────────────────


def test_backoff_seconds_matches_formula():
    assert backoff_seconds(0) == 30
    assert backoff_seconds(1) == 60
    assert backoff_seconds(2) == 120
    assert backoff_seconds(3) == 240


def test_backoff_seconds_caps_at_1800():
    assert backoff_seconds(10) == 1800
    assert backoff_seconds(20) == 1800


# ── enqueue + dedup_key ───────────────────────────────────────────────────────


async def test_enqueue_creates_pending_row(tmp_path: Path):
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        dispatch_id = await enqueue_dispatch(
            db, kind="terminal_notify", deliver_to="seat-1", body={"a": 1}
        )
        row = await get_dispatch(db, dispatch_id)

    assert row is not None
    assert row["status"] == "pending"
    assert row["attempt"] == 0
    assert row["kind"] == "terminal_notify"
    assert row["deliver_to"] == "seat-1"
    assert row["payload"]["body"] == {"a": 1}
    assert row["payload"]["dispatch_id"] == dispatch_id


async def test_dedup_key_prevents_double_queue(tmp_path: Path):
    """Re-enqueuing with the same dedup_key returns the existing row's id, not a new one."""
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        id1 = await enqueue_dispatch(
            db, kind="revival_ping", deliver_to="seat-1", dedup_key="revival:seat-1:100"
        )
        id2 = await enqueue_dispatch(
            db, kind="revival_ping", deliver_to="seat-1", dedup_key="revival:seat-1:100"
        )
        rows = await list_dispatches(db)

    assert id1 == id2
    assert len(rows) == 1


async def test_dedup_key_none_allows_multiple_rows(tmp_path: Path):
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        await enqueue_dispatch(db, kind="terminal_notify", deliver_to="seat-1")
        await enqueue_dispatch(db, kind="terminal_notify", deliver_to="seat-1")
        rows = await list_dispatches(db)

    assert len(rows) == 2


# ── delivery loop: backoff / dead_letter / expiry ────────────────────────────


async def test_transport_failure_backs_off_and_stays_pending(tmp_path: Path):
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        dispatch_id = await enqueue_dispatch(db, kind="terminal_notify", deliver_to="seat-1")
        now = time.time()
        counts = await deliver_due_dispatches(db, now=now, notify_template=_FAIL_TEMPLATE)
        row = await get_dispatch(db, dispatch_id)

    assert counts == {"attempted": 1, "delivered": 0, "dead_letter": 0, "expired": 0, "retried": 1}
    assert row["status"] == "pending"
    assert row["attempt"] == 1
    assert row["last_error"]
    assert row["next_attempt_at"] >= now + backoff_seconds(1) - 1


async def test_max_attempts_produces_dead_letter(tmp_path: Path):
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        dispatch_id = await enqueue_dispatch(
            db, kind="terminal_notify", deliver_to="seat-1", max_attempts=1
        )
        now = time.time()
        counts = await deliver_due_dispatches(db, now=now, notify_template=_FAIL_TEMPLATE)
        row = await get_dispatch(db, dispatch_id)

    assert counts["dead_letter"] == 1
    assert row["status"] == "dead_letter"


async def test_dead_letter_records_reason_code_in_status_transitions(tmp_path: Path):
    from sqlalchemy import text

    from lionagi.state.reasons import DispatchReasons

    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        dispatch_id = await enqueue_dispatch(
            db, kind="terminal_notify", deliver_to="seat-1", max_attempts=1
        )
        await deliver_due_dispatches(db, now=time.time(), notify_template=_FAIL_TEMPLATE)
        async with db._read() as conn:
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT reason_code FROM status_transitions "
                            "WHERE entity_type = 'dispatch' AND entity_id = :id "
                            "ORDER BY created_at"
                        ),
                        {"id": dispatch_id},
                    )
                )
                .mappings()
                .all()
            )

    codes = [r["reason_code"] for r in rows]
    assert DispatchReasons.DEAD_LETTER_MAX_ATTEMPTS in codes


async def test_expires_at_transitions_to_expired(tmp_path: Path):
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        dispatch_id = await enqueue_dispatch(
            db, kind="terminal_notify", deliver_to="seat-1", expires_at=time.time() - 1
        )
        # Scan strictly after enqueue's own next_attempt_at=now stamp, so the
        # due-scan's own WHERE clause selects this row.
        counts = await deliver_due_dispatches(
            db, now=time.time() + 1, notify_template=_SUCCESS_TEMPLATE
        )
        row = await get_dispatch(db, dispatch_id)

    assert counts["expired"] == 1
    assert row["status"] == "expired"


async def test_transport_success_transitions_to_delivered(tmp_path: Path):
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        dispatch_id = await enqueue_dispatch(db, kind="terminal_notify", deliver_to="seat-1")
        counts = await deliver_due_dispatches(
            db, now=time.time(), notify_template=_SUCCESS_TEMPLATE
        )
        row = await get_dispatch(db, dispatch_id)

    assert counts["delivered"] == 1
    assert row["status"] == "delivered"
    assert row["attempt"] == 1


async def test_no_notify_template_configured_is_a_transport_failure(tmp_path: Path, monkeypatch):
    """With no dispatch.notify_template configured, delivery backs off rather than crashing."""
    monkeypatch.setattr("lionagi.dispatch.outbox.resolve_notify_template", lambda: None)
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        dispatch_id = await enqueue_dispatch(db, kind="terminal_notify", deliver_to="seat-1")
        counts = await deliver_due_dispatches(db, now=time.time())
        row = await get_dispatch(db, dispatch_id)

    assert counts["retried"] == 1
    assert row["status"] == "pending"
    assert "no dispatch.notify_template configured" in row["last_error"]


# ── CAS transition guards ────────────────────────────────────────────────────


async def test_illegal_transition_is_rejected(tmp_path: Path):
    """transition() with a from_state mismatch is a no-op CAS conflict, not a write."""
    from lionagi.state.reasons import DispatchReasons
    from lionagi.state.transitions import Actor, StateReason, TransitionRequest, transition

    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        dispatch_id = await enqueue_dispatch(db, kind="terminal_notify", deliver_to="seat-1")

        result = await transition(
            db,
            TransitionRequest(
                entity_type="dispatch",
                entity_id=dispatch_id,
                from_state="delivered",  # wrong: row is actually 'pending'
                to_state="acked",
                reason=StateReason(code=DispatchReasons.ACKED_CONSUMER),
                actor=Actor(type="operator", id="test"),
                idempotency_key="bad-transition",
            ),
        )
        row = await get_dispatch(db, dispatch_id)

    assert result.applied is False
    assert result.conflict is True
    assert row["status"] == "pending"  # unchanged


# ── ack flow ──────────────────────────────────────────────────────────────────


async def test_ack_required_flow_with_correct_token(tmp_path: Path):
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        dispatch_id = await enqueue_dispatch(
            db, kind="terminal_notify", deliver_to="seat-1", ack_required=True
        )
        row = await get_dispatch(db, dispatch_id)
        token = row["ack_token"]
        assert token

        applied = await ack_dispatch(db, dispatch_id, token)
        row = await get_dispatch(db, dispatch_id)

    assert applied is True
    assert row["status"] == "acked"


async def test_ack_wrong_token_raises(tmp_path: Path):
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        dispatch_id = await enqueue_dispatch(
            db, kind="terminal_notify", deliver_to="seat-1", ack_required=True
        )
        with pytest.raises(ValueError, match="ack_token mismatch"):
            await ack_dispatch(db, dispatch_id, "wrong-token")


async def test_ack_not_required_raises(tmp_path: Path):
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        dispatch_id = await enqueue_dispatch(db, kind="terminal_notify", deliver_to="seat-1")
        with pytest.raises(ValueError, match="does not require ack"):
            await ack_dispatch(db, dispatch_id, "any-token")


async def test_default_tier_delivered_without_ack_required(tmp_path: Path):
    """ack_required=0 rows complete at 'delivered' on first transport success."""
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        dispatch_id = await enqueue_dispatch(db, kind="terminal_notify", deliver_to="seat-1")
        await deliver_due_dispatches(db, now=time.time(), notify_template=_SUCCESS_TEMPLATE)
        row = await get_dispatch(db, dispatch_id)

    assert row["status"] == "delivered"


async def test_ack_required_tier_loops_back_to_pending_on_success(tmp_path: Path):
    """ack_required=1 rows go back to pending (not delivered) on transport success."""
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        dispatch_id = await enqueue_dispatch(
            db, kind="terminal_notify", deliver_to="seat-1", ack_required=True
        )
        await deliver_due_dispatches(db, now=time.time(), notify_template=_SUCCESS_TEMPLATE)
        row = await get_dispatch(db, dispatch_id)

    assert row["status"] == "pending"
    assert row["next_attempt_at"] > time.time()


# ── retry / purge (direct-DB, no daemon) ─────────────────────────────────────


async def test_retry_forces_dead_letter_row_back_to_pending(tmp_path: Path):
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        dispatch_id = await enqueue_dispatch(
            db, kind="terminal_notify", deliver_to="seat-1", max_attempts=1
        )
        await deliver_due_dispatches(db, now=time.time(), notify_template=_FAIL_TEMPLATE)
        row = await get_dispatch(db, dispatch_id)
        assert row["status"] == "dead_letter"

        applied = await retry_dispatch(db, dispatch_id)
        row = await get_dispatch(db, dispatch_id)

    assert applied is True
    assert row["status"] == "pending"
    assert row["attempt"] == 0


async def test_retry_rejects_non_terminal_row(tmp_path: Path):
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        dispatch_id = await enqueue_dispatch(db, kind="terminal_notify", deliver_to="seat-1")
        with pytest.raises(ValueError, match="retry only applies"):
            await retry_dispatch(db, dispatch_id)


async def test_purge_deletes_row_with_no_daemon_running(tmp_path: Path):
    """Direct-DB ack/purge works with no scheduler daemon involved (RIDER B)."""
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        dispatch_id = await enqueue_dispatch(db, kind="terminal_notify", deliver_to="seat-1")
        deleted = await purge_dispatch(db, dispatch_id)
        row = await get_dispatch(db, dispatch_id)

    assert deleted is True
    assert row is None


async def test_purge_missing_id_returns_false(tmp_path: Path):
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        deleted = await purge_dispatch(db, "does-not-exist")
    assert deleted is False


# ── argv-safety (RIDER A) ─────────────────────────────────────────────────────


async def test_shell_metacharacter_payload_does_not_execute(tmp_path: Path):
    """A payload containing shell metacharacters must not execute anything — argv-exec only."""
    marker = tmp_path / "should-not-exist"
    db_path = tmp_path / "state.db"
    hostile_body = {"cmd": f"; touch {marker}; echo pwned $(touch {marker})"}

    template = _capture_argv_script(tmp_path / "argv.txt")

    async with StateDB(db_path) as db:
        await enqueue_dispatch(
            db,
            kind="terminal_notify",
            deliver_to="seat-1; touch also-not-created",
            body=hostile_body,
        )
        counts = await deliver_due_dispatches(db, now=time.time(), notify_template=template)

    assert counts["delivered"] == 1
    assert not marker.exists()
    assert not (tmp_path / "also-not-created").exists()

    argv_out = (tmp_path / "argv.txt").read_text()
    # The hostile string landed verbatim as ONE argv element, not executed.
    assert "seat-1; touch also-not-created" in argv_out


async def test_payload_delivered_via_stdin_when_template_has_no_placeholder(tmp_path: Path):
    """A template without {payload} still receives the JSON body, via stdin."""
    out_path = tmp_path / "stdin_capture.json"
    template = [
        sys.executable,
        "-c",
        f"import sys, pathlib; pathlib.Path({str(out_path)!r}).write_bytes(sys.stdin.buffer.read()); sys.exit(0)",
        "{deliver_to}",
    ]
    db_path = tmp_path / "state.db"
    async with StateDB(db_path) as db:
        await enqueue_dispatch(db, kind="terminal_notify", deliver_to="seat-1", body={"z": 9})
        counts = await deliver_due_dispatches(db, now=time.time(), notify_template=template)

    assert counts["delivered"] == 1
    captured = out_path.read_text()
    assert '"z": 9' in captured
