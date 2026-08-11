# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Non-finite floats must not reach a JSON column — the guard sits on the engine's
JSON serializer, so it covers every JSON bind rather than one write method."""

from __future__ import annotations

import json
import math

import pytest
from sqlalchemy import text
from sqlalchemy.exc import StatementError

from lionagi.state.db import StateDB, _to_json_column
from lionagi.state.engine import _dumps_with_uuid

NON_FINITE = [float("inf"), float("-inf"), float("nan")]


@pytest.fixture
async def db(tmp_path):
    state = StateDB(tmp_path / "state.db")
    await state.open()
    yield state
    await state.close()


async def _artifact_count(db: StateDB) -> int:
    async with db._read() as conn:
        row = (await conn.execute(text("SELECT COUNT(*) AS n FROM artifacts"))).mappings().first()
    return row["n"]


# The shared JSON-bind serializer


@pytest.mark.parametrize("bad", NON_FINITE)
def test_engine_json_serializer_rejects_non_finite(bad):
    with pytest.raises(ValueError):
        _dumps_with_uuid({"score": bad})


def test_engine_json_serializer_rejects_nested_non_finite():
    with pytest.raises(ValueError):
        _dumps_with_uuid({"outer": [{"inner": float("nan")}]})


def test_engine_json_serializer_keeps_ordinary_values():
    assert _dumps_with_uuid({"a": 1.5, "b": None, "c": "x"}) == '{"a": 1.5, "b": null, "c": "x"}'


# insert_artifact: rejected before a row exists


@pytest.mark.parametrize("bad", NON_FINITE)
async def test_insert_artifact_rejects_non_finite_without_writing(db, bad):
    before = await _artifact_count(db)
    # The bind-time refusal reaches the caller wrapped by the driver layer; the
    # value error underneath it is the guard.
    with pytest.raises(StatementError) as excinfo:
        await db.insert_artifact(kind="review", name="nonfinite", content={"score": bad})
    assert isinstance(excinfo.value.orig, ValueError)
    assert await _artifact_count(db) == before


async def test_insert_artifact_update_rejects_non_finite_without_changing_the_row(db):
    art_id = await db.insert_artifact(kind="review", name="upd", content={"score": 1.0})
    with pytest.raises(StatementError):
        await db.insert_artifact(kind="review", name="upd", content={"score": float("nan")})
    async with db._read() as conn:
        row = (
            (
                await conn.execute(
                    text("SELECT content FROM artifacts WHERE id = :id"), {"id": art_id}
                )
            )
            .mappings()
            .first()
        )
    stored = row["content"]
    if isinstance(stored, str):
        stored = json.loads(stored)
    assert stored == {"score": 1.0}


# Ordinary content, including a genuine null, still writes


async def test_ordinary_content_with_a_genuine_null_round_trips(db):
    content = {"score": 0.5, "note": None, "items": [1, None, "two"], "big": 1e308}
    art_id = await db.insert_artifact(kind="review", name="ok", content=content)
    stored = (await db.get_artifact(art_id))["content"]
    if isinstance(stored, str):
        stored = json.loads(stored)
    assert stored == content
    assert stored["note"] is None
    assert math.isfinite(stored["big"])


# The TEXT-column JSON write goes through the checked helper


async def test_progression_collection_writes_through_the_checked_helper(db):
    await db.create_progression("prog-ok", ["m1", "m2"])
    assert await db.get_progression("prog-ok") == ["m1", "m2"]


def test_to_json_column_rejects_non_finite():
    with pytest.raises(ValueError):
        _to_json_column({"score": float("inf")})


@pytest.mark.parametrize("bad", NON_FINITE)
async def test_set_progression_rejects_non_finite_leaving_the_row_unchanged(db, bad):
    await db.create_progression("prog-set", ["m1"])
    with pytest.raises(ValueError):
        await db.set_progression("prog-set", ["m1", bad])
    assert await db.get_progression("prog-set") == ["m1"]


async def test_set_progression_replaces_an_ordinary_collection(db):
    await db.create_progression("prog-replace", ["m1"])
    await db.set_progression("prog-replace", ["m1", "m2", "m3"])
    assert await db.get_progression("prog-replace") == ["m1", "m2", "m3"]


# The run-import path writes its collections through the same helper
#
# `li state import` reads run files written by other processes and parses them
# with json.loads, which accepts the non-standard NaN/Infinity tokens. Nothing
# on that path may hand the driver a JSON string of its own making.


def _write_run_dir(root, *, msg_id_literal: str):
    """A one-branch run directory whose single message id is `msg_id_literal`,
    written as a raw JSON token so non-standard ones can be exercised."""
    run_dir = root / "runs" / "run-1"
    (run_dir / "branches").mkdir(parents=True)
    (run_dir / "branches" / "b1.json").write_text(
        '{"id": "b1", "messages": {"collections": '
        f'[{{"id": {msg_id_literal}, "created_at": 1.0, "role": "user", "content": {{}}}}], '
        '"progression": {"order": []}}}'
    )
    return run_dir


async def _raw_collections(db: StateDB) -> list[str]:
    async with db._read() as conn:
        rows = (await conn.execute(text("SELECT collection FROM progressions"))).mappings().all()
    return [r["collection"] for r in rows]


# A NaN id is not exercised here: SQLite stores it as NULL, so the message
# insert fails on the id column's NOT NULL constraint before any collection is
# written, and the case would pass whether or not this guard exists.
@pytest.mark.parametrize("literal", ["Infinity", "-Infinity"])
async def test_import_writes_no_non_finite_collection(db, tmp_path, literal):
    from lionagi.cli.state import _import_one_run

    run_dir = _write_run_dir(tmp_path / "home", msg_id_literal=literal)
    with pytest.raises(Exception):
        await _import_one_run(db, "run-1", run_dir, {"kind": "agent"})

    for stored in await _raw_collections(db):
        assert "NaN" not in stored
        assert "Infinity" not in stored
        json.loads(stored)  # strict: every stored collection is standard JSON


async def test_import_writes_the_session_collection_through_the_db_operation(db, tmp_path):
    """The session-wide collection is set by the DB operation rather than by an
    UPDATE the CLI builds itself — the CLI holds no JSON serializer of its own."""
    from lionagi.cli.state import _import_one_run

    calls: list[tuple[str, list[str]]] = []
    real = db.set_progression

    async def spy(progression_id, collection):
        calls.append((progression_id, list(collection)))
        await real(progression_id, collection)

    db.set_progression = spy

    run_dir = _write_run_dir(tmp_path / "home", msg_id_literal='"m1"')
    _, branches, messages = await _import_one_run(db, "run-1", run_dir, {"kind": "agent"})

    assert (branches, messages) == (1, 1)
    assert len(calls) == 1, "the session collection must go through set_progression"
    session_prog_id, collection = calls[0]
    assert collection == ["m1"]
    assert await db.get_progression(session_prog_id) == ["m1"]
