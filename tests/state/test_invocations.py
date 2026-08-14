# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0077 invocations table tests.

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


# Vocabulary


def test_invocation_status_vocabulary_matches_adr0025():
    """Invocations share the ADR-0057 terminal set (now seven values, with
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


# CRUD


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
    with pytest.raises(ValueError, match="ADR-0057"):
        await db.update_invocation(inv["id"], status="stale")


async def test_update_invocation_rejects_unknown_column(db: StateDB):
    inv = await _make_invocation(db)
    with pytest.raises(ValueError, match="Invalid column"):
        await db.update_invocation(inv["id"], not_a_column="x")


# Session linkage


async def test_create_session_with_invocation_bumps_count(db: StateDB):
    """invocations.session_count tracks attached sessions
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


# attach_session_invocation: resume backfill


async def test_attach_session_invocation_links_an_unlinked_session(db: StateDB):
    """A session created with no invocation_id (or a resume reopening a row
    created before this leg's invocation existed) can be backfilled onto one,
    the same way create_session links a brand-new row."""
    inv = await _make_invocation(db)
    session = await _make_session(db, invocation_id=None, status="running")

    await db.attach_session_invocation(session["id"], inv["id"])

    assert (await db.get_session(session["id"]))["invocation_id"] == inv["id"]
    assert (await db.get_invocation(inv["id"]))["session_count"] == 1
    rows = await db.list_sessions_for_invocation(inv["id"])
    assert [r["id"] for r in rows] == [session["id"]]


async def test_attach_session_invocation_relinks_a_resumed_session(db: StateDB):
    """The resume case: a session already attributed to the invocation that
    originally created it is re-pointed at the invocation that resumed it,
    so the resume's own invocation record can find the session it drove —
    and the invocation it left behind stops claiming a session it no longer
    has, so the lifecycle reaper's session_count == 0 check still fires."""
    original = await _make_invocation(db)
    resume = await _make_invocation(db)
    session = await _make_session(db, invocation_id=original["id"], status="running")

    await db.attach_session_invocation(session["id"], resume["id"])

    assert (await db.get_session(session["id"]))["invocation_id"] == resume["id"]
    assert (await db.get_invocation(resume["id"]))["session_count"] == 1
    assert [r["id"] for r in await db.list_sessions_for_invocation(resume["id"])] == [session["id"]]

    assert (await db.get_invocation(original["id"]))["session_count"] == 0
    assert await db.list_sessions_for_invocation(original["id"]) == []


async def test_attach_session_invocation_chain_resume_only_decrements_the_immediate_prior(
    db: StateDB,
):
    """A session resumed twice — old -> mid -> new. The mid invocation is
    itself vacated when the session leaves it for new, so it must land back
    at zero rather than staying inflated because it was never the FIRST
    invocation in the chain."""
    old = await _make_invocation(db)
    mid = await _make_invocation(db)
    new = await _make_invocation(db)
    session = await _make_session(db, invocation_id=old["id"], status="running")

    await db.attach_session_invocation(session["id"], mid["id"])
    await db.attach_session_invocation(session["id"], new["id"])

    assert (await db.get_invocation(old["id"]))["session_count"] == 0
    assert (await db.get_invocation(mid["id"]))["session_count"] == 0
    assert (await db.get_invocation(new["id"]))["session_count"] == 1
    assert await db.list_sessions_for_invocation(mid["id"]) == []
    assert [r["id"] for r in await db.list_sessions_for_invocation(new["id"])] == [session["id"]]


async def test_attach_session_invocation_is_a_noop_when_already_current(db: StateDB):
    """Calling it again with the same invocation_id must not double-count —
    the same idempotence create_session's ON CONFLICT DO NOTHING gives a
    brand-new row."""
    inv = await _make_invocation(db)
    session = await _make_session(db, invocation_id=inv["id"], status="running")

    await db.attach_session_invocation(session["id"], inv["id"])
    await db.attach_session_invocation(session["id"], inv["id"])

    assert (await db.get_invocation(inv["id"]))["session_count"] == 1


# List + filter


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


async def test_list_invocations_filters_by_plugin(db: StateDB):
    await _make_invocation(db, plugin="review-toolkit")
    await _make_invocation(db, plugin="other-plugin")
    await _make_invocation(db)  # no plugin — standalone skill invocation

    only_review_toolkit = await db.list_invocations(plugin="review-toolkit")
    assert len(only_review_toolkit) == 1
    assert only_review_toolkit[0]["plugin"] == "review-toolkit"


# Count (real total, not a page count)


async def test_count_invocations_matches_row_count_below_the_page_limit(db: StateDB):
    for _ in range(3):
        await _make_invocation(db, skill="show")
    await _make_invocation(db, skill="other")

    assert await db.count_invocations(skill="show") == 3
    assert await db.count_invocations() == 4


async def test_count_invocations_exceeds_a_page_limit_smaller_than_the_real_total(db: StateDB):
    """The count must not silently plateau at a page size the way counting
    len(list_invocations(...)) does once more rows exist than `limit`."""
    for _ in range(5):
        await _make_invocation(db, skill="show")

    page = await db.list_invocations(skill="show", limit=2)
    assert len(page) == 2

    total = await db.count_invocations(skill="show")
    assert total == 5
    assert total != len(page)


async def test_count_invocations_filters_by_plugin_and_status(db: StateDB):
    a = await _make_invocation(db, plugin="review-toolkit")
    await _make_invocation(db, plugin="review-toolkit")
    await _make_invocation(db, plugin="other-plugin")
    await db.update_invocation(a["id"], status="completed", ended_at=time.time())

    assert await db.count_invocations(plugin="review-toolkit") == 2
    assert await db.count_invocations(plugin="review-toolkit", status="completed") == 1
    assert await db.count_invocations(plugin="does-not-exist") == 0
