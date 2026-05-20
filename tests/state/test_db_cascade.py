# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""
FK cascade behavior regressions for ``StateDB``.

The schema (``lionagi/state/schema.sql``) declares two ``ON DELETE
CASCADE`` relationships:

* ``branches.session_id REFERENCES sessions(id) ON DELETE CASCADE``
* ``plays.show_id REFERENCES shows(id) ON DELETE CASCADE``

Other FKs (``sessions.progression_id``, ``branches.progression_id``,
``branches.system_msg_id``, ``messages.lion_class``,
``sessions.first_msg_id`` / ``last_msg_id``, ``plays.session_id``) are
deliberately NOT cascaded — they reference shared content that may
outlive its owner.

These tests pin both groups: what cascades, what doesn't, and that
``PRAGMA foreign_keys = ON`` (set by ``_apply_pragmas``) is actually
in effect.
"""

from __future__ import annotations

import time
import uuid

import pytest

from lionagi.state.db import StateDB


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


# ── PRAGMA proof ─────────────────────────────────────────────────────────────


async def test_pragma_foreign_keys_is_on(db: StateDB):
    """If this fails, every cascade test below also fails — sanity guard
    that ``_apply_pragmas`` actually enforced foreign_keys.
    """
    cur = await db.db.execute("PRAGMA foreign_keys")
    row = await cur.fetchone()
    assert row[0] == 1


# ── Session → Branch cascade ─────────────────────────────────────────────────


async def test_delete_session_cascades_branches(db: StateDB):
    """DELETE FROM sessions WHERE id = ? drops every branch with
    matching session_id (ON DELETE CASCADE)."""
    spid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    bpid1 = str(uuid.uuid4())
    bpid2 = str(uuid.uuid4())
    bid1 = str(uuid.uuid4())
    bid2 = str(uuid.uuid4())

    await db.create_progression(spid)
    await db.create_progression(bpid1)
    await db.create_progression(bpid2)
    await db.create_session({
        "id": sid, "progression_id": spid, "status": "completed",
    })
    await db.create_branch({
        "id": bid1, "session_id": sid, "progression_id": bpid1,
    })
    await db.create_branch({
        "id": bid2, "session_id": sid, "progression_id": bpid2,
    })

    # Two branches before delete.
    cur = await db.db.execute(
        "SELECT COUNT(*) AS n FROM branches WHERE session_id = ?", (sid,),
    )
    assert (await cur.fetchone())["n"] == 2

    await db.db.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    await db.db.commit()

    # Zero branches after.
    cur = await db.db.execute(
        "SELECT COUNT(*) AS n FROM branches WHERE session_id = ?", (sid,),
    )
    assert (await cur.fetchone())["n"] == 0


async def test_delete_session_does_not_cascade_progression(db: StateDB):
    """``sessions.progression_id`` has NO cascade — the progression
    row survives the session delete. This is intentional (R5-D notes
    the leftover as a known gap), but it's a load-bearing detail for
    the prune sweep semantics, so pin it here.
    """
    spid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    await db.create_progression(spid)
    await db.create_session({"id": sid, "progression_id": spid})

    await db.db.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    await db.db.commit()

    cur = await db.db.execute(
        "SELECT COUNT(*) AS n FROM progressions WHERE id = ?", (spid,),
    )
    assert (await cur.fetchone())["n"] == 1


# ── Show → Play cascade ──────────────────────────────────────────────────────


async def test_delete_show_cascades_plays(db: StateDB):
    """DELETE FROM shows WHERE id = ? drops every play with matching
    show_id (ON DELETE CASCADE).
    """
    show_id = str(uuid.uuid4())
    await db.create_show({
        "id": show_id, "topic": f"t-{show_id}",
        "show_dir": f"shows/{show_id}", "status": "active",
    })
    play_ids = [str(uuid.uuid4()) for _ in range(3)]
    for i, pid in enumerate(play_ids):
        await db.create_play({
            "id": pid, "show_id": show_id, "name": f"p{i}",
            "status": "pending",
        })

    cur = await db.db.execute(
        "SELECT COUNT(*) AS n FROM plays WHERE show_id = ?", (show_id,),
    )
    assert (await cur.fetchone())["n"] == 3

    await db.db.execute("DELETE FROM shows WHERE id = ?", (show_id,))
    await db.db.commit()

    cur = await db.db.execute(
        "SELECT COUNT(*) AS n FROM plays WHERE show_id = ?", (show_id,),
    )
    assert (await cur.fetchone())["n"] == 0


async def test_delete_show_with_no_plays_succeeds(db: StateDB):
    """Empty-cascade case — should not error or affect other rows."""
    show_id = str(uuid.uuid4())
    await db.create_show({
        "id": show_id, "topic": f"t-{show_id}",
        "show_dir": f"shows/{show_id}",
    })

    await db.db.execute("DELETE FROM shows WHERE id = ?", (show_id,))
    await db.db.commit()

    cur = await db.db.execute(
        "SELECT COUNT(*) AS n FROM shows WHERE id = ?", (show_id,),
    )
    assert (await cur.fetchone())["n"] == 0


# ── Play.session_id is NOT cascaded ─────────────────────────────────────────


async def test_delete_session_referenced_by_play_is_rejected(db: StateDB):
    """``plays.session_id`` has no ``ON DELETE CASCADE`` and no
    ``ON DELETE SET NULL``. With ``PRAGMA foreign_keys = ON``, SQLite
    REJECTS a DELETE of a session that a play still references —
    preventing dangling pointers in play history (ADR-0012).
    """
    import aiosqlite

    show_id = str(uuid.uuid4())
    await db.create_show({
        "id": show_id, "topic": f"t-{show_id}",
        "show_dir": f"shows/{show_id}",
    })

    spid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    await db.create_progression(spid)
    await db.create_session({"id": sid, "progression_id": spid})

    play_id = str(uuid.uuid4())
    await db.create_play({
        "id": play_id, "show_id": show_id, "name": "p",
        "session_id": sid, "status": "pending",
    })

    with pytest.raises(aiosqlite.IntegrityError):
        await db.db.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        await db.db.commit()


# ── Branch.system_msg_id is NOT cascaded ─────────────────────────────────────


async def test_delete_message_does_not_cascade_to_branch_system_msg_id(
    db: StateDB,
):
    """``branches.system_msg_id`` has NO ``ON DELETE CASCADE``.
    Deleting the underlying message would orphan the branch's pointer
    — we verify SQLite REJECTS the delete via FK enforcement instead.
    """
    import aiosqlite

    msg_id = str(uuid.uuid4())
    await db.insert_message({
        "id": msg_id, "created_at": time.time(),
        "node_metadata": {}, "content": {"text": "sys"},
        "role": "system", "sender": "s",
    })
    spid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    bpid = str(uuid.uuid4())
    bid = str(uuid.uuid4())
    await db.create_progression(spid)
    await db.create_progression(bpid)
    await db.create_session({"id": sid, "progression_id": spid})
    await db.create_branch({
        "id": bid, "session_id": sid, "progression_id": bpid,
        "system_msg_id": msg_id,
    })

    # PRAGMA foreign_keys = ON: deleting a referenced message must
    # raise IntegrityError (not silently break the pointer).
    with pytest.raises(aiosqlite.IntegrityError):
        await db.db.execute("DELETE FROM messages WHERE id = ?", (msg_id,))
        await db.db.commit()
