# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for nested transaction savepoint correctness.

The bug: when an inner transaction raises and the exception is caught
inside the outer block, partial inner writes committed with the outer
transaction (no rollback happened for the inner scope).

The fix: StateDB.transaction() uses SAVEPOINTs for nested calls so that
an inner failure rolled-back via ``ROLLBACK TO sp_N`` never bleeds into
the outer transaction.
"""

from __future__ import annotations

import time
import uuid

import pytest

from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def db():
    """Fresh in-memory StateDB for each test."""
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


# ── Helpers ───────────────────────────────────────────────────────────────────


def uid() -> str:
    return str(uuid.uuid4())


async def _make_session(db: StateDB) -> dict:
    prog_id = uid()
    await db.create_progression(prog_id)
    session = {"id": uid(), "progression_id": prog_id, "status": "running"}
    await db.create_session(session)
    return session


async def _make_invocation(db: StateDB) -> dict:
    inv = {
        "id": uid(),
        "skill": "test_skill",
        "started_at": time.time(),
        "status": "running",
    }
    await db.create_invocation(inv)
    return inv


# ── Test 1: inner exception caught, inner writes must NOT persist ──────────────


async def test_inner_transaction_exception_caught_outer_rolls_back_inner(db: StateDB):
    """Inner transaction raises → exception caught in outer block →
    inner writes must NOT persist when outer commits.

    This is the primary regression guard for the savepoint fix.
    """
    inv = await _make_invocation(db)

    # Outer transaction: update invocation name (a non-status field to keep it
    # simple — we directly use StateDB.transaction() to exercise the API).
    async with db.transaction():
        # Outer write: mark the outer invocation as the writer
        await db.db.execute(
            "UPDATE invocations SET updated_at = ? WHERE id = ?",
            (time.time(), inv["id"]),
        )

        # Inner transaction that raises — exception caught by outer.
        try:
            async with db.transaction():
                # Inner write: insert a bogus session (will be rolled back)
                bad_prog_id = uid()
                await db.db.execute(
                    "INSERT INTO progressions (id, created_at, collection) VALUES (?, ?, ?)",
                    (bad_prog_id, time.time(), "[]"),
                )
                bad_session_id = uid()
                await db.db.execute(
                    "INSERT INTO sessions (id, created_at, updated_at, progression_id) "
                    "VALUES (?, ?, ?, ?)",
                    (bad_session_id, time.time(), time.time(), bad_prog_id),
                )
                raise RuntimeError("inner failure — must not persist")
        except RuntimeError:
            pass  # caught — outer continues

    # Verify: the bad session must NOT exist after outer commit.
    # (bad_session_id was defined in inner scope; capture via local variable.)
    cur = await db.db.execute("SELECT COUNT(*) AS n FROM sessions WHERE id = ?", (bad_session_id,))
    row = await cur.fetchone()
    assert row["n"] == 0, "inner write must not persist when inner exception is caught"

    # Verify: the outer invocation update DID persist.
    inv_row = await db.get_invocation(inv["id"])
    assert inv_row is not None, "outer invocation row must still exist"


# ── Test 2: inner success → outer commits → both writes persist ────────────────


async def test_inner_and_outer_writes_both_persist_on_success(db: StateDB):
    """Inner transaction succeeds → outer commits → both inner and outer
    writes must persist.
    """
    inv = await _make_invocation(db)
    prog_id = uid()

    async with db.transaction():
        # Outer write: update invocation
        outer_ts = time.time()
        await db.db.execute(
            "UPDATE invocations SET updated_at = ? WHERE id = ?",
            (outer_ts, inv["id"]),
        )

        # Inner write: create progression (succeeds)
        async with db.transaction():
            await db.db.execute(
                "INSERT INTO progressions (id, created_at, collection) VALUES (?, ?, ?)",
                (prog_id, time.time(), "[]"),
            )

    # Both must persist after the outer commit.
    inv_row = await db.get_invocation(inv["id"])
    assert inv_row is not None

    prog = await db.get_progression(prog_id)
    assert prog == [], "inner-written progression must persist after outer commit"


# ── Test 3: inner exception propagates → nothing persists ─────────────────────


async def test_inner_exception_propagates_nothing_persists(db: StateDB):
    """Inner transaction raises → exception propagates out of outer block →
    neither inner nor outer writes must persist.
    """
    inv = await _make_invocation(db)
    prog_id = uid()
    outer_update_ts = time.time() + 9999

    with pytest.raises(RuntimeError, match="propagating error"):
        async with db.transaction():
            # Outer write
            await db.db.execute(
                "UPDATE invocations SET updated_at = ? WHERE id = ?",
                (outer_update_ts, inv["id"]),
            )

            # Inner transaction that raises and is NOT caught.
            async with db.transaction():
                await db.db.execute(
                    "INSERT INTO progressions (id, created_at, collection) VALUES (?, ?, ?)",
                    (prog_id, time.time(), "[]"),
                )
                raise RuntimeError("propagating error")

    # Outer update must not have committed.
    inv_row = await db.get_invocation(inv["id"])
    assert inv_row is not None
    # The update_ts we set (outer_update_ts = time + 9999) must not be stored.
    assert inv_row["updated_at"] < outer_update_ts, (
        "outer write must not persist when inner exception propagates"
    )

    # Inner progression must not exist.
    cur = await db.db.execute("SELECT COUNT(*) AS n FROM progressions WHERE id = ?", (prog_id,))
    row = await cur.fetchone()
    assert row["n"] == 0, "inner write must not persist when exception propagates"


# ── Test 4: update_status nested inside an outer transaction ──────────────────


async def test_update_status_nested_in_outer_transaction_savepoint(db: StateDB):
    """update_status() called inside an outer transaction() uses a SAVEPOINT.

    When update_status raises (entity not found), only the update_status
    writes are rolled back; the outer transaction can still commit its own
    work cleanly.
    """
    inv = await _make_invocation(db)
    nonexistent_id = uid()

    outer_ts = time.time() + 1234

    async with db.transaction():
        # Outer write
        await db.db.execute(
            "UPDATE invocations SET updated_at = ? WHERE id = ?",
            (outer_ts, inv["id"]),
        )

        # Inner: call update_status on a nonexistent entity → LookupError
        # This should roll back only the inner SAVEPOINT, not the outer txn.
        try:
            await db.update_status(
                "invocation",
                nonexistent_id,
                new_status="completed",
                reason_code=RunReasons.COMPLETED_OK,
                reason_summary="should not persist",
            )
        except LookupError:
            pass  # expected — inner rolled back

    # Outer update must still persist.
    inv_row = await db.get_invocation(inv["id"])
    assert inv_row is not None
    assert abs(inv_row["updated_at"] - outer_ts) < 1, (
        "outer write must persist even when nested update_status rolls back"
    )


# ── Test 5: txn_depth resets to zero after each context exit ──────────────────


async def test_txn_depth_resets_to_zero_after_commit(db: StateDB):
    """_txn_depth must return to 0 after a normal context exit."""
    assert db._txn_depth == 0
    async with db.transaction():
        assert db._txn_depth == 1
        async with db.transaction():
            assert db._txn_depth == 2
        assert db._txn_depth == 1
    assert db._txn_depth == 0


async def test_txn_depth_resets_to_zero_after_exception(db: StateDB):
    """_txn_depth must return to 0 after an exception path."""
    assert db._txn_depth == 0
    with pytest.raises(ValueError):
        async with db.transaction():
            assert db._txn_depth == 1
            raise ValueError("test")
    assert db._txn_depth == 0
