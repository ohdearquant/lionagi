# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The due cursor is the claim two schedulers race for."""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from sqlalchemy import text

from lionagi.state.db import StateDB


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


async def _schedule(db: StateDB, *, due: float | None) -> str:
    sid = "sched-" + uuid.uuid4().hex[:8]
    await db.create_schedule(
        {
            "id": sid,
            "name": sid,
            "description": "",
            "enabled": 1,
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
            "action_prompt": "noop",
            "next_fire_at": due,
            "last_fired_at": None,
        }
    )
    return sid


def _occurrence(sid: str, fired_at: float) -> dict:
    return {
        "id": "run-" + uuid.uuid4().hex[:8],
        "schedule_id": sid,
        "invocation_id": None,
        "trigger_context": {"fired_at": fired_at},
        "action_kind": "agent",
        "action_args": {},
        "status": "running",
        "exit_code": None,
        "chain_parent_id": None,
        "chain_depth": 0,
        "fired_at": fired_at,
        "ended_at": None,
        "error_detail": None,
        "created_at": time.time(),
    }


async def _occurrence_count(db: StateDB, sid: str) -> int:
    async with db._tx() as conn:
        return (
            await conn.execute(
                text("SELECT COUNT(*) FROM schedule_runs WHERE schedule_id = :s"), {"s": sid}
            )
        ).scalar()


async def _cursor(db: StateDB, sid: str) -> float | None:
    async with db._tx() as conn:
        return (
            await conn.execute(text("SELECT next_fire_at FROM schedules WHERE id = :s"), {"s": sid})
        ).scalar()


async def test_two_schedulers_racing_one_due_instant_write_one_occurrence(db: StateDB):
    """Selecting a due row and firing it are separate statements, so the cursor is the claim."""
    due = time.time() - 1
    sid = await _schedule(db, due=due)
    advanced = {"last_fired_at": due, "next_fire_at": due + 60}

    async def fire():
        return await db.create_schedule_run_and_advance(
            _occurrence(sid, due),
            schedule_id=sid,
            schedule_fields=dict(advanced),
            expect_next_fire_at=due,
        )

    assert sorted(await asyncio.gather(fire(), fire())) == [False, True]
    assert await _occurrence_count(db, sid) == 1


async def test_the_scheduler_that_holds_the_cursor_still_fires(db: StateDB):
    """The control: a claim that refuses everything would bound duplicates by firing nothing."""
    due = time.time() - 1
    sid = await _schedule(db, due=due)

    assert await db.create_schedule_run_and_advance(
        _occurrence(sid, due),
        schedule_id=sid,
        schedule_fields={"last_fired_at": due, "next_fire_at": due + 60},
        expect_next_fire_at=due,
    )
    assert await _occurrence_count(db, sid) == 1
    assert await _cursor(db, sid) == due + 60


async def test_a_refused_fire_leaves_no_occurrence_and_no_cursor_move(db: StateDB):
    """A partial write is worse than a lost race: the loser must write nothing at all."""
    due = time.time() - 1
    sid = await _schedule(db, due=due)
    await db.update_schedule(sid, next_fire_at=due + 60)

    assert not await db.create_schedule_run_and_advance(
        _occurrence(sid, due),
        schedule_id=sid,
        schedule_fields={"last_fired_at": due, "next_fire_at": due + 120},
        expect_next_fire_at=due,
    )
    assert await _occurrence_count(db, sid) == 0
    assert await _cursor(db, sid) == due + 60


async def test_a_schedule_with_no_cursor_is_claimed_on_the_same_terms(db: StateDB):
    """NULL is a cursor value here, so the predicate has to compare NULL to NULL."""
    sid = await _schedule(db, due=None)
    now = time.time()

    assert await db.create_schedule_run_and_advance(
        _occurrence(sid, now),
        schedule_id=sid,
        schedule_fields={"last_fired_at": now, "next_fire_at": now + 60},
        expect_next_fire_at=None,
    )
    assert not await db.create_schedule_run_and_advance(
        _occurrence(sid, now),
        schedule_id=sid,
        schedule_fields={"last_fired_at": now, "next_fire_at": now + 120},
        expect_next_fire_at=None,
    )
    assert await _occurrence_count(db, sid) == 1


async def test_an_operator_edit_between_selection_and_fire_refuses_the_fire(db: StateDB):
    """The claim is not only about a second scheduler: any writer moving the cursor wins it."""
    due = time.time() - 1
    sid = await _schedule(db, due=due)
    await db.update_schedule(sid, next_fire_at=due + 3600)

    assert not await db.create_schedule_run_and_advance(
        _occurrence(sid, due),
        schedule_id=sid,
        schedule_fields={"last_fired_at": due, "next_fire_at": due + 60},
        expect_next_fire_at=due,
    )
    assert await _occurrence_count(db, sid) == 0
    assert await _cursor(db, sid) == due + 3600


async def test_update_schedule_still_writes_without_a_cursor_predicate(db: StateDB):
    """The predicate is opt-in on one shared statement builder, so the other path must not gain it."""
    due = time.time() - 1
    sid = await _schedule(db, due=due)

    await db.update_schedule(sid, next_fire_at=due + 5)
    assert await _cursor(db, sid) == due + 5


def test_the_claim_predicate_is_valid_on_every_supported_dialect():
    """`IS` with a bound parameter parses on sqlite and is a syntax error on postgres.

    The dual-backend suite skips without asyncpg, so a postgres-invalid predicate reaches
    CI unnoticed. Reading the generated text is what closes that.
    """
    with_value, params = StateDB._build_update_schedule_stmt(
        "sched-1", {"next_fire_at": 200.0}, expect_next_fire_at=100.0
    )
    with_null, null_params = StateDB._build_update_schedule_stmt(
        "sched-1", {"next_fire_at": 200.0}, expect_next_fire_at=None
    )
    unclaimed, _ = StateDB._build_update_schedule_stmt("sched-1", {"next_fire_at": 200.0})

    for stmt in (with_value, with_null):
        assert " IS :" not in str(stmt), f"postgres rejects a bound parameter after IS: {stmt}"

    assert "next_fire_at = :_expect_nfa" in str(with_value)
    assert params["_expect_nfa"] == 100.0
    assert "next_fire_at IS NULL" in str(with_null)
    assert "_expect_nfa" not in null_params
    # Control: without a claim there is no predicate at all, so the two arms above are
    # measuring a predicate that this builder only emits when asked for one.
    assert "next_fire_at IS NULL" not in str(unclaimed)
    assert "_expect_nfa" not in str(unclaimed)


@pytest.mark.asyncio
async def test_a_claim_of_none_matches_only_a_null_cursor(db: StateDB):
    """The NULL arm has to behave like a claim, not like an unclaimed write."""
    sid = await _schedule(db, due=500.0)
    assert not await db.update_schedule(sid, expect_next_fire_at=None, next_fire_at=900.0)
    assert (await db.get_schedule(sid))["next_fire_at"] == 500.0
    assert await db.update_schedule(sid, expect_next_fire_at=500.0, next_fire_at=None)
    assert (await db.get_schedule(sid))["next_fire_at"] is None
    assert await db.update_schedule(sid, expect_next_fire_at=None, next_fire_at=900.0)
    assert (await db.get_schedule(sid))["next_fire_at"] == 900.0


def test_the_poll_cursor_claim_uses_the_same_predicate_shape():
    """A second claim column has to get the same NULL handling as the first.

    Every event of one poll batch resolves to the same next_fire_at, so a claim on that
    value matches twice and separates nothing. github_cursor advances per event, and a
    schedule polling for the first time has none, so the NULL arm is the common case here
    rather than an edge.
    """
    with_value, params = StateDB._build_update_schedule_stmt(
        "sched-1", {"github_cursor": "b"}, expect_github_cursor="a"
    )
    with_null, null_params = StateDB._build_update_schedule_stmt(
        "sched-1", {"github_cursor": "b"}, expect_github_cursor=None
    )
    both, both_params = StateDB._build_update_schedule_stmt(
        "sched-1",
        {"github_cursor": "b"},
        expect_next_fire_at=100.0,
        expect_github_cursor="a",
    )
    unclaimed, unclaimed_params = StateDB._build_update_schedule_stmt(
        "sched-1", {"github_cursor": "b"}
    )

    for stmt in (with_value, with_null, both):
        assert " IS :" not in str(stmt), f"postgres rejects a bound parameter after IS: {stmt}"

    assert "github_cursor = :_expect_ghc" in str(with_value)
    assert params["_expect_ghc"] == "a"
    assert "github_cursor IS NULL" in str(with_null)
    assert "_expect_ghc" not in null_params
    # Both claims are independent conditions on one statement, not one overwriting the other.
    assert "next_fire_at = :_expect_nfa" in str(both)
    assert "github_cursor = :_expect_ghc" in str(both)
    assert both_params["_expect_nfa"] == 100.0
    assert both_params["_expect_ghc"] == "a"
    # Control: unasked, neither predicate appears, so the arms above measure something the
    # builder emits only on request rather than a condition it always writes.
    assert "_expect_ghc" not in str(unclaimed)
    assert "_expect_ghc" not in unclaimed_params
    assert "_expect_nfa" not in str(unclaimed)
