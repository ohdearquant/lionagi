# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0071 D2 schema-layer tests: schedule_runs generalized into the durable
task-application entity (nullable schedule_id, widened status CHECK, ADR-0071
queue columns, CAS registration in transitions._ENTITY_TABLES).
"""

from __future__ import annotations

import time
import uuid

import aiosqlite
import pytest
from sqlalchemy import text

from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons
from lionagi.state.transitions import (
    Actor,
    StateReason,
    TransitionRequest,
    transition,
)


def _uid() -> str:
    return str(uuid.uuid4())


# A `schedules` table already at current shape (the 'flow_yaml' marker in its
# action_kind CHECK) so opening a legacy schedule_runs fixture through
# StateDB.open() doesn't also trip the pre-existing, unrelated
# _drop_legacy_action_kind_check rebuild — these tests are scoped to
# schedule_runs only.
_CURRENT_SCHEDULES_DDL = """
CREATE TABLE schedules (
  id           TEXT    PRIMARY KEY,
  name         TEXT    NOT NULL UNIQUE,
  enabled      INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
  trigger_type TEXT    NOT NULL CHECK(trigger_type IN ('cron', 'interval', 'github_poll')),
  action_kind  TEXT    NOT NULL CHECK(action_kind IN ('agent', 'flow', 'fanout', 'play', 'flow_yaml')),
  created_at   REAL    NOT NULL,
  updated_at   REAL    NOT NULL
)
"""


async def _make_schedule_run(db: StateDB, *, status: str = "running") -> tuple[str, str]:
    sched_id = _uid()
    await db.create_schedule(
        {
            "id": sched_id,
            "name": f"sched-{sched_id}",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )
    run_id = _uid()
    await db.create_schedule_run(
        {
            "id": run_id,
            "schedule_id": sched_id,
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": {},
            "status": status,
            "fired_at": time.time(),
        }
    )
    return sched_id, run_id


# ── 1. Migration idempotence ─────────────────────────────────────────────────


async def test_migration_idempotent_on_already_migrated_db(tmp_path):
    """Opening an already-migrated db a second time is a no-op: same schema,
    same rows, no error."""
    db_path = tmp_path / "already_migrated.db"

    state = StateDB(db_path)
    await state.open()
    sched_id, run_id = await _make_schedule_run(state)
    await state.close()

    # Re-open the same file — _apply_schema runs again on an already-current schema.
    state2 = StateDB(db_path)
    await state2.open()
    try:
        async with state2._read() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            "SELECT sql FROM sqlite_master WHERE type='table' AND name='schedule_runs'"
                        )
                    )
                )
                .mappings()
                .first()
            )
            assert "'waiting_dependency'" in row["sql"]

            run_row = (
                (
                    await conn.execute(
                        text("SELECT * FROM schedule_runs WHERE id = :id"), {"id": run_id}
                    )
                )
                .mappings()
                .first()
            )
        assert run_row is not None
        assert run_row["schedule_id"] == sched_id
        assert run_row["status"] == "running"
    finally:
        await state2.close()


async def test_migration_preserves_populated_pre_migration_rows(tmp_path):
    """Applying the migration to a populated pre-ADR-0071 db preserves every
    existing row's values (the rebuild copies data, not just structure)."""
    db_path = tmp_path / "legacy_populated.db"

    async with aiosqlite.connect(str(db_path)) as raw:
        await raw.execute("PRAGMA foreign_keys = ON")
        await raw.execute(_CURRENT_SCHEDULES_DDL)
        await raw.execute(
            """
            CREATE TABLE schedule_runs (
              id                  TEXT    PRIMARY KEY,
              schedule_id         TEXT    NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
              invocation_id       TEXT,
              trigger_context     JSON    NOT NULL,
              action_kind         TEXT    NOT NULL,
              action_args         JSON    NOT NULL,
              status              TEXT    NOT NULL DEFAULT 'running'
                                  CHECK(status IN ('running', 'completed', 'failed',
                                                   'skipped', 'cancelled')),
              exit_code           INTEGER,
              chain_parent_id     TEXT    REFERENCES schedule_runs(id),
              chain_depth         INTEGER NOT NULL DEFAULT 0,
              fired_at            REAL    NOT NULL,
              ended_at            REAL,
              error_detail        TEXT,
              created_at          REAL    NOT NULL,
              updated_at          REAL,
              status_reason_code     TEXT,
              status_reason_summary  TEXT,
              status_evidence_refs   JSON
            )
            """
        )
        await raw.execute(
            "INSERT INTO schedules (id, name, trigger_type, action_kind, created_at, updated_at) "
            "VALUES ('sched-1', 'sched-1', 'interval', 'agent', 1.0, 1.0)"
        )
        await raw.execute(
            "INSERT INTO schedule_runs "
            "(id, schedule_id, trigger_context, action_kind, action_args, status, "
            " exit_code, chain_depth, fired_at, ended_at, error_detail, created_at, "
            " updated_at, status_reason_code, status_reason_summary, status_evidence_refs) "
            "VALUES ('run-1', 'sched-1', '{}', 'agent', '{}', 'completed', "
            " 0, 0, 10.0, 15.0, NULL, 10.0, 15.0, 'run.completed.ok', 'ok', '[]')"
        )
        await raw.commit()

    state = StateDB(db_path)
    await state.open()
    try:
        async with state._read() as conn:
            table_sql = (
                (
                    await conn.execute(
                        text(
                            "SELECT sql FROM sqlite_master WHERE type='table' AND name='schedule_runs'"
                        )
                    )
                )
                .mappings()
                .first()
            )
            assert "'waiting_dependency'" in table_sql["sql"]

            row = (
                (await conn.execute(text("SELECT * FROM schedule_runs WHERE id = 'run-1'")))
                .mappings()
                .first()
            )
        assert row is not None
        assert row["schedule_id"] == "sched-1"
        assert row["trigger_context"] == "{}"
        assert row["action_kind"] == "agent"
        assert row["action_args"] == "{}"
        assert row["status"] == "completed"
        assert row["exit_code"] == 0
        assert row["chain_depth"] == 0
        assert row["fired_at"] == 10.0
        assert row["ended_at"] == 15.0
        assert row["error_detail"] is None
        assert row["created_at"] == 10.0
        assert row["updated_at"] == 15.0
        assert row["status_reason_code"] == "run.completed.ok"
        assert row["status_reason_summary"] == "ok"
        assert row["status_evidence_refs"] == "[]"
        # New columns exist and default to NULL for pre-existing rows.
        for col in (
            "queued_at",
            "leased_by",
            "lease_expires_at",
            "concurrency_key",
            "required_capabilities",
            "execution_target",
            "library_ref",
            "library_content_hash",
        ):
            assert row[col] is None
    finally:
        await state.close()


async def test_migration_preserves_triggers(tmp_path):
    """DROP TABLE drops the table's triggers, so the rebuild must capture and
    replay them — a legacy schedule_runs trigger survives the migration."""
    db_path = tmp_path / "legacy_with_trigger.db"

    async with aiosqlite.connect(str(db_path)) as raw:
        await raw.execute(_CURRENT_SCHEDULES_DDL)
        await raw.execute(
            """
            CREATE TABLE schedule_runs (
              id                  TEXT    PRIMARY KEY,
              schedule_id         TEXT    NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
              invocation_id       TEXT,
              trigger_context     JSON    NOT NULL,
              action_kind         TEXT    NOT NULL,
              action_args         JSON    NOT NULL,
              status              TEXT    NOT NULL DEFAULT 'running'
                                  CHECK(status IN ('running', 'completed', 'failed',
                                                   'skipped', 'cancelled')),
              exit_code           INTEGER,
              chain_parent_id     TEXT    REFERENCES schedule_runs(id),
              chain_depth         INTEGER NOT NULL DEFAULT 0,
              fired_at            REAL    NOT NULL,
              ended_at            REAL,
              error_detail        TEXT,
              created_at          REAL    NOT NULL,
              updated_at          REAL,
              status_reason_code     TEXT,
              status_reason_summary  TEXT,
              status_evidence_refs   JSON
            )
            """
        )
        await raw.execute("CREATE TABLE trigger_log (run_id TEXT)")
        await raw.execute(
            """
            CREATE TRIGGER schedule_runs_audit AFTER INSERT ON schedule_runs
            BEGIN
              INSERT INTO trigger_log (run_id) VALUES (NEW.id);
            END
            """
        )
        await raw.commit()

    state = StateDB(db_path)
    await state.open()
    try:
        async with state._read() as conn:
            trig = (
                (
                    await conn.execute(
                        text(
                            "SELECT sql FROM sqlite_master "
                            "WHERE type='trigger' AND name='schedule_runs_audit'"
                        )
                    )
                )
                .mappings()
                .first()
            )
        assert trig is not None, "trigger dropped by the schedule_runs rebuild"
        assert "trigger_log" in trig["sql"]
    finally:
        await state.close()

    # The replayed trigger still fires on insert.
    async with aiosqlite.connect(str(db_path)) as raw:
        await raw.execute(
            "INSERT INTO schedules (id, name, trigger_type, action_kind, created_at, updated_at) "
            "VALUES ('sched-t', 'sched-t', 'interval', 'agent', 1.0, 1.0)"
        )
        await raw.execute(
            "INSERT INTO schedule_runs "
            "(id, schedule_id, trigger_context, action_kind, action_args, status, "
            " chain_depth, fired_at, created_at) "
            "VALUES ('run-t', 'sched-t', '{}', 'agent', '{}', 'running', 0, 1.0, 1.0)"
        )
        await raw.commit()
        cur = await raw.execute("SELECT run_id FROM trigger_log WHERE run_id = 'run-t'")
        logged = await cur.fetchone()
    assert logged is not None, "replayed trigger did not fire on insert"


async def test_migration_adds_lease_attempts_column_to_legacy_db(tmp_path):
    """A schedule_runs table already at ADR-0071 D2 shape (all D2 columns
    present) but pre-dating D3's lease_attempts column gains it additively —
    no rebuild, just StateDB._reconcile_columns's ALTER TABLE ADD COLUMN."""
    db_path = tmp_path / "legacy_no_lease_attempts.db"

    async with aiosqlite.connect(str(db_path)) as raw:
        await raw.execute(_CURRENT_SCHEDULES_DDL)
        await raw.execute(
            """
            CREATE TABLE schedule_runs (
              id                  TEXT    PRIMARY KEY,
              schedule_id         TEXT    REFERENCES schedules(id) ON DELETE CASCADE,
              invocation_id       TEXT,
              trigger_context     JSON    NOT NULL,
              action_kind         TEXT    NOT NULL,
              action_args         JSON    NOT NULL,
              status              TEXT    NOT NULL DEFAULT 'running'
                                  CHECK(status IN ('queued', 'waiting_dependency', 'running',
                                                   'retry_wait', 'completed', 'failed',
                                                   'timed_out', 'skipped', 'cancelled')),
              exit_code           INTEGER,
              chain_parent_id     TEXT    REFERENCES schedule_runs(id),
              chain_depth         INTEGER NOT NULL DEFAULT 0,
              fired_at            REAL    NOT NULL,
              ended_at            REAL,
              error_detail        TEXT,
              created_at          REAL    NOT NULL,
              updated_at          REAL,
              status_reason_code     TEXT,
              status_reason_summary  TEXT,
              status_evidence_refs   JSON,
              queued_at           REAL,
              leased_by           TEXT,
              lease_expires_at    REAL,
              concurrency_key     TEXT,
              required_capabilities  JSON,
              execution_target       TEXT,
              library_ref             TEXT,
              library_content_hash    TEXT
            )
            """
        )
        await raw.execute(
            "INSERT INTO schedules (id, name, trigger_type, action_kind, created_at, updated_at) "
            "VALUES ('sched-la', 'sched-la', 'interval', 'agent', 1.0, 1.0)"
        )
        await raw.execute(
            "INSERT INTO schedule_runs "
            "(id, schedule_id, trigger_context, action_kind, action_args, status, "
            " chain_depth, fired_at, created_at) "
            "VALUES ('run-la', 'sched-la', '{}', 'agent', '{}', 'running', 0, 1.0, 1.0)"
        )
        await raw.commit()

    state = StateDB(db_path)
    await state.open()
    try:
        row = await state.fetch_one("SELECT * FROM schedule_runs WHERE id = 'run-la'")
        assert row is not None
        assert row["lease_attempts"] == 0
    finally:
        await state.close()


# ── 2. Backup before rebuild ─────────────────────────────────────────────────


async def test_backup_created_before_rebuild(tmp_path):
    """A pre-rebuild backup file of state.db is created when the legacy
    schedule_runs CHECK is detected and the table is rebuilt."""
    db_path = tmp_path / "legacy_for_backup.db"

    async with aiosqlite.connect(str(db_path)) as raw:
        await raw.execute(_CURRENT_SCHEDULES_DDL)
        await raw.execute(
            """
            CREATE TABLE schedule_runs (
              id                  TEXT    PRIMARY KEY,
              schedule_id         TEXT    NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
              invocation_id       TEXT,
              trigger_context     JSON    NOT NULL,
              action_kind         TEXT    NOT NULL,
              action_args         JSON    NOT NULL,
              status              TEXT    NOT NULL DEFAULT 'running'
                                  CHECK(status IN ('running', 'completed', 'failed',
                                                   'skipped', 'cancelled')),
              exit_code           INTEGER,
              chain_parent_id     TEXT    REFERENCES schedule_runs(id),
              chain_depth         INTEGER NOT NULL DEFAULT 0,
              fired_at            REAL    NOT NULL,
              ended_at            REAL,
              error_detail        TEXT,
              created_at          REAL    NOT NULL
            )
            """
        )
        await raw.commit()

    pre_open_files = set(tmp_path.iterdir())

    state = StateDB(db_path)
    await state.open()
    await state.close()

    post_open_files = set(tmp_path.iterdir())
    new_files = post_open_files - pre_open_files
    backups = [p for p in new_files if "schedule_runs" in p.name and p.name.endswith(".bak")]
    assert len(backups) == 1, f"expected exactly one backup file, got {backups}"


async def test_backup_includes_wal_only_committed_rows(tmp_path):
    """The pre-rebuild backup must checkpoint WAL before copying: a row
    committed in WAL mode but never checkpointed must still show up in the
    backup file, not just in the live db once it eventually merges."""
    db_path = tmp_path / "legacy_for_wal_backup.db"

    async with aiosqlite.connect(str(db_path)) as raw:
        await raw.execute("PRAGMA journal_mode=WAL")
        await raw.execute(_CURRENT_SCHEDULES_DDL)
        await raw.execute(
            """
            CREATE TABLE schedule_runs (
              id                  TEXT    PRIMARY KEY,
              schedule_id         TEXT    NOT NULL REFERENCES schedules(id) ON DELETE CASCADE,
              invocation_id       TEXT,
              trigger_context     JSON    NOT NULL,
              action_kind         TEXT    NOT NULL,
              action_args         JSON    NOT NULL,
              status              TEXT    NOT NULL DEFAULT 'running'
                                  CHECK(status IN ('running', 'completed', 'failed',
                                                   'skipped', 'cancelled')),
              exit_code           INTEGER,
              chain_parent_id     TEXT    REFERENCES schedule_runs(id),
              chain_depth         INTEGER NOT NULL DEFAULT 0,
              fired_at            REAL    NOT NULL,
              ended_at            REAL,
              error_detail        TEXT,
              created_at          REAL    NOT NULL
            )
            """
        )
        await raw.execute(
            "INSERT INTO schedules (id, name, trigger_type, action_kind, created_at, updated_at) "
            "VALUES ('sched-wal', 'sched-wal', 'interval', 'agent', 1.0, 1.0)"
        )
        await raw.commit()

    # Hold an idle connection open across the writer's close so SQLite's
    # checkpoint-on-last-connection-close doesn't fire and merge the WAL back
    # into the main db file before StateDB.open() gets a chance to run.
    holder = await aiosqlite.connect(str(db_path))
    await holder.execute("PRAGMA journal_mode=WAL")

    writer = await aiosqlite.connect(str(db_path))
    await writer.execute("PRAGMA journal_mode=WAL")
    await writer.execute(
        "INSERT INTO schedule_runs "
        "(id, schedule_id, trigger_context, action_kind, action_args, status, "
        " chain_depth, fired_at, created_at) "
        "VALUES ('run-wal-only', 'sched-wal', '{}', 'agent', '{}', 'running', 0, 1.0, 1.0)"
    )
    await writer.commit()
    await writer.close()

    wal_path = db_path.with_name(db_path.name + "-wal")
    assert wal_path.exists() and wal_path.stat().st_size > 0, (
        "test setup invalid: committed row must still be sitting in the WAL sidecar"
    )

    pre_open_files = set(tmp_path.iterdir())

    state = StateDB(db_path)
    await state.open()
    await state.close()
    await holder.close()

    post_open_files = set(tmp_path.iterdir())
    new_files = post_open_files - pre_open_files
    backups = [p for p in new_files if "schedule_runs" in p.name and p.name.endswith(".bak")]
    assert len(backups) == 1, f"expected exactly one backup file, got {backups}"

    async with aiosqlite.connect(str(backups[0])) as check:
        cur = await check.execute("SELECT id FROM schedule_runs WHERE id = 'run-wal-only'")
        row = await cur.fetchone()
    assert row is not None, (
        "backup is missing a row that was only in the WAL sidecar at backup time"
    )


async def test_backup_not_created_when_no_rebuild_needed(tmp_path):
    """No backup file is left behind opening a db that already carries the
    current schema — the backup only fires on an actual rebuild."""
    db_path = tmp_path / "fresh.db"

    state = StateDB(db_path)
    await state.open()
    await state.close()

    pre_reopen_files = set(tmp_path.iterdir())

    state2 = StateDB(db_path)
    await state2.open()
    await state2.close()

    post_reopen_files = set(tmp_path.iterdir())
    new_files = post_reopen_files - pre_reopen_files
    backups = [p for p in new_files if p.name.endswith(".bak")]
    assert backups == []


# ── 3. Transition-vocab enforcement via the CAS transition store ────────────


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


async def test_schedule_run_registered_in_cas_entity_tables():
    from lionagi.state.transitions import _ENTITY_TABLES

    assert _ENTITY_TABLES["schedule_run"] == "schedule_runs"


async def test_transition_store_governs_schedule_run_status(db: StateDB) -> None:
    _sched_id, run_id = await _make_schedule_run(db, status="running")

    result = await transition(
        db,
        TransitionRequest(
            entity_type="schedule_run",
            entity_id=run_id,
            from_state="running",
            to_state="completed",
            reason=StateReason(code="run.completed.ok", summary="via CAS store"),
            actor=Actor(type="system", id="test"),
            idempotency_key=_uid(),
        ),
    )
    assert result.applied is True
    assert result.previous_state == "running"
    assert result.current_state == "completed"

    row = await db.fetch_one("SELECT status FROM schedule_runs WHERE id = ?", (run_id,))
    assert row["status"] == "completed"

    audit_rows = await db.fetch_all(
        "SELECT previous_status, status, entity_type FROM status_transitions WHERE entity_id = ?",
        (run_id,),
    )
    assert len(audit_rows) == 1
    assert audit_rows[0]["previous_status"] == "running"
    assert audit_rows[0]["status"] == "completed"
    assert audit_rows[0]["entity_type"] == "schedule_run"


async def test_transition_store_rejects_mismatched_from_state(db: StateDB) -> None:
    _sched_id, run_id = await _make_schedule_run(db, status="running")

    result = await transition(
        db,
        TransitionRequest(
            entity_type="schedule_run",
            entity_id=run_id,
            from_state="completed",  # wrong — row is still "running"
            to_state="failed",
            reason=StateReason(code="run.failed.exit_nonzero"),
            actor=Actor(type="system", id="test"),
            idempotency_key=_uid(),
        ),
    )
    assert result.applied is False
    assert result.conflict is True
    assert result.previous_state == "running"

    row = await db.fetch_one("SELECT status FROM schedule_runs WHERE id = ?", (run_id,))
    assert row["status"] == "running"  # untouched


async def test_transition_store_rejects_unknown_reason_code(db: StateDB) -> None:
    _sched_id, run_id = await _make_schedule_run(db, status="running")

    with pytest.raises(ValueError, match="invalid reason_code"):
        await transition(
            db,
            TransitionRequest(
                entity_type="schedule_run",
                entity_id=run_id,
                from_state="running",
                to_state="completed",
                reason=StateReason(code="not.a.real_code"),
                actor=Actor(type="system", id="test"),
                idempotency_key=_uid(),
            ),
        )


async def test_transition_allows_declared_guard_and_patch_columns(db: StateDB) -> None:
    _sched_id, run_id = await _make_schedule_run(db, status="running")
    await db.execute(
        "UPDATE schedule_runs SET leased_by = :leased_by WHERE id = :id",
        {"leased_by": "worker-a", "id": run_id},
    )

    result = await transition(
        db,
        TransitionRequest(
            entity_type="schedule_run",
            entity_id=run_id,
            from_state="running",
            to_state="failed",
            reason=StateReason(code="run.failed.exit_nonzero"),
            actor=Actor(type="system", id="test"),
            idempotency_key=_uid(),
        ),
        guard={"leased_by": "worker-a"},
        patch={"leased_by": "worker-a", "lease_expires_at": 123.0, "lease_attempts": 2},
    )
    assert result.applied is True

    row = await db.fetch_one(
        "SELECT leased_by, lease_expires_at, lease_attempts FROM schedule_runs WHERE id = ?",
        (run_id,),
    )
    assert row["leased_by"] == "worker-a"
    assert row["lease_expires_at"] == 123.0
    assert row["lease_attempts"] == 2


async def test_transition_rejects_unknown_guard_column(db: StateDB) -> None:
    _sched_id, run_id = await _make_schedule_run(db, status="running")

    with pytest.raises(ValueError, match="guard.*status_reason_code"):
        await transition(
            db,
            TransitionRequest(
                entity_type="schedule_run",
                entity_id=run_id,
                from_state="running",
                to_state="failed",
                reason=StateReason(code="run.failed.exit_nonzero"),
                actor=Actor(type="system", id="test"),
                idempotency_key=_uid(),
            ),
            guard={"status_reason_code": "whatever"},
        )

    row = await db.fetch_one("SELECT status FROM schedule_runs WHERE id = ?", (run_id,))
    assert row["status"] == "running"  # rejected before any SQL ran


async def test_transition_rejects_unknown_patch_column(db: StateDB) -> None:
    _sched_id, run_id = await _make_schedule_run(db, status="running")

    with pytest.raises(ValueError, match="patch.*action_args"):
        await transition(
            db,
            TransitionRequest(
                entity_type="schedule_run",
                entity_id=run_id,
                from_state="running",
                to_state="completed",
                reason=StateReason(code="run.completed.ok"),
                actor=Actor(type="system", id="test"),
                idempotency_key=_uid(),
            ),
            patch={"action_args": '{"evil": true}'},
        )

    row = await db.fetch_one("SELECT status FROM schedule_runs WHERE id = ?", (run_id,))
    assert row["status"] == "running"  # rejected before any SQL ran


# ── 4. Load-bearing: schedule-spawned runs stay byte-identical ──────────────
#
# Golden values captured by running the ORIGINAL (pre-ADR-0071) codebase
# through the exact same operations below (create_schedule_run then
# update_schedule_run through _route_status_change/update_status), on a
# schema without this change. `id`, `schedule_id`, and `created_at` are
# caller-generated/wall-clock and excluded from the golden dict; every other
# column produced by the existing write path must match exactly.

_GOLDEN_ROW_AFTER_CREATE = {
    "action_args": '{"prompt": "hello"}',
    "action_kind": "agent",
    "chain_depth": 0,
    "chain_parent_id": None,
    "ended_at": None,
    "error_detail": None,
    "exit_code": None,
    "fired_at": 1700000000.0,
    "invocation_id": None,
    "status": "running",
    "status_evidence_refs": None,
    "status_reason_code": None,
    "status_reason_summary": None,
    "trigger_context": '{"trigger": "interval"}',
}

_GOLDEN_ROW_AFTER_UPDATE = {
    "action_args": '{"prompt": "hello"}',
    "action_kind": "agent",
    "chain_depth": 0,
    "chain_parent_id": None,
    "ended_at": 1700000005.0,
    "error_detail": None,
    "exit_code": 0,
    "fired_at": 1700000000.0,
    "invocation_id": None,
    "status": "completed",
    "status_evidence_refs": "[]",
    "status_reason_code": "run.completed.ok",
    "status_reason_summary": "ok",
    "trigger_context": '{"trigger": "interval"}',
}

_GOLDEN_TRANSITIONS = [
    {
        "entity_type": "schedule_run",
        "previous_status": "running",
        "status": "completed",
        "reason_code": "run.completed.ok",
        "reason_summary": "ok",
        "source": "executor",
        "actor": None,
    }
]

_EXCLUDED_COLUMNS = {
    "id",
    "schedule_id",
    "created_at",
    "updated_at",
    # ADR-0071 D2 additive columns — asserted separately (must be NULL for a
    # schedule-spawned run); excluded here so the golden dicts captured from
    # the pre-ADR-0071 codebase (which never had these columns) still apply.
    "queued_at",
    "leased_by",
    "lease_expires_at",
    "concurrency_key",
    "required_capabilities",
    "execution_target",
    "library_ref",
    "library_content_hash",
    # ADR-0071 D4 additive column — asserted separately (must default to 0
    # for a schedule-spawned run); excluded here for the same reason as the
    # D2 columns above.
    "lease_attempts",
    # Delivery-contract marker (see schema.sql) — additive, asserted
    # separately (must be NULL for a schedule-spawned run that hasn't
    # confirmed dispatch); excluded here for the same reason as the D2
    # columns above.
    "dispatched_at",
    # Resume-packet sidecar metadata blob — additive, asserted separately
    # (must be NULL for a schedule-spawned run that never set it); excluded
    # here for the same reason as the D2 columns above.
    "resume_packet",
}


def _strip(row: dict) -> dict:
    return {k: v for k, v in dict(row).items() if k not in _EXCLUDED_COLUMNS}


async def test_schedule_spawned_run_stays_byte_identical(db: StateDB) -> None:
    """The schedule_id-populated path (create_schedule_run + update_schedule_run,
    i.e. an ordinary schedule fire) writes the same rows with the same column
    values, and produces the same status_transitions sequence, as the
    pre-ADR-0071 code — this codepath was not touched by the schema
    generalization; new queue/task columns simply default to NULL.
    """
    sched_id = _uid()
    await db.create_schedule(
        {
            "id": sched_id,
            "name": f"sched-{sched_id}",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )

    run_id = _uid()
    fired_at = 1700000000.0
    await db.create_schedule_run(
        {
            "id": run_id,
            "schedule_id": sched_id,
            "trigger_context": {"trigger": "interval"},
            "action_kind": "agent",
            "action_args": {"prompt": "hello"},
            "status": "running",
            "fired_at": fired_at,
        }
    )

    row_after_create = await db.fetch_one("SELECT * FROM schedule_runs WHERE id = ?", (run_id,))
    assert _strip(row_after_create) == _GOLDEN_ROW_AFTER_CREATE
    # New ADR-0071 queue/task columns are present on the row and default to NULL
    # for a schedule-spawned run — they are additive, not a behavior change.
    for col in (
        "queued_at",
        "leased_by",
        "lease_expires_at",
        "concurrency_key",
        "required_capabilities",
        "execution_target",
        "library_ref",
        "library_content_hash",
        "dispatched_at",
        "resume_packet",
    ):
        assert row_after_create[col] is None
    # ADR-0071 D4 additive column — defaults to 0, not NULL.
    assert row_after_create["lease_attempts"] == 0

    await db.update_schedule_run(
        run_id,
        status="completed",
        exit_code=0,
        ended_at=fired_at + 5,
        reason_code=RunReasons.COMPLETED_OK,
        reason_summary="ok",
    )

    row_after_update = await db.fetch_one("SELECT * FROM schedule_runs WHERE id = ?", (run_id,))
    assert _strip(row_after_update) == _GOLDEN_ROW_AFTER_UPDATE

    transitions = await db.fetch_all(
        "SELECT entity_type, entity_id, previous_status, status, reason_code, "
        "reason_summary, source, actor FROM status_transitions "
        "WHERE entity_id = ? ORDER BY created_at",
        (run_id,),
    )
    stripped_transitions = [
        {k: v for k, v in dict(t).items() if k != "entity_id"} for t in transitions
    ]
    assert stripped_transitions == _GOLDEN_TRANSITIONS
