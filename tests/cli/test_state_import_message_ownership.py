# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Run imports must not rewrite messages listed by another session."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import event

from lionagi.cli import state as state_mod
from lionagi.state import db as db_mod
from lionagi.state.db import StateDB


def _message(message_id: str, value: Any) -> dict:
    return {"id": message_id, "created_at": 1.0, "role": "user", "content": {"text": value}}


def _make_run(runs, value: Any):
    run = runs / "run-1"
    branches = run / "branches"
    branches.mkdir(parents=True)
    manifest = {"run_id": "run-1", "kind": "agent", "status": "completed"}
    (run / "run.json").write_text(json.dumps(manifest))
    for number, messages in enumerate(([_message("fresh", "new")], [_message("shared", value)])):
        path = branches / f"{number}.json"
        path.write_text(
            json.dumps({"id": f"branch-{number}", "messages": {"collections": messages}})
        )
        os.utime(path, (1_000_000 + number, 1_000_000 + number))
    return run, manifest


async def _keeper(db, membership: str):
    await db.create_progression("keeper-session-progress")
    await db.create_session(
        {"id": "keeper", "progression_id": "keeper-session-progress", "status": "running"}
    )
    await db.insert_message(_message("shared", "original"))
    if membership == "session":
        await db.append_to_progression("keeper-session-progress", "shared")
    elif membership in {"first", "last"}:
        await db.update_session("keeper", **{f"{membership}_msg_id": "shared"})
    else:
        await db.create_progression(
            "keeper-branch-progress", ["shared"] if membership == "branch" else []
        )
        await db.create_branch(
            {
                "id": "keeper-branch",
                "session_id": "keeper",
                "progression_id": "keeper-branch-progress",
                "system_msg_id": "shared" if membership == "system" else None,
            }
        )


async def _snapshot(db):
    return {
        table: await db.fetch_all(f"SELECT * FROM {table} ORDER BY id")  # noqa: S608
        for table in ("sessions", "branches", "progressions", "messages")
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("membership", ["session", "branch", "first", "last", "system"])
async def test_import_conflicting_shared_message_refuses_before_any_write(tmp_path, membership):
    run, manifest = _make_run(tmp_path / "runs", "replacement")
    async with StateDB(tmp_path / "state.db") as db:
        await _keeper(db, membership)
        before = await _snapshot(db)
        writes = []

        def record(_conn, _cursor, statement, _parameters, _context, _executemany):
            if (
                statement.lstrip()
                .upper()
                .startswith(("INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"))
            ):
                writes.append(statement)

        event.listen(db._engine.sync_engine, "before_cursor_execute", record)
        try:
            with pytest.raises(state_mod.ImportMessageConflictError) as caught:
                await state_mod._import_one_run(db, "run-1", run, manifest)
        finally:
            event.remove(db._engine.sync_engine, "before_cursor_execute", record)
        assert caught.value.message_id == "shared"
        assert "original" not in str(caught.value)
        assert "replacement" not in str(caught.value)
        assert writes == []
        assert await _snapshot(db) == before
        assert (await db.get_message("shared"))["content"] == {"text": "original"}


@pytest.mark.asyncio
async def test_identical_shared_message_import_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    runs = tmp_path / "runs"
    _make_run(runs, "original")
    monkeypatch.setattr(state_mod, "RUNS_ROOT", runs)
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    async with StateDB(path) as db:
        await _keeper(db, "session")
    assert await state_mod._import_runs() == {
        "sessions": 1,
        "branches": 2,
        "messages": 2,
        "skipped": 0,
        "errors": 0,
    }
    async with StateDB(path) as db:
        before = await _snapshot(db)
        assert (await db.get_message("shared"))["content"] == {"text": "original"}
        assert await db.get_progression("keeper-session-progress") == ["shared"]
    assert await state_mod._import_runs() == {
        "sessions": 0,
        "branches": 0,
        "messages": 0,
        "skipped": 1,
        "errors": 0,
    }
    async with StateDB(path) as db:
        assert await _snapshot(db) == before
        assert (await db.get_message("shared"))["content"] == {"text": "original"}


@pytest.mark.asyncio
async def test_conflicting_import_is_reported_and_leaves_keeper_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    runs = tmp_path / "runs"
    _make_run(runs, "replacement")
    monkeypatch.setattr(state_mod, "RUNS_ROOT", runs)
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    async with StateDB(path) as db:
        await _keeper(db, "session")
        before = await _snapshot(db)
    assert await state_mod._import_runs() == {
        "sessions": 0,
        "branches": 0,
        "messages": 0,
        "skipped": 0,
        "errors": 1,
    }
    async with StateDB(path) as db:
        assert await _snapshot(db) == before
        assert (await db.get_message("shared"))["content"] == {"text": "original"}


@pytest.mark.asyncio
@pytest.mark.parametrize("original,replacement", [(False, 0), (True, 1), (0, False), (1, True)])
async def test_json_boolean_and_number_are_distinct_content(tmp_path, original, replacement):
    run, manifest = _make_run(tmp_path / "runs", replacement)
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_progression("keeper-progress")
        await db.create_session({"id": "keeper", "progression_id": "keeper-progress"})
        await db.insert_message(_message("shared", original))
        await db.append_to_progression("keeper-progress", "shared")
        before = await _snapshot(db)
        with pytest.raises(state_mod.ImportMessageConflictError):
            await state_mod._import_one_run(db, "run-1", run, manifest)
        assert await _snapshot(db) == before
        content = (await db.get_message("shared"))["content"]["text"]
        assert type(content) is type(original)
        assert content == original


@pytest.mark.asyncio
async def test_unreferenced_existing_message_keeps_existing_import_upsert_behavior(tmp_path):
    run, manifest = _make_run(tmp_path / "runs", "replacement")
    async with StateDB(tmp_path / "state.db") as db:
        await db.insert_message(_message("shared", "original"))
        assert await state_mod._import_one_run(db, "run-1", run, manifest) == (1, 2, 2)
        assert (await db.get_message("shared"))["content"] == {"text": "replacement"}


@pytest.mark.asyncio
async def test_later_occurrence_with_changed_content_refuses_before_writes(tmp_path):
    run, manifest = _make_run(tmp_path / "runs", "replacement")
    first = run / "branches" / "0.json"
    first.write_text(
        json.dumps(
            {"id": "branch-0", "messages": {"collections": [_message("shared", "original")]}}
        )
    )
    os.utime(first, (1_000_000, 1_000_000))
    async with StateDB(tmp_path / "state.db") as db:
        await _keeper(db, "session")
        before = await _snapshot(db)
        with pytest.raises(state_mod.ImportMessageConflictError):
            await state_mod._import_one_run(db, "run-1", run, manifest)
        assert await _snapshot(db) == before
        assert (await db.get_message("shared"))["content"] == {"text": "original"}


@pytest.mark.asyncio
async def test_matching_content_with_different_role_is_refused(tmp_path):
    run, manifest = _make_run(tmp_path / "runs", "original")
    second = run / "branches" / "1.json"
    data = json.loads(second.read_text())
    data["messages"]["collections"][0]["role"] = "assistant"
    second.write_text(json.dumps(data))
    os.utime(second, (1_000_001, 1_000_001))
    async with StateDB(tmp_path / "state.db") as db:
        await _keeper(db, "session")
        before = await _snapshot(db)
        with pytest.raises(state_mod.ImportMessageConflictError) as caught:
            await state_mod._import_one_run(db, "run-1", run, manifest)
        assert caught.value.columns == ("role",)
        assert await _snapshot(db) == before


@pytest.mark.asyncio
async def test_conflict_in_second_bind_batch_refuses_all_writes(tmp_path):
    run, manifest = _make_run(tmp_path / "runs", "replacement")
    first = run / "branches" / "0.json"
    first.write_text(
        json.dumps(
            {
                "id": "branch-0",
                "messages": {"collections": [_message(f"fresh-{i}", "new") for i in range(500)]},
            }
        )
    )
    os.utime(first, (1_000_000, 1_000_000))
    async with StateDB(tmp_path / "state.db") as db:
        await _keeper(db, "session")
        before = await _snapshot(db)
        with pytest.raises(state_mod.ImportMessageConflictError) as caught:
            await state_mod._import_one_run(db, "run-1", run, manifest)
        assert caught.value.message_id == "shared"
        assert await _snapshot(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
async def test_ownership_lookup_uses_bounded_parameters_and_dialect_sql(dialect):
    from sqlalchemy.dialects import sqlite

    statements = []
    candidates = [f"id-{index}" for index in range(500)]

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def mappings(self):
            return self

        def all(self):
            return self.rows

    class Connection:
        async def execute(self, statement, params):
            statements.append((statement, params))
            if len(statements) == 1:
                return Result(candidates)
            return Result([])

    class FakeDB:
        @asynccontextmanager
        async def read_snapshot(self):
            yield Connection()

    db = FakeDB()
    db.dialect = dialect
    assert await state_mod._shared_import_messages(db, "imported", candidates) == {}
    assert len(statements) == 2
    statement, params = statements[-1]
    assert len(params) == 501
    assert set(statement._bindparams) == set(params)
    sql = str(statement)
    if dialect == "sqlite":
        assert "json_each(p.collection)" in sql
        assert len(statement.compile(dialect=sqlite.dialect()).positiontup) == 504
    else:
        assert "jsonb_array_elements_text(p.collection::jsonb)" in sql
        assert "jsonb" not in statement._bindparams


@pytest.mark.asyncio
async def test_ownership_lookup_refuses_unknown_dialect_before_read():
    class FakeDB:
        dialect = "other"

        def read_snapshot(self):
            pytest.fail("unsupported dialect must be refused before opening a snapshot")

    with pytest.raises(ValueError, match="other"):
        await state_mod._shared_import_messages(FakeDB(), "imported", ["shared"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("original", "incoming", "conflicts"),
    [
        ("true", True, True),
        ("1", 1, True),
        ("null", None, True),
        ("{}", {}, True),
        (True, "true", True),
        (1, "1", True),
        (None, "null", True),
        ({}, "{}", True),
        ("true", "true", False),
        ("1", "1", False),
        ("null", "null", False),
        ("{}", "{}", False),
    ],
)
async def test_json_scalar_string_content_is_not_decoded_twice(
    tmp_path, original, incoming, conflicts
):
    run, manifest = _make_run(tmp_path / "runs", "placeholder")
    second = run / "branches" / "1.json"
    data = json.loads(second.read_text())
    data["messages"]["collections"][0]["content"] = incoming
    second.write_text(json.dumps(data))
    os.utime(second, (1_000_001, 1_000_001))
    async with StateDB(tmp_path / "state.db") as db:
        await db.create_progression("keeper-progress")
        await db.create_session({"id": "keeper", "progression_id": "keeper-progress"})
        message = _message("shared", "placeholder")
        message["content"] = original if original is not None else "placeholder"
        await db.insert_message(message)
        if original is None:
            await db.execute("UPDATE messages SET content = ? WHERE id = ?", ("null", "shared"))
        await db.append_to_progression("keeper-progress", "shared")
        before = await _snapshot(db)
        if conflicts:
            with pytest.raises(state_mod.ImportMessageConflictError):
                await state_mod._import_one_run(db, "run-1", run, manifest)
            assert await _snapshot(db) == before
        else:
            assert await state_mod._import_one_run(db, "run-1", run, manifest) == (1, 2, 2)
        content = await db.fetch_one(
            "SELECT CAST(content AS TEXT) AS content FROM messages WHERE id = ?", ("shared",)
        )
        decoded = json.loads(content["content"])
        assert type(decoded) is type(original)
        assert decoded == original


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["true", "1", "null", "{}"])
async def test_postgres_text_cast_returns_json_string_without_double_decode(value):
    statements = []

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def mappings(self):
            return self

        def all(self):
            return self.rows

    class Connection:
        async def execute(self, statement, params):
            statements.append(str(statement))
            if len(statements) == 1:
                return Result(["shared"])
            return Result(
                [{"id": "shared", "content": json.dumps(value), "node_metadata": json.dumps(value)}]
            )

    class FakeDB:
        dialect = "postgresql"

        @asynccontextmanager
        async def read_snapshot(self):
            yield Connection()

    shared = await state_mod._shared_import_messages(FakeDB(), "run-1", ["shared"])
    assert shared["shared"]["content"] == value
    assert type(shared["shared"]["content"]) is str
    assert shared["shared"]["node_metadata"] == value
    assert type(shared["shared"]["node_metadata"]) is str
    assert "CAST(m.content AS TEXT)" in statements[1]
    assert "CAST(m.node_metadata AS TEXT)" in statements[1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "reported"),
    [
        ("content", {"text": "PRIVATE-CONTENT"}, "content"),
        ("role", "PRIVATE-ROLE", "role"),
        ("sender", "PRIVATE-SENDER", "sender"),
        ("recipient", "PRIVATE-RECIPIENT", "recipient"),
        ("channel", "PRIVATE-CHANNEL", "channel"),
        ("metadata", {"token": "PRIVATE-METADATA"}, "node_metadata"),
        ("embedding", [0.125, 0.5], "embedding"),
    ],
)
async def test_shared_upsert_field_difference_refuses_without_disclosing_values(
    tmp_path, field, value, reported
):
    run, manifest = _make_run(tmp_path / "runs", "original")
    second = run / "branches" / "1.json"
    data = json.loads(second.read_text())
    data["messages"]["collections"][0][field] = value
    second.write_text(json.dumps(data))
    os.utime(second, (1_000_001, 1_000_001))
    async with StateDB(tmp_path / "state.db") as db:
        await _keeper(db, "session")
        before = await _snapshot(db)
        with pytest.raises(state_mod.ImportMessageConflictError) as caught:
            await state_mod._import_one_run(db, "run-1", run, manifest)
        assert caught.value.message_id == "shared"
        assert caught.value.columns == (reported,)
        assert reported in str(caught.value)
        assert "PRIVATE" not in str(caught.value)
        assert "original" not in str(caught.value)
        assert await _snapshot(db) == before


@pytest.mark.asyncio
async def test_multiple_shared_differences_report_only_column_names(tmp_path):
    run, manifest = _make_run(tmp_path / "runs", "PRIVATE-CONTENT")
    second = run / "branches" / "1.json"
    data = json.loads(second.read_text())
    data["messages"]["collections"][0].update(
        {"sender": "PRIVATE-SENDER", "metadata": {"secret": "PRIVATE-META"}}
    )
    second.write_text(json.dumps(data))
    os.utime(second, (1_000_001, 1_000_001))
    async with StateDB(tmp_path / "state.db") as db:
        await _keeper(db, "session")
        before = await _snapshot(db)
        with pytest.raises(state_mod.ImportMessageConflictError) as caught:
            await state_mod._import_one_run(db, "run-1", run, manifest)
        assert caught.value.columns == ("content", "node_metadata", "sender")
        assert "PRIVATE" not in str(caught.value)
        assert await _snapshot(db) == before


@pytest.mark.asyncio
async def test_same_shared_embedding_uses_stored_float32_precision(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    runs = tmp_path / "runs"
    run, _ = _make_run(runs, "original")
    second = run / "branches" / "1.json"
    data = json.loads(second.read_text())
    data["messages"]["collections"][0]["embedding"] = [0.1, 0.2]
    second.write_text(json.dumps(data))
    os.utime(second, (1_000_001, 1_000_001))
    monkeypatch.setattr(state_mod, "RUNS_ROOT", runs)
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", path)
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    async with StateDB(path) as db:
        await _keeper(db, "session")
        await db.insert_message({**_message("shared", "original"), "embedding": [0.1, 0.2]})
        before_shared = await db.get_message("shared")
    assert (await state_mod._import_runs())["errors"] == 0
    async with StateDB(path) as db:
        before_reimport = await _snapshot(db)
        assert await db.get_message("shared") == before_shared
    assert (await state_mod._import_runs())["skipped"] == 1
    async with StateDB(path) as db:
        assert await _snapshot(db) == before_reimport
        assert await db.get_message("shared") == before_shared


@pytest.mark.asyncio
async def test_stored_lion_class_difference_is_checked_independently(tmp_path):
    run, manifest = _make_run(tmp_path / "runs", "original")
    second = run / "branches" / "1.json"
    data = json.loads(second.read_text())
    data["messages"]["collections"][0]["metadata"] = {
        "lion_class": "lionagi.protocols.messages.system.System"
    }
    second.write_text(json.dumps(data))
    os.utime(second, (1_000_001, 1_000_001))
    async with StateDB(tmp_path / "state.db") as db:
        await _keeper(db, "session")
        await db.insert_message(
            {
                **_message("shared", "original"),
                "node_metadata": {"lion_class": "lionagi.protocols.messages.system.System"},
            }
        )
        await db.execute("UPDATE messages SET lion_class = 0 WHERE id = ?", ("shared",))
        before = await _snapshot(db)
        with pytest.raises(state_mod.ImportMessageConflictError) as caught:
            await state_mod._import_one_run(db, "run-1", run, manifest)
        assert caught.value.columns == ("lion_class",)
        assert await _snapshot(db) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("old,new", [(False, 0), (True, 1), (0, False), (1, True)])
async def test_shared_metadata_comparison_preserves_json_boolean_type(tmp_path, old, new):
    run, manifest = _make_run(tmp_path / "runs", "original")
    second = run / "branches" / "1.json"
    data = json.loads(second.read_text())
    data["messages"]["collections"][0]["metadata"] = {"value": new}
    second.write_text(json.dumps(data))
    os.utime(second, (1_000_001, 1_000_001))
    async with StateDB(tmp_path / "state.db") as db:
        await _keeper(db, "session")
        await db.insert_message({**_message("shared", "original"), "node_metadata": {"value": old}})
        before = await _snapshot(db)
        with pytest.raises(state_mod.ImportMessageConflictError) as caught:
            await state_mod._import_one_run(db, "run-1", run, manifest)
        assert caught.value.columns == ("node_metadata",)
        assert await _snapshot(db) == before


@pytest.mark.asyncio
async def test_postgres_fake_decodes_both_json_columns_once_and_compares_float32():
    statements = []
    packed = db_mod._pack_embedding([0.1, 0.2])

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def mappings(self):
            return self

        def all(self):
            return self.rows

    class Connection:
        async def execute(self, statement, params):
            statements.append(str(statement))
            if len(statements) == 1:
                return Result(["shared"])
            return Result(
                [
                    {
                        "id": "shared",
                        "content": json.dumps("true"),
                        "node_metadata": json.dumps({"flag": False}),
                        "role": "user",
                        "sender": None,
                        "recipient": None,
                        "channel": None,
                        "lion_class": "__unknown__",
                        "lion_class_id": 0,
                        "embedding": memoryview(packed),
                    }
                ]
            )

    class FakeDB:
        dialect = "postgresql"

        @asynccontextmanager
        async def read_snapshot(self):
            yield Connection()

    shared = await state_mod._shared_import_messages(FakeDB(), "run-1", ["shared"])
    incoming = {
        "id": "shared",
        "created_at": 1.0,
        "content": "true",
        "metadata": {"flag": False},
        "embedding": [0.1, 0.2],
        "role": "user",
    }
    assert state_mod._import_message_differences(shared["shared"], incoming) == ()
    incoming["metadata"] = {"flag": 0}
    assert state_mod._import_message_differences(shared["shared"], incoming) == ("node_metadata",)
    assert "CAST(m.content AS TEXT)" in statements[1]
    assert "CAST(m.node_metadata AS TEXT)" in statements[1]


@pytest.mark.asyncio
async def test_identical_shared_metadata_with_reordered_keys_and_known_class(tmp_path):
    run, manifest = _make_run(tmp_path / "runs", "original")
    second = run / "branches" / "1.json"
    data = json.loads(second.read_text())
    data["messages"]["collections"][0]["metadata"] = {
        "nested": {"b": 2, "a": 1},
        "lion_class": "lionagi.protocols.messages.system.System",
    }
    second.write_text(json.dumps(data))
    os.utime(second, (1_000_001, 1_000_001))
    async with StateDB(tmp_path / "state.db") as db:
        await _keeper(db, "session")
        await db.insert_message(
            {
                **_message("shared", "original"),
                "node_metadata": {
                    "lion_class": "lionagi.protocols.messages.system.System",
                    "nested": {"a": 1, "b": 2},
                },
            }
        )
        before = await db.get_message("shared")
        assert await state_mod._import_one_run(db, "run-1", run, manifest) == (1, 2, 2)
        assert await db.get_message("shared") == before
