# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""StateDB persistence + migration tests for the 'command' action kind."""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from unittest.mock import patch

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


def test_rebuild_restores_fk_enforcement_when_pragma_flush_commit_fails():
    """A failure in the flush commit that makes `PRAGMA foreign_keys = OFF`
    take effect must still restore enforcement: that commit is awaited, so a
    cancellation or driver error there is a realistic boundary, and leaving
    the pooled connection with enforcement disabled would silently disable
    every cascade/integrity check for the rest of the process."""

    async def _run():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            sid = uuid.uuid4().hex[:12]
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
                await raw.execute(
                    "INSERT INTO schedules "
                    "(id, name, trigger_type, action_kind, created_at, updated_at) "
                    "VALUES (?, ?, 'cron', 'agent', 0, 0)",
                    (sid, "pragma-flush-fault-target"),
                )
                await raw.commit()

            real_execute = aiosqlite.Connection.execute
            real_commit = aiosqlite.Connection.commit
            fault_state = {"armed": False, "fired": False}

            async def _tracking_execute(self, sql, parameters=None):
                if (
                    not fault_state["fired"]
                    and isinstance(sql, str)
                    and "foreign_keys = OFF" in sql
                ):
                    fault_state["armed"] = True
                return await real_execute(self, sql, parameters)

            async def _faulty_commit(self):
                if fault_state["armed"] and not fault_state["fired"]:
                    fault_state["fired"] = True
                    fault_state["armed"] = False
                    raise RuntimeError("fault-injected failure in pragma flush commit")
                return await real_commit(self)

            db = StateDB(db_path)
            try:
                with (
                    patch.object(aiosqlite.Connection, "execute", _tracking_execute),
                    patch.object(aiosqlite.Connection, "commit", _faulty_commit),
                ):
                    with pytest.raises(RuntimeError, match="pragma flush commit"):
                        await db.open()

                # Enforcement must be back ON on the live connection even
                # though the failure happened before the rebuild began.
                fk_row = await db.fetch_one("PRAGMA foreign_keys")
                assert fk_row is not None
                assert list(fk_row.values())[0] == 1

                # The legacy table and its row are untouched: nothing was
                # rebuilt, nothing was lost.
                sched_row = await db.fetch_one(
                    "SELECT * FROM schedules WHERE id = :id", {"id": sid}
                )
                assert sched_row is not None
            finally:
                await db.close()

            # A fresh open with no fault completes the migration.
            async with StateDB(db_path) as db2:
                row2 = await db2.fetch_one("SELECT * FROM schedules WHERE id = :id", {"id": sid})
                assert row2 is not None

        finally:
            os.unlink(db_path)

    asyncio.run(_run())


def test_legacy_pre_flow_yaml_rebuild_rolls_back_on_fault_and_permits_clean_retry():
    """Fault-injection regression for the schedules-rebuild atomicity fix.

    The raw-driver rebuild used to run CREATE/copy/DROP/RENAME/index as
    independent autocommit statements with no explicit transaction: an
    exception between DROP and RENAME left only `schedules_new` on disk, and
    the next ordinary StateDB open's `metadata.create_all` then created a
    fresh EMPTY `schedules`, stranding the original rows in `schedules_new`.
    This injects a failure at exactly that point (right before the RENAME)
    and asserts the explicit `BEGIN IMMEDIATE` transaction rolls the whole
    rebuild back: the original `schedules` table and its row survive intact,
    `schedules_new` does not exist, and `PRAGMA foreign_keys` reads back 1
    despite the failure. A subsequent clean retry (fresh StateDB open, no
    fault) must then complete the migration successfully.
    """

    async def _run():
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            sid = uuid.uuid4().hex[:12]
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
                await raw.execute(
                    "INSERT INTO schedules "
                    "(id, name, trigger_type, action_kind, created_at, updated_at) "
                    "VALUES (?, ?, 'cron', 'agent', 0, 0)",
                    (sid, "fault-injection-target"),
                )
                await raw.commit()

            real_execute = aiosqlite.Connection.execute
            fault_state = {"triggered": False}

            async def _faulty_execute(self, sql, parameters=None):
                if (
                    not fault_state["triggered"]
                    and isinstance(sql, str)
                    and "RENAME TO schedules" in sql
                ):
                    fault_state["triggered"] = True
                    raise RuntimeError("fault-injected failure before RENAME")
                return await real_execute(self, sql, parameters)

            db = StateDB(db_path)
            try:
                with patch.object(aiosqlite.Connection, "execute", _faulty_execute):
                    with pytest.raises(RuntimeError, match="fault-injected"):
                        await db.open()

                # The original legacy table (and its row) must survive the
                # aborted rebuild untouched -- rolled back, not stranded.
                table_row = await db.fetch_one(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='schedules'"
                )
                assert table_row is not None
                assert "'flow_yaml'" not in table_row["sql"], (
                    "original legacy schedules table must survive the rollback intact"
                )
                sched_row = await db.fetch_one(
                    "SELECT * FROM schedules WHERE id = :id", {"id": sid}
                )
                assert sched_row is not None
                assert sched_row["name"] == "fault-injection-target"

                # schedules_new must be gone -- rolled back, not left stranded
                # for the next metadata.create_all to paper over with an
                # empty fresh `schedules`.
                stray_row = await db.fetch_one(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='schedules_new'"
                )
                assert stray_row is None

                # Foreign-key enforcement was restored despite the failure.
                fk_row = await db.fetch_one("PRAGMA foreign_keys")
                assert fk_row is not None
                assert list(fk_row.values())[0] == 1
            finally:
                await db.close()

            # A clean retry (fresh StateDB open, no fault injected) completes
            # the migration and preserves the original row.
            async with StateDB(db_path) as db2:
                sched_row2 = await db2.fetch_one(
                    "SELECT * FROM schedules WHERE id = :id", {"id": sid}
                )
                assert sched_row2 is not None
                assert sched_row2["name"] == "fault-injection-target"
                await db2.create_schedule(
                    {
                        "id": uuid.uuid4().hex[:12],
                        "name": "post-retry-command-admit-check",
                        "trigger_type": "cron",
                        "cron_expr": "0 * * * *",
                        "action_kind": "command",
                        "action_command": "kdev",
                        "action_command_args": [],
                    }
                )
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
