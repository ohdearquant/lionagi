# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""StateDB persistence + migration tests for the 'command' action kind."""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid

import aiosqlite
import pytest

from lionagi.state.db import StateDB


async def test_create_schedule_command_kind_roundtrips():
    """A fresh DB accepts action_kind='command' (CHECK widened) and
    action_command / action_command_args roundtrip through create+get."""
    state = StateDB(":memory:")
    await state.open()

    sid = uuid.uuid4().hex[:12]
    await state.create_schedule(
        {
            "id": sid,
            "name": "command-roundtrip",
            "trigger_type": "cron",
            "cron_expr": "0 * * * *",
            "action_kind": "command",
            "action_command": "kdev",
            "action_command_args": ["review-pr", "--repo", "{{repo}}"],
        }
    )
    row = await state.get_schedule(sid)
    assert row is not None
    assert row["action_kind"] == "command"
    assert row["action_command"] == "kdev"
    assert row["action_command_args"] == ["review-pr", "--repo", "{{repo}}"]

    await state.close()


async def test_update_schedule_command_fields():
    """update_schedule accepts action_command / action_command_args as
    allowed fields and persists the JSON-typed args list correctly."""
    state = StateDB(":memory:")
    await state.open()

    sid = uuid.uuid4().hex[:12]
    await state.create_schedule(
        {
            "id": sid,
            "name": "command-update",
            "trigger_type": "cron",
            "cron_expr": "0 * * * *",
            "action_kind": "command",
            "action_command": "kdev",
            "action_command_args": [],
        }
    )
    await state.update_schedule(
        sid, action_command="other-tool", action_command_args=["--flag", "{{value}}"]
    )
    row = await state.get_schedule(sid)
    assert row["action_command"] == "other-tool"
    assert row["action_command_args"] == ["--flag", "{{value}}"]

    await state.close()


def test_legacy_flow_yaml_schedules_table_upgraded_to_admit_command():
    """A schedules table already rebuilt to admit 'flow_yaml' (so
    _drop_legacy_action_kind_check's own marker check short-circuits) but
    predating the widened action-kind vocabulary must still be upgraded to admit 'command' by the
    new, distinct _drop_legacy_schedules_command_check migration."""

    async def _run():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            async with aiosqlite.connect(db_path) as raw:
                await raw.execute("""
                    CREATE TABLE schedules (
                        id               TEXT PRIMARY KEY,
                        name             TEXT NOT NULL UNIQUE,
                        trigger_type     TEXT NOT NULL,
                        action_kind      TEXT NOT NULL
                            CHECK(action_kind IN ('agent', 'flow', 'fanout', 'play', 'flow_yaml')),
                        action_flow_yaml TEXT,
                        created_at       REAL NOT NULL,
                        updated_at       REAL NOT NULL
                    )
                """)
                await raw.commit()

            async with StateDB(db_path) as db:
                sid = uuid.uuid4().hex[:12]
                await db.create_schedule(
                    {
                        "id": sid,
                        "name": "post-flow-yaml-command-upgrade",
                        "trigger_type": "cron",
                        "cron_expr": "0 * * * *",
                        "action_kind": "command",
                        "action_command": "kdev",
                        "action_command_args": ["review-pr"],
                    }
                )
                row = await db.get_schedule(sid)

            assert row is not None, "schedule not found after command-check upgrade"
            assert row["action_kind"] == "command"
            assert row["action_command"] == "kdev"
            assert row["action_command_args"] == ["review-pr"]
        finally:
            os.unlink(db_path)

    asyncio.run(_run())


def test_legacy_pre_flow_yaml_schedules_table_upgraded_to_admit_command():
    """The oldest-legacy 4-value CHECK table (predates both 'flow_yaml' and
    'command') is upgraded to the full current CHECK in a single pass via
    _drop_legacy_action_kind_check's schema_meta-derived rebuild."""

    async def _run():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            async with aiosqlite.connect(db_path) as raw:
                await raw.execute("""
                    CREATE TABLE schedules (
                        id           TEXT PRIMARY KEY,
                        name         TEXT NOT NULL UNIQUE,
                        trigger_type TEXT NOT NULL,
                        action_kind  TEXT NOT NULL
                            CHECK(action_kind IN ('agent', 'flow', 'fanout', 'play')),
                        created_at   REAL NOT NULL,
                        updated_at   REAL NOT NULL
                    )
                """)
                await raw.commit()

            async with StateDB(db_path) as db:
                sid = uuid.uuid4().hex[:12]
                await db.create_schedule(
                    {
                        "id": sid,
                        "name": "pre-flow-yaml-command-upgrade",
                        "trigger_type": "cron",
                        "cron_expr": "0 * * * *",
                        "action_kind": "command",
                        "action_command": "kdev",
                        "action_command_args": [],
                    }
                )
                row = await db.get_schedule(sid)

            assert row is not None
            assert row["action_kind"] == "command"
        finally:
            os.unlink(db_path)

    asyncio.run(_run())


def test_legacy_pre_flow_yaml_rebuild_preserves_dependent_schedule_runs():
    """The oldest-legacy schedules rebuild must not cascade-delete
    schedule_runs rows that reference the schedule being rebuilt.

    Regression for a rebuild that flipped `PRAGMA foreign_keys = OFF`
    through a SQLAlchemy connection already inside an open `engine.begin()`
    transaction: SQLite silently ignores a pragma toggle issued mid-
    transaction, so the subsequent `DROP TABLE schedules` cascaded through
    the schedule_runs -> schedules foreign key and deleted every dependent
    run row. The fix routes the rebuild through the same raw-driver
    autocommit technique used by the sibling schedules/schedule_runs
    rebuilds, so the pragma flip actually takes effect before the drop.
    """

    async def _run():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            sid = uuid.uuid4().hex[:12]
            run_id = uuid.uuid4().hex[:12]
            async with aiosqlite.connect(db_path) as raw:
                await raw.execute("""
                    CREATE TABLE schedules (
                        id           TEXT PRIMARY KEY,
                        name         TEXT NOT NULL UNIQUE,
                        trigger_type TEXT NOT NULL,
                        action_kind  TEXT NOT NULL
                            CHECK(action_kind IN ('agent', 'flow', 'fanout', 'play')),
                        created_at   REAL NOT NULL,
                        updated_at   REAL NOT NULL
                    )
                """)
                # Current-shape schedule_runs table (widened CHECK already
                # present, so _drop_legacy_schedule_runs_check no-ops and
                # this test isolates the schedules rebuild specifically).
                await raw.execute("""
                    CREATE TABLE schedule_runs (
                        id              TEXT PRIMARY KEY,
                        schedule_id     TEXT REFERENCES schedules(id) ON DELETE CASCADE,
                        invocation_id   TEXT,
                        trigger_context JSON NOT NULL,
                        action_kind     TEXT NOT NULL,
                        action_args     JSON NOT NULL,
                        status          TEXT NOT NULL DEFAULT 'running'
                            CHECK(status IN ('queued', 'waiting_dependency',
                                  'running', 'retry_wait', 'completed',
                                  'failed', 'timed_out', 'skipped',
                                  'cancelled')),
                        fired_at        REAL NOT NULL,
                        created_at      REAL NOT NULL
                    )
                """)
                await raw.execute(
                    "INSERT INTO schedules "
                    "(id, name, trigger_type, action_kind, created_at, updated_at) "
                    "VALUES (?, ?, 'cron', 'agent', 0, 0)",
                    (sid, "pre-flow-yaml-with-dependent-run"),
                )
                await raw.execute(
                    "INSERT INTO schedule_runs "
                    "(id, schedule_id, trigger_context, action_kind, action_args, "
                    "status, fired_at, created_at) "
                    "VALUES (?, ?, '{}', 'agent', '[]', 'completed', 0, 0)",
                    (run_id, sid),
                )
                await raw.commit()

            async with StateDB(db_path) as db:
                run_row = await db.fetch_one(
                    "SELECT * FROM schedule_runs WHERE id = :id", {"id": run_id}
                )
                assert run_row is not None, (
                    "dependent schedule_runs row was cascade-deleted by the "
                    "pre-flow_yaml schedules rebuild"
                )

                # The schedule survived too, and its CHECK now admits 'command'.
                await db.create_schedule(
                    {
                        "id": uuid.uuid4().hex[:12],
                        "name": "post-rebuild-command-admit-check",
                        "trigger_type": "cron",
                        "cron_expr": "0 * * * *",
                        "action_kind": "command",
                        "action_command": "kdev",
                        "action_command_args": [],
                    }
                )

                # Foreign-key enforcement was restored (not left OFF).
                fk_row = await db.fetch_one("PRAGMA foreign_keys")
                assert fk_row is not None
                assert list(fk_row.values())[0] == 1
        finally:
            os.unlink(db_path)

    asyncio.run(_run())


async def test_invalid_action_kind_still_rejected_by_check_constraint():
    """The widened CHECK still rejects an unknown action_kind -- 'command'
    joins the closed set, it doesn't open it up."""
    from sqlalchemy.exc import IntegrityError

    state = StateDB(":memory:")
    await state.open()

    with pytest.raises(IntegrityError):
        await state.create_schedule(
            {
                "id": uuid.uuid4().hex[:12],
                "name": "bogus-kind",
                "trigger_type": "cron",
                "cron_expr": "0 * * * *",
                "action_kind": "not-a-real-kind",
            }
        )

    await state.close()
