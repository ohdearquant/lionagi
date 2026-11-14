# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""CLI state prune preview, deletion and retention on both state backends."""

from __future__ import annotations

import time
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import event, text

from lionagi.state import db as db_mod
from lionagi.state.db import StateDB
from lionhub.serve.cli.state import _prune


@pytest_asyncio.fixture(params=["sqlite", "postgresql"])
async def prune_store(request, tmp_path, monkeypatch):
    if request.param == "sqlite":
        monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", tmp_path / "state.db")
        monkeypatch.setattr(
            db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
        )
        yield
        return

    # CLI prune selects from the whole store. Use a private schema even when
    # the session's PostgreSQL URL is shared with other tests.
    pg_url = request.getfixturevalue("pg_url")
    schema = "cli_prune_" + uuid.uuid4().hex
    make_engine = db_mod.make_engine
    admin = make_engine(pg_url)

    def scoped_engine(url, **kwargs):
        engine = make_engine(url, **kwargs)

        @event.listens_for(engine.sync_engine, "connect")
        def set_schema(connection, _record):
            cursor = connection.cursor()
            try:
                cursor.execute(f'SET search_path TO "{schema}"')  # noqa: S608
            finally:
                cursor.close()
            connection.commit()

        return engine

    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))  # noqa: S608
        try:
            monkeypatch.setattr(db_mod, "make_engine", scoped_engine)
            monkeypatch.setattr(
                db_mod,
                "settings",
                db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": pg_url}),
            )
            yield
        finally:
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))  # noqa: S608
    finally:
        await admin.dispose()


async def test_cli_prune_previews_and_deletes_only_unshared_messages(prune_store):
    now = time.time()
    async with StateDB() as db:
        for message_id in ("victim-listed", "victim-direct", "shared-listed", "shared-direct"):
            await db.insert_message(
                {
                    "id": message_id,
                    "created_at": now,
                    "role": "user",
                    "content": {"text": message_id},
                }
            )
        await db.create_progression(
            "victim-history", ["victim-listed", "shared-listed", "shared-direct"]
        )
        await db.create_progression("victim-branch-history")
        await db.create_progression("keeper-history")
        # Older stores can have a JSON null in a collection. It cannot pin
        # unrelated orphan messages when the survivor references are checked.
        await db.execute(
            "UPDATE progressions SET collection = ? WHERE id = ?",
            ('[null, "shared-listed"]', "keeper-history"),
        )
        await db.create_progression("keeper-branch-history")
        for name in ("victim", "keeper"):
            await db.create_session(
                {"id": name, "progression_id": f"{name}-history", "status": "completed"}
            )
            await db.create_branch(
                {
                    "id": f"{name}-branch",
                    "session_id": name,
                    "progression_id": f"{name}-branch-history",
                }
            )
        await db.execute(
            "UPDATE sessions SET updated_at = ?, first_msg_id = ? WHERE id = ?",
            (now - 60 * 86400, "victim-direct", "victim"),
        )
        await db.execute(
            "UPDATE branches SET system_msg_id = ? WHERE id = ?", ("shared-direct", "keeper-branch")
        )

    async def stored_rows():
        async with StateDB() as db:
            return {
                table: await db.fetch_all(f"SELECT * FROM {table} ORDER BY id")  # noqa: S608
                for table in ("sessions", "branches", "progressions", "messages")
            }

    before = await stored_rows()
    expected = {"sessions": 1, "branches": 1, "messages": 2}
    assert await _prune(keep_days=30, keep_n=1, dry_run=True) == expected
    assert await stored_rows() == before

    assert await _prune(keep_days=30, keep_n=1, dry_run=False) == expected
    survivor_ids = {
        "sessions": {"keeper"},
        "branches": {"keeper-branch"},
        "progressions": {"keeper-history", "keeper-branch-history"},
        "messages": {"shared-listed", "shared-direct"},
    }
    survivors = {
        table: [row for row in rows if row["id"] in survivor_ids[table]]
        for table, rows in before.items()
    }
    assert {table: {row["id"] for row in rows} for table, rows in survivors.items()} == survivor_ids
    assert await stored_rows() == survivors
    assert await _prune(keep_days=30, keep_n=1, dry_run=False) == dict.fromkeys(expected, 0)
    assert await stored_rows() == survivors
