# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Compatibility-mode StateDB (ADR-0128 D1, D2, D3, D6) against a synthetic managed store."""

from __future__ import annotations

import hashlib
import pickle
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy import text

import lionagi.state as state_pkg
from lionagi.state import db as db_mod
from lionagi.state.compat import (
    _SQLITE_AUTH,
    CompatibilityProfile,
    StateCompatibilityRefused,
    TableWrites,
    _operation,
    _sql_tokens,
    _statement_kind,
    _write_refusal,
    read_closure,
)
from lionagi.state.db import StateDB

# A store another application manages: lionagi's operations write runs and events, never other.
_SCHEMA = """
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL DEFAULT 'managed',
    status TEXT NOT NULL CHECK (status IN ('running', 'done')),
    note TEXT,
    updated_at REAL
);
CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    body TEXT NOT NULL,
    UNIQUE (run_id, body)
);
CREATE TABLE other (id TEXT PRIMARY KEY, value TEXT);
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO schema_meta VALUES ('version', '14');
INSERT INTO other VALUES ('keep', 'untouched');
"""

_WRITES = {
    "runs": TableWrites(
        insert={"id", "status", "note", "updated_at"},
        update={"status", "updated_at"},
        unwritten={"owner": "the managing application's default names the owner"},
    ),
    "events": TableWrites(insert={"run_id", "body"}, unwritten={"seq": "SQLite assigns the rowid"}),
}

_OPERATIONS = {
    "start_run",
    "finish_run",
    "run_sql",
    "two_phase",
    "caught",
    "raw_commit",
    "autocommit_write",
    "rolled_back",
}


def _store(path: Path, schema: str = _SCHEMA, *, journal: str = "wal") -> Path:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(f"PRAGMA journal_mode = {journal}")
        conn.executescript(schema)
    return path


def _profile(reference: Path, **changes) -> CompatibilityProfile:
    with closing(sqlite3.connect(reference)) as conn:
        closure = read_closure(conn, ["runs", "events"])
    fields = {
        "name": "synthetic",
        "version": 1,
        "closure": closure,
        "writes": _WRITES,
        "operations": _OPERATIONS,
    }
    fields.update(changes)
    return CompatibilityProfile(**fields)


def _rows(path: Path, sql: str) -> list:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchall()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _RunsDB(StateDB):
    """The operations a synthetic profile names, written the way StateDB operations are."""

    entered: list[str]

    @_operation("start_run")
    async def start_run(self, run_id: str) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text(
                    "INSERT INTO runs (id, status, note, updated_at) "
                    "VALUES (:id, 'running', NULL, 1.0)"
                ),
                {"id": run_id},
            )
            await conn.execute(
                text("INSERT INTO events (run_id, body) VALUES (:id, 'started')"), {"id": run_id}
            )

    @_operation("finish_run")
    async def finish_run(self, run_id: str) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text("UPDATE runs SET status = 'done', updated_at = 2.0 WHERE id = :id"),
                {"id": run_id},
            )

    @_operation("run_sql")
    async def run_sql(self, sql: str, params: dict | None = None) -> None:
        async with self._tx() as conn:
            await conn.execute(text(sql), params or {})

    @_operation("two_phase")
    async def two_phase(self, run_id: str, second: str) -> None:
        await self.start_run(run_id)
        async with self._tx() as conn:
            await conn.execute(text(second))

    @_operation("caught")
    async def caught(self, run_id: str, denied: str) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text("INSERT INTO runs (id, status) VALUES (:id, 'running')"), {"id": run_id}
            )
            with pytest.raises(StateCompatibilityRefused):
                await conn.execute(text(denied))

    @_operation("raw_commit")
    async def raw_commit(self) -> int:
        refused = 0
        async with self._engine.connect() as conn:
            # A clean transaction committed by a raw COMMIT first, so that statement would sit
            # in the driver's statement cache if the cache were on.
            await conn.begin()
            await conn.execute(text("INSERT INTO runs (id, status) VALUES ('r0', 'running')"))
            await conn.exec_driver_sql("COMMIT")
            await conn.rollback()
            await conn.begin()
            await conn.execute(text("INSERT INTO runs (id, status) VALUES ('r1', 'running')"))
            with pytest.raises(StateCompatibilityRefused):
                await conn.execute(text("UPDATE runs SET note = 'x'"))
            for _ in range(2):
                with pytest.raises(StateCompatibilityRefused, match="COMMIT"):
                    await conn.exec_driver_sql("COMMIT")
                refused += 1
            await conn.rollback()
        return refused

    @_operation("autocommit_write")
    async def autocommit_write(self, run_id: str) -> None:
        async with self._read() as conn:
            await conn.execute(
                text("INSERT INTO runs (id, status) VALUES (:id, 'running')"), {"id": run_id}
            )

    @_operation("rolled_back")
    async def rolled_back(self, run_id: str) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text("INSERT INTO runs (id, status) VALUES (:id, 'running')"), {"id": run_id}
            )
            driver = (await conn.get_raw_connection()).driver_connection
            # Past the statement check: a conflict SQLite resolves by rolling the transaction back.
            with pytest.raises(sqlite3.IntegrityError):
                await driver.execute(
                    "INSERT OR ROLLBACK INTO runs (id, status) VALUES (?, 'running')", (run_id,)
                )
            await conn.execute(text("INSERT INTO runs (id, status) VALUES ('after', 'running')"))

    @_operation("not_in_profile")
    async def not_in_profile(self) -> None:
        self.entered.append("not_in_profile")


@pytest.fixture
def reference(tmp_path: Path) -> Path:
    return _store(tmp_path / "reference.db")


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return _store(tmp_path / "managed.db")


async def _open(path: Path, profile: CompatibilityProfile) -> _RunsDB:
    db = _RunsDB(path, compatibility=profile)
    db.entered = []
    await db.open()
    return db


async def _raw(db: StateDB, sql: str) -> list:
    """Run *sql* on the driver connection, past every SQLAlchemy listener."""
    async with db._engine.connect() as conn:
        driver = (await conn.get_raw_connection()).driver_connection
        cursor = await driver.execute(sql)
        return list(await cursor.fetchall())


# Admission (D1, D2)


async def test_a_matching_store_is_admitted_and_never_migrated(store, reference):
    before = _rows(store, "SELECT type, name, sql FROM sqlite_master ORDER BY name")
    db = await _open(store, _profile(reference))
    try:
        assert db.mode == "compatibility"
        await db.start_run("r1")
        await db.finish_run("r1")
    finally:
        await db.close()
    assert _rows(store, "SELECT type, name, sql FROM sqlite_master ORDER BY name") == before
    assert _rows(store, "SELECT id, owner, status, updated_at FROM runs") == [
        ("r1", "managed", "done", 2.0)
    ]
    assert _rows(store, "SELECT run_id, body FROM events") == [("r1", "started")]
    assert _rows(store, "SELECT value FROM schema_meta WHERE key = 'version'") == [("14",)]


_ALTERED = {
    "missing table": ("DROP TABLE events;", "table events is missing"),
    "extra column": ("ALTER TABLE runs ADD COLUMN extra TEXT;", "column runs.extra"),
    "generated column": (
        "ALTER TABLE runs ADD COLUMN g TEXT GENERATED ALWAYS AS (status) VIRTUAL;",
        "column runs.g",
    ),
    "incoming foreign key": (
        "CREATE TABLE notes (run_id TEXT REFERENCES runs(id) ON DELETE CASCADE);",
        "foreign key not in the profile",
    ),
    "trigger": (
        "CREATE TRIGGER touch AFTER INSERT ON runs BEGIN UPDATE other SET value = 'x'; END;",
        "trigger touch",
    ),
    "view": ("CREATE VIEW open_runs AS SELECT id FROM runs WHERE status = 'running';", "view"),
    "unique index": ("CREATE UNIQUE INDEX one_note ON runs (note);", "unique of runs"),
}


@pytest.mark.parametrize("change", sorted(_ALTERED))
async def test_a_store_that_differs_from_the_profile_is_refused(change, store, reference):
    ddl, expected = _ALTERED[change]
    with closing(sqlite3.connect(store)) as conn:
        conn.executescript(ddl)
    digest = _digest(store)
    db = _RunsDB(store, compatibility=_profile(reference))
    with pytest.raises(StateCompatibilityRefused, match=expected) as caught:
        await db.open()
    assert caught.value.operation == "open"
    assert caught.value.committed == ()
    assert db._engine is None
    assert _digest(store) == digest


_REBUILT = {
    "column type": ("updated_at REAL", "updated_at TEXT"),
    "default": ("DEFAULT 'managed'", "DEFAULT 'other'"),
    "not null": ("note TEXT,", "note TEXT NOT NULL DEFAULT '',"),
    "check": ("'running', 'done'", "'running', 'done', 'lost'"),
    "outgoing foreign key": ("REFERENCES runs(id)", ""),
    "unique key": ("UNIQUE (run_id, body)", "UNIQUE (body, run_id)"),
    "without rowid": ("    updated_at REAL\n);", "    updated_at REAL\n) WITHOUT ROWID;"),
    "conflict replace": ("id TEXT PRIMARY KEY,", "id TEXT PRIMARY KEY ON CONFLICT REPLACE,"),
    "conflict rollback": ("UNIQUE (run_id, body)", "UNIQUE (run_id, body) ON CONFLICT ROLLBACK"),
}


@pytest.mark.parametrize("change", sorted(_REBUILT))
async def test_a_table_built_differently_is_refused(change, tmp_path, reference):
    old, new = _REBUILT[change]
    assert old in _SCHEMA
    store = _store(tmp_path / "rebuilt.db", _SCHEMA.replace(old, new, 1))
    db = _RunsDB(store, compatibility=_profile(reference))
    with pytest.raises(StateCompatibilityRefused, match="does not match the profile"):
        await db.open()
    assert db._engine is None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("id TEXT PRIMARY KEY,", "id TEXT PRIMARY KEY ON CONFLICT REPLACE,"),
        ("UNIQUE (run_id, body)", "UNIQUE (run_id, body) ON CONFLICT ROLLBACK"),
        ("body TEXT NOT NULL,", "body TEXT NOT NULL ON CONFLICT IGNORE,"),
        ("UNIQUE (run_id, body)", "UNIQUE (run_id, body) ON CONFLICT FAIL"),
    ],
    ids=["replace", "rollback", "ignore", "fail"],
)
async def test_a_profile_whose_tables_resolve_conflicts_is_refused(old, new, tmp_path):
    schema = _SCHEMA.replace(old, new, 1)
    reference = _store(tmp_path / "reference.db", schema)
    store = _store(tmp_path / "managed.db", schema)
    db = _RunsDB(store, compatibility=_profile(reference))
    with pytest.raises(StateCompatibilityRefused, match="admits only ABORT") as caught:
        await db.open()
    assert f"ON CONFLICT {new.rsplit(' ', 1)[-1].rstrip(',')}" in caught.value.reason
    assert db._engine is None


async def test_an_explicit_on_conflict_abort_is_only_a_spelling(tmp_path, reference):
    store = _store(
        tmp_path / "abort.db",
        _SCHEMA.replace("id TEXT PRIMARY KEY,", "id TEXT PRIMARY KEY ON CONFLICT ABORT,", 1),
    )
    db = await _open(store, _profile(reference))
    await db.close()


async def test_spelling_differences_alone_do_not_refuse(tmp_path, reference):
    respelled = _SCHEMA.replace(
        "CHECK (status IN ('running', 'done'))", "check(  \"status\" in ('running','done'))"
    ).replace("TEXT PRIMARY KEY,\n    owner", "text   PRIMARY KEY, -- the run\n    owner")
    store = _store(tmp_path / "respelled.db", respelled)
    db = await _open(store, _profile(reference))
    await db.close()


async def test_a_store_not_in_wal_mode_is_refused_and_left_in_its_mode(tmp_path, reference):
    store = _store(tmp_path / "rollback-journal.db", journal="delete")
    db = _RunsDB(store, compatibility=_profile(reference))
    with pytest.raises(StateCompatibilityRefused, match="journal_mode is delete"):
        await db.open()
    assert db._engine is None
    assert _rows(store, "PRAGMA journal_mode") == [("delete",)]


async def test_a_missing_store_is_refused_and_never_created(tmp_path, reference):
    missing = tmp_path / "absent.db"
    db = _RunsDB(missing, compatibility=_profile(reference))
    with pytest.raises(StateCompatibilityRefused, match="never creates one"):
        await db.open()
    assert not missing.exists()


async def test_an_in_memory_store_is_refused(reference):
    db = _RunsDB(":memory:", compatibility=_profile(reference))
    with pytest.raises(StateCompatibilityRefused, match="existing store file"):
        await db.open()


def test_compatibility_needs_sqlite_and_is_not_readonly(reference):
    profile = _profile(reference)
    with pytest.raises(StateCompatibilityRefused, match="postgresql is refused"):
        StateDB(url="postgresql+asyncpg://localhost/x", compatibility=profile)
    with pytest.raises(ValueError, match="separate open modes"):
        StateDB(reference, readonly=True, compatibility=profile)
    with pytest.raises(TypeError, match="CompatibilityProfile"):
        StateDB(reference, compatibility="synthetic")


async def test_a_store_version_narrows_but_never_admits(store, reference, tmp_path):
    narrowed_away = _profile(reference, store_versions={"13"})
    db = _RunsDB(store, compatibility=narrowed_away)
    with pytest.raises(StateCompatibilityRefused, match="store version '14'"):
        await db.open()
    db = await _open(store, _profile(reference, store_versions={"14"}))
    await db.close()
    # The right version on the wrong structure is still refused.
    with closing(sqlite3.connect(store)) as conn:
        conn.execute("ALTER TABLE runs ADD COLUMN extra TEXT")
    db = _RunsDB(store, compatibility=_profile(reference, store_versions={"14"}))
    with pytest.raises(StateCompatibilityRefused, match="column runs.extra"):
        await db.open()


# Enforcement (D3)


async def test_generic_sql_reads_and_refuses_everything_else(store, reference):
    db = await _open(store, _profile(reference))
    try:
        assert await db.fetch_all("SELECT id, value FROM other") == [
            {"id": "keep", "value": "untouched"}
        ]
        for statement in (
            "DELETE FROM other",
            "UPDATE runs SET status = 'done'",
            "WITH x AS (SELECT 1) DELETE FROM other",
            "/* SELECT */ DELETE FROM other",
            "PRAGMA journal_mode = DELETE",
        ):
            with pytest.raises(StateCompatibilityRefused, match="not a read"):
                await db.fetch_all(statement)
            with pytest.raises(StateCompatibilityRefused, match="not a read"):
                await db.fetch_one(statement)
        with pytest.raises(StateCompatibilityRefused, match="read with fetch_all"):
            await db.execute("INSERT INTO runs (id, status) VALUES ('g', 'running')")
        with pytest.raises(StateCompatibilityRefused, match="read_snapshot"):
            db.transaction()
        with pytest.raises(StateCompatibilityRefused, match="inside an operation") as caught:
            async with db._tx():
                pass
        assert caught.value.operation == "transaction"
    finally:
        await db.close()
    assert _rows(store, "SELECT * FROM other") == [("keep", "untouched")]
    assert _rows(store, "SELECT count(*) FROM runs") == [(0,)]


async def test_an_operation_the_profile_does_not_name_never_runs(store, reference):
    db = await _open(store, _profile(reference))
    try:
        with pytest.raises(StateCompatibilityRefused, match="does not name") as caught:
            await db.not_in_profile()
        assert caught.value.operation == "not_in_profile"
        assert db.entered == []
    finally:
        await db.close()


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        ("INSERT OR REPLACE INTO runs (id, status) VALUES ('r1', 'done')", "OR REPLACE"),
        ("REPLACE INTO runs (id, status) VALUES ('r1', 'done')", "REPLACE is not"),
        ("UPDATE OR REPLACE runs SET status = 'done'", "OR REPLACE"),
        ("INSERT OR ROLLBACK INTO runs (id, status) VALUES ('r1', 'done')", "OR ROLLBACK"),
        ("INSERT OR IGNORE INTO runs (id, status) VALUES ('r1', 'done')", "OR IGNORE"),
        ("UPDATE OR FAIL runs SET status = 'done'", "OR FAIL"),
        ("INSERT INTO runs (id, status) VALUES ('r2', 'running') RETURNING id", "RETURNING"),
        ("DELETE FROM runs", "DELETE is not"),
        ("CREATE TEMP TABLE scratch (x)", "CREATE is not"),
        ("ATTACH DATABASE ':memory:' AS side", "ATTACH is not"),
        ("PRAGMA foreign_keys = OFF", "PRAGMA is not"),
        ("SAVEPOINT s1", "savepoints"),
        ("UPDATE runs SET note = 'x'", "authorizer denied UPDATE of main.runs.note"),
        (
            "INSERT INTO other (id, value) VALUES ('new', 'x')",
            "authorizer denied INSERT INTO main.other",
        ),
    ],
)
async def test_an_operation_cannot_write_outside_its_profile(statement, reason, store, reference):
    db = await _open(store, _profile(reference))
    try:
        await db.start_run("r1")
        with pytest.raises(StateCompatibilityRefused, match=reason) as caught:
            await db.run_sql(statement)
        assert caught.value.operation == "run_sql"
        assert caught.value.committed == ()
    finally:
        await db.close()
    assert _rows(store, "SELECT id, status, note FROM runs") == [("r1", "running", None)]
    assert _rows(store, "SELECT * FROM other") == [("keep", "untouched")]


async def test_an_or_abort_conflict_clause_is_admitted(store, reference):
    db = await _open(store, _profile(reference))
    try:
        await db.run_sql("INSERT OR ABORT INTO runs (id, status) VALUES ('r1', 'running')")
    finally:
        await db.close()
    assert _rows(store, "SELECT id, status FROM runs") == [("r1", "running")]


async def test_a_write_after_sqlite_rolled_the_transaction_back_is_refused(store, reference):
    db = await _open(store, _profile(reference))
    try:
        with pytest.raises(StateCompatibilityRefused, match="SQLite rolled back") as caught:
            await db.rolled_back("r1")
        assert caught.value.operation == "rolled_back"
    finally:
        await db.close()
    assert _rows(store, "SELECT count(*) FROM runs") == [(0,)]


async def test_the_function_named_replace_is_not_the_conflict_clause(store, reference):
    db = await _open(store, _profile(reference))
    try:
        await db.start_run("r1")
        await db.run_sql(
            "UPDATE runs SET status = replace('dxne', 'x', 'o') WHERE id = :id OR replace(id, 'r', '') = '1'",
            {"id": "r1"},
        )
    finally:
        await db.close()
    assert _rows(store, "SELECT status FROM runs") == [("done",)]


async def test_a_caught_refusal_cannot_be_committed(store, reference):
    db = await _open(store, _profile(reference))
    try:
        with pytest.raises(StateCompatibilityRefused, match="COMMIT of a transaction"):
            await db.caught("r1", "INSERT OR REPLACE INTO runs (id, status) VALUES ('r1', 'done')")
    finally:
        await db.close()
    assert _rows(store, "SELECT count(*) FROM runs") == [(0,)]


async def test_a_caught_authorizer_denial_cannot_be_committed_even_by_a_raw_commit(
    store, reference
):
    db = await _open(store, _profile(reference))
    try:
        assert await db.raw_commit() == 2
        with pytest.raises(StateCompatibilityRefused, match="COMMIT of a transaction"):
            await db.caught("r2", "UPDATE runs SET note = 'x'")
        # The connection is usable again once the poisoned transaction has ended.
        await db.start_run("r3")
    finally:
        await db.close()
    assert _rows(store, "SELECT id FROM runs ORDER BY id") == [("r0",), ("r3",)]


async def test_a_write_outside_the_operations_transaction_is_refused(store, reference):
    db = await _open(store, _profile(reference))
    try:
        with pytest.raises(StateCompatibilityRefused, match="inside the transaction"):
            await db.autocommit_write("r1")
    finally:
        await db.close()
    assert _rows(store, "SELECT count(*) FROM runs") == [(0,)]


async def test_a_refusal_names_the_transactions_that_committed(store, reference):
    db = await _open(store, _profile(reference))
    try:
        with pytest.raises(StateCompatibilityRefused) as caught:
            await db.two_phase("r1", "DELETE FROM runs")
    finally:
        await db.close()
    assert caught.value.operation == "two_phase"
    assert caught.value.committed == ("start_run transaction 1",)
    assert "already committed: start_run transaction 1" in str(caught.value)
    assert _rows(store, "SELECT id FROM runs") == [("r1",)]


@pytest.mark.parametrize(
    ("statement", "denied"),
    [
        ("CREATE TEMP TABLE scratch (x)", "CREATE_TEMP_TABLE"),
        ("ATTACH DATABASE ':memory:' AS side", "ATTACH"),
        ("INSERT INTO runs (id, status) VALUES ('raw', 'running')", "INSERT"),
        ("UPDATE runs SET status = 'done'", "UPDATE"),
        ("DELETE FROM other", "DELETE"),
        ("PRAGMA journal_mode = DELETE", "PRAGMA"),
        ("PRAGMA wal_checkpoint(TRUNCATE)", "PRAGMA"),
        ("PRAGMA schema_version = 1", "PRAGMA"),
        ("VACUUM", "VACUUM"),
    ],
)
async def test_the_authorizer_denies_what_reaches_sqlite_past_the_engine(
    statement, denied, store, reference
):
    db = await _open(store, _profile(reference))
    digest_rows = _rows(store, "SELECT * FROM other")
    try:
        assert await _raw(db, "SELECT count(*) FROM runs") == [(0,)]
        with pytest.raises(sqlite3.DatabaseError) as caught:
            await _raw(db, statement)
    finally:
        await db.close()
    # SQLite reports a denial as it prepares the statement, except for VACUUM, whose
    # own statements are prepared and denied while it runs.
    assert str(caught.value) == (
        "authorization denied" if statement == "VACUUM" else "not authorized"
    )
    assert getattr(caught.value, "sqlite_errorcode", _SQLITE_AUTH) == _SQLITE_AUTH
    assert _rows(store, "SELECT * FROM other") == digest_rows
    assert _rows(store, "SELECT count(*) FROM runs") == [(0,)]
    assert _rows(store, "PRAGMA journal_mode") == [("wal",)]


async def test_every_new_connection_carries_the_authorizer(store, reference):
    db = await _open(store, _profile(reference))
    try:
        async with db._engine.connect() as held:
            await held.get_raw_connection()
            # The pool has to open a second connection while the first is held.
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                await _raw(db, "CREATE TEMP TABLE scratch (x)")
        await db._engine.dispose()
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            await _raw(db, "CREATE TEMP TABLE scratch (x)")
        await db.start_run("r1")
    finally:
        await db.close()
    assert _rows(store, "SELECT id FROM runs") == [("r1",)]


async def test_a_schema_change_after_admission_is_reread_before_the_next_write(store, reference):
    db = await _open(store, _profile(reference))
    try:
        await db.start_run("r1")
        admitted = db._compat.schema_version
        with closing(sqlite3.connect(store)) as conn:
            conn.execute("CREATE TABLE unrelated (x)")
        await db.start_run("r2")
        assert db._compat.schema_version > admitted
        with closing(sqlite3.connect(store)) as conn:
            conn.execute("ALTER TABLE runs ADD COLUMN extra TEXT")
        with pytest.raises(
            StateCompatibilityRefused, match="schema changed after admission"
        ) as caught:
            await db.start_run("r3")
        assert caught.value.operation == "start_run"
    finally:
        await db.close()
    assert _rows(store, "SELECT id FROM runs ORDER BY id") == [("r1",), ("r2",)]


# Refusals (D6)


@pytest.mark.parametrize("mode", ["PASSIVE", "FULL", "RESTART", "TRUNCATE"])
async def test_a_manual_checkpoint_is_refused(mode, store, reference):
    db = await _open(store, _profile(reference))
    try:
        await db.start_run("r1")
        wal = store.with_name(store.name + "-wal")
        size = wal.stat().st_size
        with pytest.raises(StateCompatibilityRefused, match="checkpoints") as caught:
            await db.checkpoint(mode)
        assert caught.value.operation == "checkpoint"
        assert wal.stat().st_size == size
    finally:
        await db.close()


async def test_vacuum_and_migration_are_refused(store, reference):
    db = await _open(store, _profile(reference))
    digest = _digest(store)
    try:
        with pytest.raises(StateCompatibilityRefused) as caught:
            await db.vacuum()
        assert caught.value.operation == "vacuum"
        with pytest.raises(StateCompatibilityRefused) as caught:
            await db._apply_schema()
        assert caught.value.operation == "migration"
    finally:
        await db.close()
    assert _digest(store) == digest


def test_the_refusal_is_exported_and_survives_pickling():
    assert state_pkg.StateCompatibilityRefused is StateCompatibilityRefused
    error = StateCompatibilityRefused("start_run", "synthetic@1", "a reason", ("t1",))
    copy = pickle.loads(pickle.dumps(error))
    assert (copy.operation, copy.profile, copy.reason, copy.committed) == (
        "start_run",
        "synthetic@1",
        "a reason",
        ("t1",),
    )
    assert str(copy) == str(error)


# Handles


async def test_a_compatibility_handle_is_never_the_shared_native_handle(store, reference):
    compat = _RunsDB(store, compatibility=_profile(reference))
    native = StateDB(store)
    try:
        await db_mod.register_shared_db(compat)
        assert db_mod._shared_key(compat) != db_mod._shared_key(native)
        # a native handle keeps the key it always had
        assert db_mod._shared_key(native) == native.url
        assert db_mod._SHARED.get(db_mod._shared_key(native)) is None
        db_mod.unregister_shared_db(native)
        assert db_mod._SHARED.get(db_mod._shared_key(compat)) is compat
    finally:
        await db_mod.close_shared_db()


# Profiles


def test_a_profile_must_account_for_what_it_writes(reference):
    with closing(sqlite3.connect(reference)) as conn:
        closure = read_closure(conn, ["runs", "events"])

    def build(writes, **changes):
        fields = {
            "name": "p",
            "version": 1,
            "closure": closure,
            "writes": writes,
            "operations": {"op"},
        }
        fields.update(changes)
        return CompatibilityProfile(**fields)

    with pytest.raises(ValueError, match="not in the closure"):
        build({"other": TableWrites(update={"value"})})
    with pytest.raises(ValueError, match="no such columns"):
        build({"runs": TableWrites(update={"missing"})})
    with pytest.raises(ValueError, match="every column"):
        build({"runs": TableWrites(insert={"id", "status"})})
    with pytest.raises(ValueError, match="needs its reason"):
        build(
            {
                "runs": TableWrites(
                    insert={"id", "status", "note"},
                    unwritten={"owner": "default", "updated_at": " "},
                )
            }
        )
    with pytest.raises(ValueError, match="NOT NULL with no default"):
        build(
            {
                "runs": TableWrites(
                    insert={"id", "owner", "note", "updated_at"},
                    unwritten={"status": "never set"},
                )
            }
        )
    with pytest.raises(ValueError, match="names no operation"):
        build(_WRITES, operations=set())
    with pytest.raises(ValueError, match="positive integer"):
        build(_WRITES, version=0)
    with pytest.raises(ValueError, match="without '@'"):
        build(_WRITES, name="a@b")
    # A rowid alias may be left to SQLite.
    assert build(_WRITES).label == "p@1"


def test_a_closure_is_read_with_the_keys_and_objects_that_touch_it(tmp_path):
    store = _store(
        tmp_path / "touched.db",
        _SCHEMA
        + "CREATE TABLE notes (run_id TEXT REFERENCES runs(id));"
        + "CREATE TRIGGER t AFTER UPDATE ON other BEGIN UPDATE runs SET note = 'x'; END;"
        + "CREATE VIEW v AS SELECT value FROM other;",
    )
    with closing(sqlite3.connect(store)) as conn:
        closure = read_closure(conn, ["Runs"])
    assert closure.table_names == {"runs"}
    (runs,) = closure.tables
    assert [c.name for c in runs.columns] == ["id", "owner", "status", "note", "updated_at"]
    assert runs.checks == {"status in ('running', 'done')"}
    assert {(k.table, k.parent) for k in closure.foreign_keys} == {
        ("events", "runs"),
        ("notes", "runs"),
    }
    assert {(o.type, o.name) for o in closure.objects} == {("trigger", "t")}


# Statement classification


@pytest.mark.parametrize(
    ("statement", "kind"),
    [
        ("SELECT 1", "read"),
        ("  select * from runs -- ; DELETE FROM runs", "read"),
        ("VALUES (1)", "read"),
        ("WITH x AS (SELECT 1) SELECT * FROM x", "read"),
        ("WITH x AS (SELECT 1) INSERT INTO runs SELECT * FROM x", "write"),
        ("/* select */ DELETE FROM runs", "write"),
        ("PRAGMA schema_version", "read"),
        ("PRAGMA main.table_info(runs)", "read"),
        ("PRAGMA schema_version = 3", "write"),
        ("PRAGMA wal_checkpoint", "write"),
        ("PRAGMA optimize", "write"),
        ("BEGIN IMMEDIATE", "begin"),
        ("COMMIT", "commit"),
        ("END TRANSACTION", "commit"),
        ("ROLLBACK", "rollback"),
        ("ROLLBACK TO s1", "savepoint"),
        ("RELEASE s1", "savepoint"),
        ("", "write"),
        ("EXPLAIN SELECT 1", "write"),
    ],
)
def test_statements_are_classified_conservatively(statement, kind):
    assert _statement_kind(_sql_tokens(statement)) == kind


@pytest.mark.parametrize(
    ("statement", "refused"),
    [
        ('INSERT INTO runs ("returning") VALUES (1)', False),
        ("INSERT INTO runs (note) VALUES ('or replace returning')", False),
        ("INSERT INTO runs (id) VALUES (1) ON CONFLICT (id) DO UPDATE SET note = 'x'", False),
        ("WITH x AS (SELECT 1) UPDATE runs SET note = (SELECT 1 FROM x)", False),
        ("INSERT INTO runs SELECT * FROM (SELECT 1 RETURNING)", False),
        ("INSERT or replace INTO runs (id) VALUES (1)", True),
        ("WITH x AS (SELECT 1) DELETE FROM runs", True),
        ("UPDATE runs SET note = 'x' RETURNING *", True),
    ],
)
def test_write_shapes(statement, refused):
    assert (_write_refusal(_sql_tokens(statement)) is not None) is refused
