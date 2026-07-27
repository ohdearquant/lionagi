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


# ── The shared JSON-bind serializer ──────────────────────────────────────────


@pytest.mark.parametrize("bad", NON_FINITE)
def test_engine_json_serializer_rejects_non_finite(bad):
    with pytest.raises(ValueError):
        _dumps_with_uuid({"score": bad})


def test_engine_json_serializer_rejects_nested_non_finite():
    with pytest.raises(ValueError):
        _dumps_with_uuid({"outer": [{"inner": float("nan")}]})


def test_engine_json_serializer_keeps_ordinary_values():
    assert _dumps_with_uuid({"a": 1.5, "b": None, "c": "x"}) == '{"a": 1.5, "b": null, "c": "x"}'


# ── insert_artifact: rejected before a row exists ────────────────────────────


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


# ── Ordinary content, including a genuine null, still writes ─────────────────


async def test_ordinary_content_with_a_genuine_null_round_trips(db):
    content = {"score": 0.5, "note": None, "items": [1, None, "two"], "big": 1e308}
    art_id = await db.insert_artifact(kind="review", name="ok", content=content)
    stored = (await db.get_artifact(art_id))["content"]
    if isinstance(stored, str):
        stored = json.loads(stored)
    assert stored == content
    assert stored["note"] is None
    assert math.isfinite(stored["big"])


# ── The TEXT-column JSON write goes through the checked helper ───────────────


async def test_progression_collection_writes_through_the_checked_helper(db):
    await db.create_progression("prog-ok", ["m1", "m2"])
    assert await db.get_progression("prog-ok") == ["m1", "m2"]


def test_to_json_column_rejects_non_finite():
    with pytest.raises(ValueError):
        _to_json_column({"score": float("inf")})
