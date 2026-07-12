# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""FK cascade behavior regressions for StateDB — pins what cascades (branches, plays) and what doesn't (progressions, system_msg_id)."""

from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from lionagi.state.db import StateDB


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


# ── PRAGMA proof ─────────────────────────────────────────────────────────────


async def test_pragma_foreign_keys_is_on(db: StateDB):
    """Sanity guard: if PRAGMA foreign_keys is off, every cascade test would silently pass."""
    async with db._read() as conn:
        row = (await conn.execute(text("PRAGMA foreign_keys"))).first()
    assert row[0] == 1


# ── Session → Branch cascade ─────────────────────────────────────────────────


async def test_delete_session_cascades_branches(db: StateDB):
    """Deleting a session drops all branches via ON DELETE CASCADE."""
    spid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    bpid1 = str(uuid.uuid4())
    bpid2 = str(uuid.uuid4())
    bid1 = str(uuid.uuid4())
    bid2 = str(uuid.uuid4())

    await db.create_progression(spid)
    await db.create_progression(bpid1)
    await db.create_progression(bpid2)
    await db.create_session(
        {
            "id": sid,
            "progression_id": spid,
            "status": "completed",
        }
    )
    await db.create_branch(
        {
            "id": bid1,
            "session_id": sid,
            "progression_id": bpid1,
        }
    )
    await db.create_branch(
        {
            "id": bid2,
            "session_id": sid,
            "progression_id": bpid2,
        }
    )

    async with db._read() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT COUNT(*) AS n FROM branches WHERE session_id = :sid"),
                    {"sid": sid},
                )
            )
            .mappings()
            .first()
        )
    assert row["n"] == 2

    async with db._tx() as conn:
        await conn.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": sid})

    async with db._read() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT COUNT(*) AS n FROM branches WHERE session_id = :sid"),
                    {"sid": sid},
                )
            )
            .mappings()
            .first()
        )
    assert row["n"] == 0


async def test_delete_session_does_not_cascade_progression(db: StateDB):
    """sessions.progression_id has no cascade — the progression row survives a session delete (intentional; prune sweep handles cleanup)."""
    spid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    await db.create_progression(spid)
    await db.create_session({"id": sid, "progression_id": spid})

    async with db._tx() as conn:
        await conn.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": sid})

    async with db._read() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT COUNT(*) AS n FROM progressions WHERE id = :id"),
                    {"id": spid},
                )
            )
            .mappings()
            .first()
        )
    assert row["n"] == 1


# ── Show → Play cascade ──────────────────────────────────────────────────────


async def test_delete_show_cascades_plays(db: StateDB):
    """Deleting a show drops all its plays via ON DELETE CASCADE."""
    show_id = str(uuid.uuid4())
    await db.create_show(
        {
            "id": show_id,
            "topic": f"t-{show_id}",
            "show_dir": f"shows/{show_id}",
            "status": "active",
        }
    )
    play_ids = [str(uuid.uuid4()) for _ in range(3)]
    for i, pid in enumerate(play_ids):
        await db.create_play(
            {
                "id": pid,
                "show_id": show_id,
                "name": f"p{i}",
                "status": "pending",
            }
        )

    async with db._read() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT COUNT(*) AS n FROM plays WHERE show_id = :sid"),
                    {"sid": show_id},
                )
            )
            .mappings()
            .first()
        )
    assert row["n"] == 3

    async with db._tx() as conn:
        await conn.execute(text("DELETE FROM shows WHERE id = :id"), {"id": show_id})

    async with db._read() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT COUNT(*) AS n FROM plays WHERE show_id = :sid"),
                    {"sid": show_id},
                )
            )
            .mappings()
            .first()
        )
    assert row["n"] == 0


async def test_delete_show_with_no_plays_succeeds(db: StateDB):
    """Empty-cascade case — should not error or affect other rows."""
    show_id = str(uuid.uuid4())
    await db.create_show(
        {
            "id": show_id,
            "topic": f"t-{show_id}",
            "show_dir": f"shows/{show_id}",
        }
    )

    async with db._tx() as conn:
        await conn.execute(text("DELETE FROM shows WHERE id = :id"), {"id": show_id})

    async with db._read() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT COUNT(*) AS n FROM shows WHERE id = :id"),
                    {"id": show_id},
                )
            )
            .mappings()
            .first()
        )
    assert row["n"] == 0


# ── Play.session_id is NOT cascaded ─────────────────────────────────────────


async def test_delete_session_referenced_by_play_is_rejected(db: StateDB):
    """plays.session_id has no cascade — SQLite rejects deleting a session still referenced by a play."""
    show_id = str(uuid.uuid4())
    await db.create_show(
        {
            "id": show_id,
            "topic": f"t-{show_id}",
            "show_dir": f"shows/{show_id}",
        }
    )

    spid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    await db.create_progression(spid)
    await db.create_session({"id": sid, "progression_id": spid})

    play_id = str(uuid.uuid4())
    await db.create_play(
        {
            "id": play_id,
            "show_id": show_id,
            "name": "p",
            "session_id": sid,
            "status": "pending",
        }
    )

    with pytest.raises(IntegrityError):
        async with db._tx() as conn:
            await conn.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": sid})


# ── Branch.system_msg_id is NOT cascaded ─────────────────────────────────────


async def test_delete_message_does_not_cascade_to_branch_system_msg_id(
    db: StateDB,
):
    """branches.system_msg_id has no cascade — SQLite rejects deleting a message still referenced as a system message."""
    msg_id = str(uuid.uuid4())
    await db.insert_message(
        {
            "id": msg_id,
            "created_at": time.time(),
            "node_metadata": {},
            "content": {"text": "sys"},
            "role": "system",
            "sender": "s",
        }
    )
    spid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    bpid = str(uuid.uuid4())
    bid = str(uuid.uuid4())
    await db.create_progression(spid)
    await db.create_progression(bpid)
    await db.create_session({"id": sid, "progression_id": spid})
    await db.create_branch(
        {
            "id": bid,
            "session_id": sid,
            "progression_id": bpid,
            "system_msg_id": msg_id,
        }
    )

    with pytest.raises(IntegrityError):
        async with db._tx() as conn:
            await conn.execute(text("DELETE FROM messages WHERE id = :id"), {"id": msg_id})
