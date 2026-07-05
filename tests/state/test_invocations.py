# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0020 invocations table tests.

Covers: CRUD lifecycle, session_count denormalization, session ↔
invocation linkage, validation of the status vocabulary, and list /
filter behavior used by /api/invocations.
"""

from __future__ import annotations

import time
import uuid

import pytest

from lionagi.state.db import _INVOCATION_STATUSES, StateDB


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


def _uid() -> str:
    return str(uuid.uuid4())


def _short_id() -> str:
    return uuid.uuid4().hex[:12]


async def _make_invocation(db: StateDB, **fields) -> dict:
    inv = {
        "id": _short_id(),
        "skill": fields.pop("skill", "show"),
        "started_at": fields.pop("started_at", time.time()),
        **fields,
    }
    await db.create_invocation(inv)
    return inv


async def _make_session(db: StateDB, *, invocation_id: str | None = None, **fields) -> dict:
    prog_id = _uid()
    await db.create_progression(prog_id)
    session = {
        "id": _uid(),
        "progression_id": prog_id,
        "invocation_id": invocation_id,
        **fields,
    }
    await db.create_session(session)
    return session


# ── Vocabulary ────────────────────────────────────────────────────────────────


def test_invocation_status_vocabulary_matches_adr0025():
    """Invocations share the ADR-0025 terminal set (now seven values, with
    'completed_empty' for the completion-trust gate) + 'running'."""
    assert _INVOCATION_STATUSES == frozenset(
        {
            "running",
            "completed",
            "completed_empty",
            "failed",
            "timed_out",
            "aborted",
            "cancelled",
        }
    )


# ── CRUD ──────────────────────────────────────────────────────────────────────


async def test_create_and_get_invocation(db: StateDB):
    inv = await _make_invocation(
        db,
        skill="show",
        plugin="show",
        prompt="resolve lionagi issues",
        node_metadata={"plays": []},
    )
    fetched = await db.get_invocation(inv["id"])
    assert fetched["id"] == inv["id"]
    assert fetched["skill"] == "show"
    assert fetched["plugin"] == "show"
    assert fetched["prompt"] == "resolve lionagi issues"
    assert fetched["status"] == "running"
    assert fetched["session_count"] == 0


async def test_update_invocation_status_terminal(db: StateDB):
    inv = await _make_invocation(db)
    await db.update_invocation(inv["id"], status="completed", ended_at=time.time())
    fetched = await db.get_invocation(inv["id"])
    assert fetched["status"] == "completed"
    assert fetched["ended_at"] is not None


async def test_update_invocation_rejects_unknown_status(db: StateDB):
    inv = await _make_invocation(db)
    with pytest.raises(ValueError, match="ADR-0020"):
        await db.update_invocation(inv["id"], status="stale")


async def test_update_invocation_rejects_unknown_column(db: StateDB):
    inv = await _make_invocation(db)
    with pytest.raises(ValueError, match="Invalid column"):
        await db.update_invocation(inv["id"], not_a_column="x")


# ── Session linkage ───────────────────────────────────────────────────────────


async def test_create_session_with_invocation_bumps_count(db: StateDB):
    """ADR-0020: invocations.session_count tracks attached sessions
    via the create_session denormalized increment."""
    inv = await _make_invocation(db)
    await _make_session(db, invocation_id=inv["id"], status="running")
    await _make_session(db, invocation_id=inv["id"], status="running")
    await _make_session(db, invocation_id=None, status="running")  # standalone

    fetched = await db.get_invocation(inv["id"])
    assert fetched["session_count"] == 2


async def test_duplicate_create_session_does_not_inflate_session_count(db: StateDB):
    """Regression: a duplicate create_session call (same id) must not increment
    session_count a second time (INSERT OR IGNORE no-op)."""
    inv = await _make_invocation(db)
    session = await _make_session(db, invocation_id=inv["id"], status="running")

    # Replay the exact same session dict — simulates an idempotent retry.
    await db.create_session(session)

    fetched = await db.get_invocation(inv["id"])
    assert fetched["session_count"] == 1, (
        "session_count must be 1 after one distinct session, even if create_session "
        "was called twice with the same id"
    )
    rows = await db.list_sessions_for_invocation(inv["id"])
    assert len(rows) == 1


async def test_list_sessions_for_invocation_orders_by_created(db: StateDB):
    inv = await _make_invocation(db)
    s1 = await _make_session(
        db,
        invocation_id=inv["id"],
        status="running",
    )
    s2 = await _make_session(
        db,
        invocation_id=inv["id"],
        status="completed",
    )
    rows = await db.list_sessions_for_invocation(inv["id"])
    assert [r["id"] for r in rows] == [s1["id"], s2["id"]]


async def test_session_without_invocation_id_is_unaffected(db: StateDB):
    """Sessions with no invocation_id continue to work — backward-compat."""
    s = await _make_session(db, invocation_id=None, status="running")
    fetched = await db.get_session(s["id"])
    assert fetched["invocation_id"] is None


# ── List + filter ─────────────────────────────────────────────────────────────


async def test_list_invocations_filters_by_skill(db: StateDB):
    await _make_invocation(db, skill="show")
    await _make_invocation(db, skill="codex-pr-review")
    await _make_invocation(db, skill="show")

    only_show = await db.list_invocations(skill="show")
    assert len(only_show) == 2
    assert all(r["skill"] == "show" for r in only_show)


async def test_list_invocations_filters_by_status(db: StateDB):
    a = await _make_invocation(db)
    b = await _make_invocation(db)
    await db.update_invocation(a["id"], status="completed", ended_at=time.time())

    running_only = await db.list_invocations(status="running")
    assert {r["id"] for r in running_only} == {b["id"]}

    done_only = await db.list_invocations(status="completed")
    assert {r["id"] for r in done_only} == {a["id"]}
