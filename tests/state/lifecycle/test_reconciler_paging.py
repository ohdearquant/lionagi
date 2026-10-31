# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A pass reads pages until the traversal wraps or its budget is spent; ``start`` sets the baseline."""

from __future__ import annotations

import math
import uuid

import pytest

from lionagi.state.db import StateDB
from lionagi.state.lifecycle.deliveries import is_acknowledged
from lionagi.state.lifecycle.reconciler import DeliveryOutcome, TerminalReconciler


async def _seed(db: StateDB) -> str:
    sid, pid = str(uuid.uuid4()), str(uuid.uuid4())
    await db.create_progression(pid)
    await db.create_session({"id": sid, "progression_id": pid, "status": "running"})
    assert await db.update_status(
        "session",
        sid,
        new_status="failed",
        reason_code="session.stale.no_heartbeat",
        source="system",
        actor="test",
    )
    row = await db.fetch_one(
        "SELECT id FROM status_transitions WHERE entity_type = ? AND entity_id = ? AND status = ?",
        ("session", sid, "failed"),
    )
    return row["id"]


async def _seed_ordered(db: StateDB, count: int) -> list[str]:
    ids = [await _seed(db) for _ in range(count)]
    for timestamp, transition_id in enumerate(ids, start=1):
        await db.execute(
            "UPDATE status_transitions SET created_at = ? WHERE id = ?",
            (timestamp, transition_id),
        )
    return ids


@pytest.mark.asyncio
async def test_default_budget_attempts_one_page_per_pass(tmp_path):
    seen = []

    async def prepare(row, _db):
        seen.append(row["transition_id"])
        return DeliveryOutcome.SUCCESS

    async with StateDB(tmp_path / "state.db") as db:
        ids = await _seed_ordered(db, 5)
        reconciler = TerminalReconciler("one-page", prepare, batch_size=2)
        result = await reconciler.run_pass(db)
        assert (result["pages"], result["attempted"], result["acknowledged"]) == (1, 2, 2)
        assert seen == ids[:2]


@pytest.mark.asyncio
async def test_budgeted_pass_reads_pages_until_the_traversal_wraps(tmp_path):
    seen = []

    async def prepare(row, _db):
        seen.append(row["transition_id"])
        return DeliveryOutcome.SUCCESS

    async with StateDB(tmp_path / "state.db") as db:
        ids = await _seed_ordered(db, 5)
        clock = [0.0]
        reconciler = TerminalReconciler(
            "until-wrap", prepare, batch_size=2, pass_budget=60.0, clock=lambda: clock[0]
        )
        result = await reconciler.run_pass(db)
        assert (result["pages"], result["attempted"], result["acknowledged"]) == (3, 5, 5)
        assert seen == ids
        assert reconciler._cursor is None
        for transition_id in ids:
            assert await is_acknowledged(db, transition_id, "until-wrap")
        idle = await reconciler.run_pass(db)
        assert (idle["pages"], idle["attempted"]) == (0, 0)


@pytest.mark.parametrize(
    ("budget", "pages"),
    [(5.0, 1), (7.0, 2)],
    ids=["spent-after-first-page", "one-page-past-the-budget"],
)
@pytest.mark.asyncio
async def test_budget_is_checked_between_pages(tmp_path, budget, pages):
    seen = []
    clock = [0.0]

    async def prepare(row, _db):
        seen.append(row["transition_id"])
        clock[0] += 3.0
        return DeliveryOutcome.SUCCESS

    async with StateDB(tmp_path / "state.db") as db:
        ids = await _seed_ordered(db, 6)
        reconciler = TerminalReconciler(
            "budget", prepare, batch_size=2, pass_budget=budget, clock=lambda: clock[0]
        )
        result = await reconciler.run_pass(db)
        assert (result["pages"], result["attempted"]) == (pages, 2 * pages)
        assert seen == ids[: 2 * pages]
        # The next pass resumes after the cursor rather than restarting the traversal.
        await reconciler.run_pass(db)
        assert seen[2 * pages : 2 * pages + 2] == ids[2 * pages : 2 * pages + 2]


@pytest.mark.asyncio
async def test_stop_inside_a_page_ends_the_pass(tmp_path):
    seen = []

    async def prepare(row, db):
        seen.append(row["transition_id"])
        if len(seen) == 1:
            from lionagi.state.lifecycle.deliveries import retire_consumer

            assert await retire_consumer(db, "stops")
        return DeliveryOutcome.SUCCESS

    async with StateDB(tmp_path / "state.db") as db:
        await _seed_ordered(db, 5)
        reconciler = TerminalReconciler("stops", prepare, batch_size=2, pass_budget=60.0)
        result = await reconciler.run_pass(db)
        assert (result["pages"], result["attempted"], len(seen)) == (1, 0, 1)


@pytest.mark.asyncio
async def test_start_now_acknowledges_history_and_owes_later_rows(tmp_path):
    seen = []

    async def prepare(row, _db):
        seen.append(row["transition_id"])
        return DeliveryOutcome.SUCCESS

    async with StateDB(tmp_path / "state.db") as db:
        history = await _seed_ordered(db, 3)
        reconciler = TerminalReconciler("baseline", prepare, start="now", pass_budget=60.0)
        first = await reconciler.run_pass(db)
        assert first["attempted"] == 0 and seen == []
        for transition_id in history:
            assert await is_acknowledged(db, transition_id, "baseline")
        later = await _seed(db)
        # Registration is already active, so the second pass's start="now" acknowledges nothing.
        second = await reconciler.run_pass(db)
        assert (second["attempted"], second["acknowledged"]) == (1, 1)
        assert seen == [later]


@pytest.mark.asyncio
async def test_start_all_owes_history(tmp_path):
    async def prepare(_row, _db):
        return DeliveryOutcome.SUCCESS

    async with StateDB(tmp_path / "state.db") as db:
        await _seed_ordered(db, 3)
        reconciler = TerminalReconciler("owed", prepare, pass_budget=60.0)
        assert (await reconciler.run_pass(db))["attempted"] == 3


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start": "later"},
        {"pass_budget": -1.0},
        {"pass_budget": math.nan},
        {"pass_budget": math.inf},
    ],
    ids=["unknown-start", "negative-budget", "nan-budget", "infinite-budget"],
)
def test_invalid_start_or_budget_is_refused(kwargs):
    async def prepare(_row, _db):
        return DeliveryOutcome.SUCCESS

    with pytest.raises(ValueError):
        TerminalReconciler("refused", prepare, **kwargs)
