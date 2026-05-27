# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for work_items and worker_definitions persistence (ADR-0065).

All tests use in-memory SQLite (:memory:) for speed and isolation.
asyncio_mode = "auto" in pyproject.toml — no @pytest.mark.asyncio needed.
"""

from __future__ import annotations

import time
import uuid

import pytest

from lionagi.state.db import StateDB

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
    return uuid.uuid4().hex[:12]


def make_work_item(**overrides) -> dict:
    base = {
        "id": uid(),
        "worker_name": "test_worker",
        "priority": 0,
        "args": {"input": "value"},
        "depends_on": [],
    }
    base.update(overrides)
    return base


def make_worker_definition(**overrides) -> dict:
    base = {
        "name": f"worker_{uid()}",
        "description": "A test worker",
        "definition_yaml": "steps:\n  - run: echo hello\n",
    }
    base.update(overrides)
    return base


# ── Work item tests ───────────────────────────────────────────────────────────


async def test_create_and_get_work_item(db):
    item = make_work_item()
    await db.create_work_item(item)

    fetched = await db.get_work_item(item["id"])
    assert fetched is not None
    assert fetched["id"] == item["id"]
    assert fetched["worker_name"] == "test_worker"
    assert fetched["status"] == "pending"
    assert fetched["args"] == {"input": "value"}
    assert fetched["depends_on"] == []
    assert fetched["priority"] == 0
    assert fetched["created_at"] > 0
    assert fetched["updated_at"] > 0


async def test_get_work_item_not_found(db):
    result = await db.get_work_item("nonexistent_id")
    assert result is None


async def test_list_work_items_by_status(db):
    pending1 = make_work_item(id=uid(), worker_name="w1", status="pending")
    pending2 = make_work_item(id=uid(), worker_name="w2", status="pending")
    running = make_work_item(id=uid(), worker_name="w3", status="running")

    for item in (pending1, pending2, running):
        await db.create_work_item(item)

    pending_items = await db.list_work_items(status="pending")
    running_items = await db.list_work_items(status="running")

    assert len(pending_items) == 2
    assert len(running_items) == 1
    assert running_items[0]["worker_name"] == "w3"

    pending_ids = {i["id"] for i in pending_items}
    assert pending1["id"] in pending_ids
    assert pending2["id"] in pending_ids


async def test_list_work_items_by_session(db):
    # Create a real session to satisfy the FK constraint
    prog_id = uid()
    await db.create_progression(prog_id)
    session = {"id": uid(), "progression_id": prog_id}
    await db.create_session(session)
    session_id = session["id"]

    item_with_session = make_work_item(id=uid(), session_id=session_id)
    item_no_session = make_work_item(id=uid())

    await db.create_work_item(item_with_session)
    await db.create_work_item(item_no_session)

    session_items = await db.list_work_items(session_id=session_id)
    all_items = await db.list_work_items()

    assert len(session_items) == 1
    assert session_items[0]["id"] == item_with_session["id"]
    assert len(all_items) == 2


async def test_list_work_items_by_status_and_session(db):
    prog_id = uid()
    await db.create_progression(prog_id)
    session = {"id": uid(), "progression_id": prog_id}
    await db.create_session(session)
    session_id = session["id"]

    item1 = make_work_item(id=uid(), session_id=session_id, status="pending")
    item2 = make_work_item(id=uid(), session_id=session_id, status="running")
    item3 = make_work_item(id=uid(), status="pending")  # no session

    for item in (item1, item2, item3):
        await db.create_work_item(item)

    result = await db.list_work_items(session_id=session_id, status="pending")
    assert len(result) == 1
    assert result[0]["id"] == item1["id"]


async def test_update_work_item_fields(db):
    item = make_work_item()
    await db.create_work_item(item)

    await db.update_work_item(
        item["id"],
        status="running",
        started_at=time.time(),
    )

    fetched = await db.get_work_item(item["id"])
    assert fetched["status"] == "running"
    assert fetched["started_at"] is not None
    assert fetched["updated_at"] >= fetched["created_at"]


async def test_update_work_item_result(db):
    item = make_work_item()
    await db.create_work_item(item)

    result_data = {"output": "processed", "count": 42}
    await db.update_work_item(
        item["id"],
        status="completed",
        result=result_data,
        completed_at=time.time(),
    )

    fetched = await db.get_work_item(item["id"])
    assert fetched["status"] == "completed"
    assert fetched["result"] == result_data
    assert fetched["completed_at"] is not None


async def test_update_work_item_error(db):
    item = make_work_item()
    await db.create_work_item(item)

    await db.update_work_item(
        item["id"],
        status="failed",
        error="RuntimeError: something went wrong",
    )

    fetched = await db.get_work_item(item["id"])
    assert fetched["status"] == "failed"
    assert fetched["error"] == "RuntimeError: something went wrong"


async def test_priority_ordering(db):
    """Higher priority items appear first in list results."""
    low = make_work_item(id=uid(), priority=0, worker_name="low")
    high = make_work_item(id=uid(), priority=10, worker_name="high")
    mid = make_work_item(id=uid(), priority=5, worker_name="mid")

    # Insert in non-priority order
    for item in (low, mid, high):
        await db.create_work_item(item)

    items = await db.list_work_items()
    assert len(items) == 3
    # Should be ordered by priority DESC
    assert items[0]["priority"] == 10
    assert items[1]["priority"] == 5
    assert items[2]["priority"] == 0
    assert items[0]["worker_name"] == "high"
    assert items[1]["worker_name"] == "mid"
    assert items[2]["worker_name"] == "low"


# ── Worker definition tests ───────────────────────────────────────────────────


async def test_save_and_get_worker_definition(db):
    defn = make_worker_definition()
    await db.save_worker_definition(defn)

    fetched = await db.get_worker_definition(defn["name"])
    assert fetched is not None
    assert fetched["name"] == defn["name"]
    assert fetched["description"] == defn["description"]
    assert fetched["definition_yaml"] == defn["definition_yaml"]
    assert fetched["version"] == 1
    assert fetched["created_at"] > 0
    assert fetched["updated_at"] > 0


async def test_get_worker_definition_not_found(db):
    result = await db.get_worker_definition("no_such_worker")
    assert result is None


async def test_list_worker_definitions(db):
    d1 = make_worker_definition(name="alpha_worker")
    d2 = make_worker_definition(name="beta_worker")
    d3 = make_worker_definition(name="gamma_worker")

    for d in (d3, d1, d2):  # insert out-of-order
        await db.save_worker_definition(d)

    definitions = await db.list_worker_definitions()
    assert len(definitions) == 3
    # Should be ordered by name
    names = [d["name"] for d in definitions]
    assert names == sorted(names)


async def test_worker_definition_version_on_update(db):
    defn = make_worker_definition()
    await db.save_worker_definition(defn)

    first = await db.get_worker_definition(defn["name"])
    assert first["version"] == 1

    # Update with new YAML content
    updated = dict(defn)
    updated["definition_yaml"] = "steps:\n  - run: echo updated\n"
    await db.save_worker_definition(updated)

    second = await db.get_worker_definition(defn["name"])
    assert second["version"] == 2
    assert second["definition_yaml"] == "steps:\n  - run: echo updated\n"


async def test_worker_definition_version_increments_each_save(db):
    defn = make_worker_definition()

    for i in range(1, 5):
        defn_copy = dict(defn, definition_yaml=f"version: {i}\n")
        await db.save_worker_definition(defn_copy)
        fetched = await db.get_worker_definition(defn["name"])
        assert fetched["version"] == i


async def test_worker_definition_without_description(db):
    defn = make_worker_definition()
    del defn["description"]
    await db.save_worker_definition(defn)

    fetched = await db.get_worker_definition(defn["name"])
    assert fetched is not None
    assert fetched["description"] is None


async def test_work_item_default_id_generated(db):
    """create_work_item auto-generates id when not provided."""
    item = {
        "worker_name": "auto_id_worker",
        "args": {},
    }
    await db.create_work_item(item)

    all_items = await db.list_work_items()
    assert len(all_items) == 1
    assert all_items[0]["worker_name"] == "auto_id_worker"
    # id was auto-generated — just verify it is a non-empty string
    assert isinstance(all_items[0]["id"], str)
    assert len(all_items[0]["id"]) > 0
