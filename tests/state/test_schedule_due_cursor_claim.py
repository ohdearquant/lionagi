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
