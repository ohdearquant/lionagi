# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0057 admin event log tests."""

from __future__ import annotations

import pytest

from lionagi.state.db import StateDB


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


async def test_insert_and_list_admin_event(db: StateDB):
    eid = await db.insert_admin_event(
        action="transition",
        target_id="abc",
        actor="admin",
        details={"target_status": "failed", "reason": "manual cleanup"},
    )
    assert isinstance(eid, str) and len(eid) == 12

    events = await db.list_admin_events()
    assert len(events) == 1
    e = events[0]
    assert e["action"] == "transition"
    assert e["target_id"] == "abc"
    assert e["actor"] == "admin"


async def test_list_filters_by_action(db: StateDB):
    await db.insert_admin_event(action="transition", details={})
    await db.insert_admin_event(action="prune", details={})
    await db.insert_admin_event(action="checkpoint", details={})

    transitions = await db.list_admin_events(action="transition")
    assert len(transitions) == 1
    assert transitions[0]["action"] == "transition"


async def test_list_filters_by_target_id(db: StateDB):
    await db.insert_admin_event(action="classify", target_id="s1", details={})
    await db.insert_admin_event(action="classify", target_id="s2", details={})

    only_s1 = await db.list_admin_events(target_id="s1")
    assert {r["target_id"] for r in only_s1} == {"s1"}


async def test_events_returned_newest_first(db: StateDB):
    import asyncio

    a = await db.insert_admin_event(action="a", details={})
    await asyncio.sleep(0.01)
    b = await db.insert_admin_event(action="b", details={})

    events = await db.list_admin_events()
    assert [e["id"] for e in events][0] == b
    assert [e["id"] for e in events][1] == a
