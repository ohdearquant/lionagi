# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""StateDB.insert_messages: one transaction; a string-id progression is rewritten at most once."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, text

from lionagi.state.db import StateDB


def uid() -> str:
    return str(uuid.uuid4())


@pytest_asyncio.fixture
async def db(tmp_path: Path):
    state = StateDB(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    async with state:
        yield state


def _msg(mid: str, t: float = 1.0) -> dict:
    return {
        "id": mid,
        "created_at": t,
        "role": "assistant",
        "content": {"assistant_response": mid},
        "node_metadata": {"lion_class": "lionagi.protocols.messages.AssistantResponse"},
    }


def _progression_updates(db: StateDB) -> list[str]:
    """Record every UPDATE of the progressions table issued on db's engine."""
    seen: list[str] = []

    def _spy(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE PROGRESSIONS"):
            seen.append(statement)

    event.listen(db._engine.sync_engine, "before_cursor_execute", _spy)
    return seen


async def test_a_batch_appends_in_order_with_one_rewrite_per_progression(db: StateDB):
    a, b, first = uid(), uid(), uid()
    await db.create_progression(a, [first])
    await db.create_progression(b)
    ids = [uid() for _ in range(5)]
    updates = _progression_updates(db)

    await db.insert_messages([_msg(i) for i in ids], progression_ids=(a, b))

    assert await db.get_progression(a) == [first, *ids]
    assert await db.get_progression(b) == ids
    assert len(updates) == 2
    for i in ids:
        assert await db.get_message(i) is not None


async def test_ids_already_present_or_repeated_in_the_batch_are_skipped(db: StateDB):
    p, old, new = uid(), uid(), uid()
    await db.create_progression(p, [old])

    await db.insert_messages([_msg(old), _msg(new), _msg(new)], progression_ids=(p,))

    assert await db.get_progression(p) == [old, new]


async def test_rerunning_a_batch_rewrites_no_progression(db: StateDB):
    p = uid()
    await db.create_progression(p)
    batch = [_msg(uid()) for _ in range(3)]
    await db.insert_messages(batch, progression_ids=(p,))
    before = await db.get_progression(p)
    updates = _progression_updates(db)

    await db.insert_messages(batch, progression_ids=(p,))

    assert await db.get_progression(p) == before
    assert updates == []


async def test_a_missing_progression_is_left_alone(db: StateDB):
    missing, mid = uid(), uid()

    await db.insert_messages([_msg(mid)], progression_ids=(missing,))

    assert await db.get_progression(missing) == []
    assert await db.get_message(mid) is not None


async def test_same_result_as_appending_one_message_at_a_time(db: StateDB):
    one, batched = uid(), uid()
    seed = [uid(), uid()]
    await db.create_progression(one, seed)
    await db.create_progression(batched, seed)
    sequence = [seed[1], uid(), uid(), seed[0], uid()]
    sequence.append(sequence[2])

    for mid in sequence:
        await db.append_to_progression(one, mid)
    await db.insert_messages([_msg(m) for m in sequence], progression_ids=(batched,))

    assert await db.get_progression(batched) == await db.get_progression(one)


async def _stored(db: StateDB, pid: str):
    # The stored text, compared as written: a decoded NaN never equals another.
    async with db._tx() as conn:
        result = await conn.execute(
            text("SELECT collection FROM progressions WHERE id = :id"), {"id": pid}
        )
        return result.scalar()


async def _outcome(call) -> str | None:
    try:
        await call()
    except Exception as exc:
        return type(exc).__name__
    return None


@pytest.mark.parametrize(
    "stored",
    [
        '"[\\"m1\\"]"',
        '{"m1": 1}',
        "5",
        "null",
        "NaN",
        "[NaN]",
        "[1e400]",
        "[5]",
        '["m0", 5]',
        '["\\ud800"]',
        "[",
    ],
    ids=[
        "json-string",
        "object",
        "number",
        "null",
        "bare-nan",
        "nan-element",
        "overflow-element",
        "number-element",
        "mixed-elements",
        "lone-surrogate-element",
        "malformed",
    ],
)
async def test_any_other_stored_shape_ends_as_appending_one_at_a_time(db: StateDB, stored: str):
    one, batched = uid(), uid()
    for pid in (one, batched):
        await db.create_progression(pid)
        async with db._tx() as conn:
            await conn.execute(
                text("UPDATE progressions SET collection = :c WHERE id = :id"),
                {"c": stored, "id": pid},
            )
    ids = [uid(), uid()]

    async def one_at_a_time():
        for mid in ids:
            await db.append_to_progression(one, mid)

    expected = await _outcome(one_at_a_time)
    got = await _outcome(
        lambda: db.insert_messages([_msg(m) for m in ids], progression_ids=(batched,))
    )

    assert got == expected
    assert await _stored(db, batched) == await _stored(db, one)
    if expected is None:
        assert all([await db.get_message(m) is not None for m in ids])


@pytest.mark.parametrize("seed", [[], ["5"]], ids=["empty", "string-spelled-alike"])
async def test_a_non_string_id_ends_as_appending_one_at_a_time(db: StateDB, seed: list):
    one, batched = uid(), uid()
    await db.create_progression(one, seed)
    await db.create_progression(batched, seed)

    with pytest.raises(ValueError, match=one):
        await db.append_to_progression(one, 5)
    assert await db.get_progression(one) == seed

    # Batch inserts retain the legacy SQL append behavior for non-string IDs;
    # the public single-ID append now rejects such input before writing.
    for _ in range(2):
        async with db._tx() as conn:
            await conn.execute(text(db._progression_append_sql(db.dialect)), {"v": 5, "id": one})
        await db.insert_messages([_msg(5)], progression_ids=(batched,))

    assert await _stored(db, batched) == await _stored(db, one)


async def test_a_rejected_message_writes_nothing(db: StateDB):
    p = uid()
    await db.create_progression(p)
    good = _msg(uid())
    bad = {**_msg(uid()), "content": None}

    with pytest.raises(ValueError, match="content is NOT NULL"):
        await db.insert_messages([good, bad], progression_ids=(p,))

    assert await db.get_progression(p) == []
    assert await db.get_message(good["id"]) is None


async def test_a_failure_inside_the_transaction_commits_no_message(db: StateDB):
    p = uid()
    await db.create_progression(p)
    ids = [uid(), uid()]

    def _fail_progression_write(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE PROGRESSIONS"):
            raise RuntimeError("progression write failed")

    event.listen(db._engine.sync_engine, "before_cursor_execute", _fail_progression_write)
    try:
        with pytest.raises(Exception, match="progression write failed"):
            await db.insert_messages([_msg(i) for i in ids], progression_ids=(p,))
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", _fail_progression_write)

    assert await db.get_progression(p) == []
    for i in ids:
        assert await db.get_message(i) is None


async def test_the_session_activity_clock_moves_in_the_same_batch(db: StateDB):
    prog, sid = uid(), uid()
    await db.create_progression(prog)
    await db.create_session(
        {
            "id": sid,
            "progression_id": prog,
            "created_at": 1.0,
            "updated_at": 1.0,
            "last_message_at": 1.0,
        }
    )

    await db.insert_messages(
        [_msg(uid())], progression_ids=(prog,), session_id=sid, activity_at=50.0
    )

    row = await db.get_session(sid)
    assert row["last_message_at"] == 50.0
    assert row["updated_at"] == 50.0


async def test_a_mirrored_batch_is_written_in_chunks_of_500(db: StateDB):
    from lionagi.state._mirror_common import write_mirrored_messages

    branch, session = uid(), uid()
    await db.create_progression(branch)
    await db.create_progression(session)
    updates = _progression_updates(db)

    rows = [_msg(uid(), t) for t in range(501)]
    await write_mirrored_messages(
        db, rows, progression_ids=(branch, session), session_id=uid(), activity_at=501.0
    )

    # Two chunks (500 and 1), each rewriting both progressions once.
    assert len(updates) == 4
    assert await db.get_progression(branch) == [row["id"] for row in rows]
    assert await db.get_progression(session) == [row["id"] for row in rows]


async def test_an_empty_batch_writes_nothing(db: StateDB):
    p = uid()
    await db.create_progression(p)
    updates = _progression_updates(db)

    await db.insert_messages([], progression_ids=(p,), session_id=uid())

    assert updates == []
    assert await db.get_progression(p) == []


async def test_get_messages_reads_in_chunks_of_500(db: StateDB):
    stored = [uid() for _ in range(3)]
    await db.insert_messages([_msg(i) for i in stored])

    reads: list[str] = []

    def _spy(conn, cursor, statement, parameters, context, executemany):
        upper = statement.lstrip().upper()
        if upper.startswith("SELECT") and "FROM MESSAGES" in upper:
            reads.append(statement)

    async def _read(wanted: list[str]) -> dict:
        reads.clear()
        event.listen(db._engine.sync_engine, "before_cursor_execute", _spy)
        try:
            return await db.get_messages(wanted)
        finally:
            event.remove(db._engine.sync_engine, "before_cursor_execute", _spy)

    # A full chunk of 500 ids fits in one query; one id past it takes a second. Both
    # sizes are pinned so the chunk bound is fixed at 500 rather than any smaller value.
    wanted = [uid() for _ in range(501)]
    for position, mid in zip((0, 499, 500), stored, strict=True):
        wanted[position] = mid

    found = await _read(wanted[:500])
    assert sorted(found) == sorted(stored[:2])
    assert len(reads) == 1

    found = await _read(wanted)
    assert sorted(found) == sorted(stored)
    assert len(reads) == 2
