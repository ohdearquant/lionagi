# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""PostgreSQL driver coverage for shared-message import preflight."""

from __future__ import annotations

import json
import os
import struct
import sys
import uuid
import warnings
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, text

from lionagi.cli import state as state_mod
from lionagi.state.db import StateDB


def _uid() -> str:
    return str(uuid.uuid4())


def _message(message_id: str, *, content: Any, metadata: Any, embedding=None) -> dict[str, Any]:
    return {
        "id": message_id,
        "created_at": 1.0,
        "content": content,
        "metadata": metadata,
        "embedding": embedding,
        "role": "user",
        "sender": "sender",
        "recipient": "recipient",
        "channel": "channel",
    }


def _write_run(
    root: Path, run_id: str, branch_ids: tuple[str, str], fresh_id: str, shared: dict[str, Any]
) -> tuple[Path, dict[str, Any]]:
    run = root / run_id
    branches = run / "branches"
    branches.mkdir(parents=True)
    manifest = {"run_id": run_id, "kind": "agent", "status": "completed"}
    (run / "run.json").write_text(json.dumps(manifest))
    fresh = _message(fresh_id, content={"text": "fresh"}, metadata=None)
    for index, (branch_id, message) in enumerate(zip(branch_ids, (fresh, shared))):
        path = branches / f"{index}.json"
        path.write_text(json.dumps({"id": branch_id, "messages": {"collections": [message]}}))
        os.utime(path, (1_000_000 + index, 1_000_000 + index))
    return run, manifest


@asynccontextmanager
async def _seeded_store(pg_url: str, original: dict[str, Any]):
    """Create only unique rows and remove them through normal state operations."""
    keeper_id, keeper_progression = _uid(), _uid()
    run_id, branch_a, branch_b, fresh_id = _uid(), _uid(), _uid(), _uid()
    tracked_progressions = {keeper_progression}
    async with StateDB(url=pg_url) as db:
        assert db.dialect == "postgresql"
        original_create = db.create_progression

        async def track_progression(progression_id, collection=None):
            await original_create(progression_id, collection)
            tracked_progressions.add(progression_id)

        db.create_progression = track_progression
        try:
            await db.create_progression(keeper_progression)
            await db.create_session(
                {
                    "id": keeper_id,
                    "progression_id": keeper_progression,
                    "source_kind": "imported_fs",
                    "status": "completed",
                }
            )
            await db.insert_message(state_mod._msg_from_collection_entry(original))
            await db.append_to_progression(keeper_progression, original["id"])
            yield db, run_id, (branch_a, branch_b), fresh_id, keeper_id, keeper_progression
        finally:
            original_error = sys.exc_info()[1]
            try:
                # The import's session owns its fresh rows; deleting the keeper
                # second preserves its shared message until the keeper is gone.
                for session_id in (run_id, keeper_id):
                    if await db.get_session(session_id) is not None:
                        deleted = await db.delete_imported_session(
                            session_id, require_source_kind="imported_fs"
                        )
                        if not deleted:
                            raise RuntimeError("could not clean up test session")
                # These exact generated IDs also cover partial setup/import failure.
                for message_id in (fresh_id, original["id"]):
                    await db.execute("DELETE FROM messages WHERE id = :id", {"id": message_id})
                for progression_id in tracked_progressions:
                    await db.execute(
                        "DELETE FROM progressions WHERE id = :id", {"id": progression_id}
                    )
            except Exception as exc:
                if original_error is None:
                    raise
                warnings.warn(
                    f"PostgreSQL test cleanup also failed: {type(exc).__name__}",
                    RuntimeWarning,
                    stacklevel=1,
                )


async def _stored_row(db: StateDB, message_id: str) -> dict[str, Any]:
    async with db._read() as conn:
        row = (
            (
                await conn.execute(
                    text(
                        "SELECT id, created_at, CAST(content AS TEXT) AS content, "
                        "CAST(node_metadata AS TEXT) AS node_metadata, embedding, "
                        "sender, recipient, channel, role, lion_class "
                        "FROM messages WHERE id = :id"
                    ),
                    {"id": message_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "stored", "incoming"),
    [
        ("content", "true", True),
        ("content", True, "true"),
        ("content", "1", 1),
        ("content", 1, "1"),
        ("metadata", {"flag": "true"}, {"flag": True}),
        ("metadata", {"flag": 1}, {"flag": True}),
        ("metadata", {"source": "original"}, {"source": "replacement"}),
    ],
)
async def test_postgres_shared_message_mismatch_refused_before_writes(
    pg_url, tmp_path, field, stored, incoming
):
    shared_id = _uid()
    original = _message(shared_id, content={"text": "same"}, metadata={"source": "same"})
    original[field] = stored
    replacement = {**original, field: incoming}
    async with _seeded_store(pg_url, original) as (db, run_id, branches, fresh_id, keeper_id, prog):
        run, manifest = _write_run(tmp_path, run_id, branches, fresh_id, replacement)
        before = await _stored_row(db, shared_id)
        keeper_before = await db.get_session(keeper_id)
        progression_before = await db.get_progression(prog)
        statements: list[str] = []

        def record(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(db._engine.sync_engine, "before_cursor_execute", record)
        try:
            with pytest.raises(state_mod.ImportMessageConflictError) as caught:
                await state_mod._import_one_run(db, run_id, run, manifest)
        finally:
            event.remove(db._engine.sync_engine, "before_cursor_execute", record)
        assert caught.value.message_id == shared_id
        assert caught.value.columns == (("node_metadata" if field == "metadata" else field),)
        assert any("CAST(m.content AS TEXT)" in statement for statement in statements)
        assert any("CAST(m.node_metadata AS TEXT)" in statement for statement in statements)
        assert not any(
            statement.lstrip()
            .upper()
            .startswith(("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"))
            for statement in statements
        )
        assert await _stored_row(db, shared_id) == before
        assert await db.get_session(keeper_id) == keeper_before
        assert await db.get_progression(prog) == progression_before == [shared_id]
        assert await db.get_session(run_id) is None
        assert await db.get_message(fresh_id) is None
        for branch_id in branches:
            assert await db.get_branch(branch_id) is None


@pytest.mark.asyncio
async def test_postgres_scalar_json_cast_preserves_string_types(pg_url):
    shared_id = _uid()
    original = _message(shared_id, content="true", metadata={"flag": "true"})
    async with _seeded_store(pg_url, original) as (db, run_id, _branches, _fresh, _keeper, _prog):
        # PostgreSQL's actual async driver must return the JSON text of the
        # scalar, not a driver-decoded value that is parsed a second time.
        raw = await _stored_row(db, shared_id)
        assert json.loads(raw["content"]) == "true"
        assert type(json.loads(raw["content"])) is str
        shared = await state_mod._shared_import_messages(db, run_id, [shared_id])
        assert shared[shared_id]["content"] == "true"
        assert type(shared[shared_id]["content"]) is str
        assert shared[shared_id]["node_metadata"] == {"flag": "true"}
        assert type(shared[shared_id]["node_metadata"]["flag"]) is str

        # A stored JSON scalar is valid to read even though the import's
        # message metadata normally comes from an object.
        await db.execute(
            "UPDATE messages SET node_metadata = CAST(:value AS JSON) WHERE id = :id",
            {"value": json.dumps("true"), "id": shared_id},
        )
        shared = await state_mod._shared_import_messages(db, run_id, [shared_id])
        assert shared[shared_id]["node_metadata"] == "true"
        assert type(shared[shared_id]["node_metadata"]) is str


@pytest.mark.asyncio
async def test_postgres_identical_shared_row_preserved_with_float32_embedding(pg_url, tmp_path):
    shared_id = _uid()
    original = _message(
        shared_id,
        content={"text": "same", "value": "true"},
        metadata={"flag": "true"},
        embedding=[0.1, 0.2],
    )
    async with _seeded_store(pg_url, original) as (db, run_id, branches, fresh_id, keeper_id, prog):
        run, manifest = _write_run(tmp_path, run_id, branches, fresh_id, original)
        before = await _stored_row(db, shared_id)
        assert isinstance(before["embedding"], bytes)
        assert before["embedding"] == struct.pack("<2f", 0.1, 0.2)
        assert struct.unpack("<f", before["embedding"][:4])[0] != 0.1
        assert json.loads(before["content"]) == original["content"]
        assert json.loads(before["node_metadata"]) == original["metadata"]
        assert await state_mod._import_one_run(db, run_id, run, manifest) == (1, 2, 2)
        assert await _stored_row(db, shared_id) == before
        assert await db.get_progression(prog) == [shared_id]
        assert await db.get_session(keeper_id) is not None
        imported = await db.get_session(run_id)
        assert imported is not None
        assert await db.get_progression(imported["progression_id"]) == [fresh_id, shared_id]
        for branch_id in branches:
            assert await db.get_branch(branch_id) is not None
