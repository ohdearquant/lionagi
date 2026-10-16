# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Compatibility-mode StateDB (ADR-0128 D1, D2, D3, D6) against a synthetic managed store."""

from __future__ import annotations

import asyncio
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
    _STATE_KEY,
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
    "rolled_back_raw",
    "rolled_back_then_commit",
    "rolled_back_then_begin",
    "rolled_back_and_left",
    "begin_inside",
    "spawns_writer",
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

    @_operation("rolled_back_raw")
    async def rolled_back_raw(self, run_id: str) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text("INSERT INTO runs (id, status) VALUES (:id, 'running')"), {"id": run_id}
            )
            driver = (await conn.get_raw_connection()).driver_connection
            with pytest.raises(sqlite3.IntegrityError):
                await driver.execute(
                    "INSERT OR ROLLBACK INTO runs (id, status) VALUES (?, 'running')", (run_id,)
                )
            # Past the statement check too: only the authorizer sees this write, which would
            # otherwise run in a transaction the driver opens and this operation commits.
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                await driver.execute("INSERT INTO runs (id, status) VALUES ('after', 'running')")

    @_operation("rolled_back_then_commit")
    async def rolled_back_then_commit(self, run_id: str) -> None:
        async with self._engine.connect() as conn:
            await conn.begin()
            await conn.execute(
                text("INSERT INTO runs (id, status) VALUES (:id, 'running')"), {"id": run_id}
            )
            driver = (await conn.get_raw_connection()).driver_connection
            with pytest.raises(sqlite3.IntegrityError):
                await driver.execute(
                    "INSERT OR ROLLBACK INTO runs (id, status) VALUES (?, 'running')", (run_id,)
                )
            with pytest.raises(StateCompatibilityRefused, match="SQLite rolled back"):
                await conn.execute(
                    text("INSERT INTO runs (id, status) VALUES ('after', 'running')")
                )
            with pytest.raises(StateCompatibilityRefused, match="COMMIT of a transaction"):
                await conn.exec_driver_sql("COMMIT")
            await conn.rollback()

    @_operation("rolled_back_then_begin")
    async def rolled_back_then_begin(self, run_id: str, raw: bool) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text("INSERT INTO runs (id, status) VALUES (:id, 'running')"), {"id": run_id}
            )
            driver = (await conn.get_raw_connection()).driver_connection
            with pytest.raises(sqlite3.IntegrityError):
                await driver.execute(
                    "INSERT OR ROLLBACK INTO runs (id, status) VALUES (?, 'running')", (run_id,)
                )
            # A transaction the guard did not open, on the connection SQLite took the
            # operation's transaction away from.
            await conn.exec_driver_sql("BEGIN")
            if raw:
                with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                    await driver.execute(
                        "INSERT INTO runs (id, status) VALUES ('after', 'running')"
                    )
            else:
                with pytest.raises(StateCompatibilityRefused, match="inside the transaction"):
                    await conn.execute(
                        text("INSERT INTO runs (id, status) VALUES ('after', 'running')")
                    )

    @_operation("rolled_back_and_left")
    async def rolled_back_and_left(self, run_id: str) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text("INSERT INTO runs (id, status) VALUES (:id, 'running')"), {"id": run_id}
            )
            driver = (await conn.get_raw_connection()).driver_connection
            await driver.execute(
                "INSERT OR ROLLBACK INTO runs (id, status) VALUES (?, 'running')", (run_id,)
            )

    @_operation("begin_inside")
    async def begin_inside(self, run_id: str) -> None:
        async with self._tx() as conn:
            await conn.execute(
                text("INSERT INTO runs (id, status) VALUES (:id, 'running')"), {"id": run_id}
            )
            # SQLite would refuse it too, and leave this transaction open.
            with pytest.raises(StateCompatibilityRefused, match="BEGIN inside an open"):
                await conn.exec_driver_sql("BEGIN")

    @_operation("spawns_writer")
    async def spawns_writer(self, release: asyncio.Event) -> asyncio.Task:
        async def late() -> None:
            await release.wait()
            async with self._tx() as conn:
                await conn.execute(text("INSERT INTO runs (id, status) VALUES ('late', 'running')"))

        return asyncio.create_task(late())

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
    "collation": ("id TEXT PRIMARY KEY,", "id TEXT PRIMARY KEY COLLATE NOCASE,"),
    "deferrable foreign key": (
        "REFERENCES runs(id)",
        "REFERENCES runs(id) DEFERRABLE INITIALLY DEFERRED",
    ),
    "autoincrement": ("INTEGER PRIMARY KEY AUTOINCREMENT", "INTEGER PRIMARY KEY"),
    "implicit parent key": ("REFERENCES runs(id)", "REFERENCES runs"),
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


_TAG = "    updated_at REAL,\n    tag TEXT GENERATED ALWAYS AS (status || '?') VIRTUAL\n);"


@pytest.mark.parametrize(
    ("old", "reference_new", "store_new"),
    [
        ("    updated_at REAL\n);", _TAG, _TAG.replace("'?'", "'!'")),
        (
            "INSERT INTO other",
            "CREATE UNIQUE INDEX note_key ON runs (lower(note));\nINSERT INTO other",
            "CREATE UNIQUE INDEX note_key ON runs (upper(note));\nINSERT INTO other",
        ),
        (
            "INSERT INTO other",
            "CREATE UNIQUE INDEX note_key ON runs (note);\nINSERT INTO other",
            "CREATE UNIQUE INDEX note_key ON runs (note COLLATE NOCASE);\nINSERT INTO other",
        ),
    ],
    ids=["generated expression", "indexed expression", "indexed collation"],
)
async def test_a_difference_pragma_output_leaves_out_is_refused(
    old, reference_new, store_new, tmp_path
):
    assert old in _SCHEMA
    reference = _store(tmp_path / "reference.db", _SCHEMA.replace(old, reference_new, 1))
    store = _store(tmp_path / "managed.db", _SCHEMA.replace(old, store_new, 1))
    writes = {
        **_WRITES,
        "runs": TableWrites(
            insert=_WRITES["runs"].insert,
            update=_WRITES["runs"].update,
            unwritten={**_WRITES["runs"].unwritten, "tag": "SQLite computes it"}
            if "tag" in reference_new
            else _WRITES["runs"].unwritten,
        ),
    }
    profile = _profile(reference, writes=writes)
    # The same store as the profile's is admitted, so only the difference refuses.
    await (await _open(reference, profile)).close()
    db = _RunsDB(store, compatibility=profile)
    with pytest.raises(StateCompatibilityRefused, match="does not match the profile"):
        await db.open()
    assert db._engine is None


@pytest.mark.parametrize(
    ("old", "reference_new", "store_new"),
    [
        ("IN ('running', 'done')", 'IN ("running", "done")', 'IN ("Running", "done")'),
        (
            "INSERT INTO other",
            'CREATE UNIQUE INDEX open_note ON runs (note) WHERE status = "running";\n'
            "INSERT INTO other",
            'CREATE UNIQUE INDEX open_note ON runs (note) WHERE status = "Running";\n'
            "INSERT INTO other",
        ),
    ],
    ids=["check", "partial index"],
)
async def test_a_table_definition_keeps_sqlite_resolution_of_names(
    old, reference_new, store_new, tmp_path
):
    """A table's definition is compared as SQLite resolves names, so a double-quoted token that
    names no column is a string literal and its case is kept."""
    assert old in _SCHEMA
    reference = _store(tmp_path / "reference.db", _SCHEMA.replace(old, reference_new, 1))
    store = _store(tmp_path / "managed.db", _SCHEMA.replace(old, store_new, 1))
    profile = _profile(reference)
    await (await _open(reference, profile)).close()
    db = _RunsDB(store, compatibility=profile)
    with pytest.raises(StateCompatibilityRefused, match="does not match the profile"):
        await db.open()
    assert db._engine is None


_DEFAULTS = [
    ("open", "Open"),
    ("'managed'", '"managed"'),
    ('"managed"', '"Managed"'),
]


@pytest.mark.parametrize(
    ("reference_default", "store_default"), _DEFAULTS, ids=["bare", "quotes", "case"]
)
async def test_a_default_value_is_compared_as_written(reference_default, store_default, tmp_path):
    """A DEFAULT is compared as the column's definition writes it: SQLite stores the word as
    given, so ``DEFAULT open`` and ``DEFAULT Open`` are two different defaults."""
    old = "DEFAULT 'managed'"
    assert old in _SCHEMA
    reference = _store(
        tmp_path / "reference.db", _SCHEMA.replace(old, f"DEFAULT {reference_default}", 1)
    )
    store = _store(tmp_path / "managed.db", _SCHEMA.replace(old, f"DEFAULT {store_default}", 1))
    profile = _profile(reference)
    await (await _open(reference, profile)).close()
    db = _RunsDB(store, compatibility=profile)
    with pytest.raises(StateCompatibilityRefused, match="does not match the profile"):
        await db.open()
    assert db._engine is None


_QUOTED_A_QUALIFIED_COLUMN = (
    "CREATE VIEW live AS SELECT runs.id FROM runs;\n",
    'CREATE VIEW live AS SELECT "runs.id" FROM runs;\n',
)
_QUOTED_A_LITERAL_IN_A_VIEW = (
    'CREATE VIEW live AS SELECT runs.id FROM runs JOIN other ON "done" = runs.status;\n',
    'CREATE VIEW live AS SELECT runs.id FROM runs JOIN other ON "DONE" = runs.status;\n',
)
_QUOTED_A_LITERAL_IN_A_TRIGGER = (
    "CREATE TRIGGER fill AFTER INSERT ON events BEGIN "
    "INSERT INTO runs (id, status, note) SELECT NEW.run_id || '-n', 'running', \"note\"; END;\n",
    "CREATE TRIGGER fill AFTER INSERT ON events BEGIN "
    "INSERT INTO runs (id, status, note) SELECT NEW.run_id || '-n', 'running', \"NOTE\"; END;\n",
)
_QUOTED_A_LITERAL_IN_A_TRIGGER_CONDITION = (
    'CREATE TRIGGER finished AFTER UPDATE ON runs WHEN NEW.status = "done" BEGIN SELECT 1; END;\n',
    'CREATE TRIGGER finished AFTER UPDATE ON runs WHEN NEW.status = "Done" BEGIN SELECT 1; END;\n',
)


@pytest.mark.parametrize(
    ("reference_object", "store_object"),
    [
        _QUOTED_A_QUALIFIED_COLUMN,
        _QUOTED_A_LITERAL_IN_A_VIEW,
        _QUOTED_A_LITERAL_IN_A_TRIGGER,
        _QUOTED_A_LITERAL_IN_A_TRIGGER_CONDITION,
    ],
    ids=["view column", "view literal", "trigger literal", "trigger condition"],
)
async def test_a_trigger_or_view_body_is_compared_as_written(
    reference_object, store_object, tmp_path
):
    """A view's or a trigger's body is compared as written: whether SQLite reads a quoted token
    as a name or as a string literal depends on its scope and on the connection's setting, so a
    difference in quoting refuses."""
    reference = _store(tmp_path / "reference.db", _SCHEMA + reference_object)
    store = _store(tmp_path / "managed.db", _SCHEMA + store_object)
    profile = _profile(reference)
    await (await _open(reference, profile)).close()
    db = _RunsDB(store, compatibility=profile)
    with pytest.raises(StateCompatibilityRefused, match="does not match the profile") as caught:
        await db.open()
    assert "missing or changed" in caught.value.reason
    assert db._engine is None


async def test_a_column_added_later_matches_one_the_table_was_created_with(tmp_path, reference):
    """A store the managing application migrated is compared by definition, not by text."""
    migrated = _SCHEMA.replace("    note TEXT,\n    updated_at REAL\n);", "    note TEXT\n);", 1)
    assert migrated != _SCHEMA
    store = _store(
        tmp_path / "migrated.db", migrated + "ALTER TABLE runs ADD COLUMN updated_at REAL;\n"
    )
    db = await _open(store, _profile(reference))
    await db.close()


_TRIGGER_RESOLUTIONS = {
    "insert or replace": (
        "AFTER INSERT ON runs BEGIN INSERT OR REPLACE INTO events (seq, run_id, body) "
        "VALUES (1, NEW.id, 'mirrored'); END",
        "INSERT OR REPLACE",
    ),
    "replace into": (
        "AFTER INSERT ON runs BEGIN REPLACE INTO events (seq, run_id, body) "
        "VALUES (1, NEW.id, 'mirrored'); END",
        "REPLACE INTO",
    ),
    "insert or ignore": (
        "AFTER INSERT ON runs BEGIN INSERT OR IGNORE INTO events (run_id, body) "
        "VALUES (NEW.id, 'mirrored'); END",
        "INSERT OR IGNORE",
    ),
    "update or rollback": (
        "AFTER UPDATE ON runs BEGIN UPDATE OR ROLLBACK runs SET note = 'x' WHERE id = NEW.id; END",
        "UPDATE OR ROLLBACK",
    ),
    "raise ignore": (
        "BEFORE INSERT ON runs WHEN NEW.note = 'skip' BEGIN SELECT RAISE(IGNORE); END",
        "RAISE(IGNORE)",
    ),
    "raise rollback": (
        "BEFORE INSERT ON runs WHEN NEW.note = 'stop' BEGIN SELECT RAISE(ROLLBACK, 'no'); END",
        "RAISE(ROLLBACK)",
    ),
    "quoted names in another case": (
        'AFTER INSERT ON "Runs" BEGIN INSERT OR REPLACE INTO "Events" (seq, run_id, body) '
        "VALUES (1, NEW.id, 'mirrored'); END",
        "INSERT OR REPLACE",
    ),
    "string literal names": (
        "AFTER INSERT ON 'runs' BEGIN INSERT OR REPLACE INTO 'events' (seq, run_id, body) "
        "VALUES (1, NEW.id, 'mirrored'); END",
        "INSERT OR REPLACE",
    ),
}


@pytest.mark.parametrize("change", sorted(_TRIGGER_RESOLUTIONS))
async def test_a_trigger_that_names_a_resolution_but_abort_is_refused(change, tmp_path):
    """A REPLACE in a trigger deletes a row the authorizer never sees, as a table's would."""
    body, spelling = _TRIGGER_RESOLUTIONS[change]
    schema = _SCHEMA + f"CREATE TRIGGER managed {body};\n"
    reference = _store(tmp_path / "reference.db", schema)
    store = _store(tmp_path / "managed.db", schema)
    db = _RunsDB(store, compatibility=_profile(reference))
    with pytest.raises(StateCompatibilityRefused, match="admits only ABORT") as caught:
        await db.open()
    assert f"trigger managed names {spelling}" in caught.value.reason
    assert db._engine is None


async def test_a_trigger_that_raises_abort_or_writes_plainly_is_admitted(tmp_path):
    schema = _SCHEMA + (
        "CREATE TRIGGER guarded BEFORE UPDATE ON runs WHEN NEW.status = 'lost' "
        "BEGIN SELECT RAISE(ABORT, 'no'); END;\n"
        "CREATE TRIGGER mirrored AFTER INSERT ON runs "
        "BEGIN INSERT INTO events (run_id, body) VALUES (NEW.id, 'mirrored'); END;\n"
    )
    reference = _store(tmp_path / "reference.db", schema)
    store = _store(tmp_path / "managed.db", schema)
    db = await _open(store, _profile(reference))
    await db.close()


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


_PAST_ASCII = (
    "CREATE TABLE runs€ (id TEXT PRIMARY KEY);\n"
    "CREATE TABLE tail€ (id TEXT PRIMARY KEY, value TEXT);\n"
    "CREATE TRIGGER mirror AFTER INSERT ON runs€ BEGIN "
    "INSERT OR REPLACE INTO tail€ (id, value) VALUES (NEW.id, 'mirrored'); END;\n"
)


async def test_tables_named_past_ascii_are_read_as_sqlite_reads_them(tmp_path):
    """SQLite reads every character past ASCII as part of a bare name: ``runs€`` is one name."""
    reference = _store(tmp_path / "reference.db", _PAST_ASCII)
    store = _store(tmp_path / "managed.db", _PAST_ASCII)
    with closing(sqlite3.connect(reference)) as conn:
        closure = read_closure(conn, ["runs€", "tail€"])
    assert {(o.type, o.name) for o in closure.objects} == {("trigger", "mirror")}
    profile = CompatibilityProfile(
        name="past-ascii", version=1, closure=closure, writes={}, operations={"start_run"}
    )
    db = StateDB(store, compatibility=profile)
    with pytest.raises(StateCompatibilityRefused, match="admits only ABORT") as caught:
        await db.open()
    assert "trigger mirror names INSERT OR REPLACE" in caught.value.reason
    assert db._engine is None


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


async def test_a_raw_write_after_sqlite_rolled_the_transaction_back_is_refused(store, reference):
    db = await _open(store, _profile(reference))
    try:
        await db.rolled_back_raw("r1")
        # The next operation opens its own transaction and writes as usual.
        await db.start_run("r2")
    finally:
        await db.close()
    assert _rows(store, "SELECT id FROM runs ORDER BY id") == [("r2",)]


async def test_a_commit_after_a_refusal_for_a_rolled_back_transaction_is_refused(store, reference):
    """The refusal is the guard's own, as for any other refused write."""
    db = await _open(store, _profile(reference))
    try:
        await db.rolled_back_then_commit("r1")
        await db.start_run("r2")
    finally:
        await db.close()
    assert _rows(store, "SELECT id FROM runs ORDER BY id") == [("r2",)]


@pytest.mark.parametrize("raw", [False, True], ids=["through the engine", "to the driver"])
async def test_a_write_in_a_transaction_begun_after_sqlite_rolled_back_is_refused(
    raw, store, reference
):
    db = await _open(store, _profile(reference))
    try:
        if raw:
            # The denial poisons the transaction the raw BEGIN opened.
            with pytest.raises(StateCompatibilityRefused, match="COMMIT of a transaction"):
                await db.rolled_back_then_begin("r1", raw=True)
        else:
            await db.rolled_back_then_begin("r1", raw=False)
        await db.start_run("r2")
    finally:
        await db.close()
    assert _rows(store, "SELECT id FROM runs ORDER BY id") == [("r2",)]


async def test_a_raw_transaction_after_sqlite_ended_an_operations_cannot_write(store, reference):
    """No ROLLBACK reaches SQLite for a transaction it already ended, so the mark outlives it."""
    db = await _open(store, _profile(reference))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            await db.rolled_back_and_left("r1")
        async with db._engine.connect() as conn:
            raw = await conn.get_raw_connection()
            # The connection the operation used, still carrying the mark.
            assert raw.info[_STATE_KEY].write_txn
            driver = raw.driver_connection
            await driver.execute("BEGIN")
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                await driver.execute("INSERT INTO runs (id, status) VALUES ('raw', 'running')")
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                await driver.execute("COMMIT")
            await driver.execute("ROLLBACK")
        await db.start_run("r2")
    finally:
        await db.close()
    assert _rows(store, "SELECT id FROM runs ORDER BY id") == [("r2",)]


async def test_a_task_an_operation_started_cannot_write_once_it_returns(store, reference):
    db = await _open(store, _profile(reference))
    try:
        release = asyncio.Event()
        task = await db.spawns_writer(release)
        release.set()
        with pytest.raises(StateCompatibilityRefused, match="only inside an operation"):
            await task
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


async def test_a_begin_inside_the_operations_transaction_poisons_it(store, reference):
    db = await _open(store, _profile(reference))
    try:
        with pytest.raises(StateCompatibilityRefused, match="COMMIT of a transaction"):
            await db.begin_inside("r1")
        # The connection is usable again once the poisoned transaction has ended.
        await db.start_run("r2")
    finally:
        await db.close()
    assert _rows(store, "SELECT id FROM runs ORDER BY id") == [("r2",)]


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


async def test_a_read_only_handle_is_shared_by_its_url_as_before(store):
    readonly = StateDB(store, readonly=True)
    try:
        await db_mod.register_shared_db(readonly)
        assert db_mod._shared_key(readonly) == readonly.url
        assert await db_mod.get_shared_db(store) is readonly
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
    # The trigger reads other, so other is part of the closure too, and the view that reads other
    # with it.
    assert closure.table_names == {"runs", "other"}
    shapes = {table.name: table for table in closure.tables}
    runs = shapes["runs"]
    assert [c.name for c in runs.columns] == ["id", "owner", "status", "note", "updated_at"]
    assert runs.checks == {"status in ('running', 'done')"}
    assert {(k.table, k.parent) for k in closure.foreign_keys} == {
        ("events", "runs"),
        ("notes", "runs"),
    }
    assert {(o.type, o.name) for o in closure.objects} == {("trigger", "t"), ("view", "v")}


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


def test_tokens_split_where_sqlite_splits_them():
    assert _sql_tokens("INSERT INTO tail€ VALUES (1)")[2] == ("word", "tail€")
    # Only ASCII whitespace separates; a no-break space is part of the name.
    assert _sql_tokens("SELECT a\u00a0b") == [("word", "select"), ("word", "a\u00a0b")]
    assert _sql_tokens("(\u00a0runs)")[1] == ("word", "\u00a0runs")
    assert _sql_tokens('"Open"') == [("ident", "open")]
    # Only a double-quoted token can be a string literal, and it keeps its quotes with its case.
    assert _sql_tokens('"Open" `Open` [Open]', spelled=True) == [
        ("ident", '"Open"'),
        ("ident", "open"),
        ("ident", "open"),
    ]
    # A double-quoted literal keeps its quotes, so it equals only the same literal as written.
    assert _sql_tokens('"null"', spelled=True) == [("ident", '"null"')]
    assert _sql_tokens('CHECK (note IS NOT "null")', spelled=True)[3:] == [
        ("word", "is"),
        ("word", "not"),
        ("ident", '"null"'),
        ("punct", ")"),
    ]
    # A single x or X immediately before a quote is a blob literal, one token through its close.
    assert _sql_tokens("SELECT x'41', X'af'") == [
        ("word", "select"),
        ("blob", "x'41'"),
        ("punct", ","),
        ("blob", "X'af'"),
    ]
    # A word ending in x before a quote is not a blob: only a whole word of one x starts one.
    assert _sql_tokens("SELECT ax'41'") == [
        ("word", "select"),
        ("word", "ax"),
        ("string", "'41'"),
    ]
    assert _sql_tokens("SELECT x '41'") == [("word", "select"), ("word", "x"), ("string", "'41'")]
    # Verbatim, a trigger body, a view body or a DEFAULT keeps its text as written.
    assert _sql_tokens('"Open" `Open` [Open] Open', verbatim=True) == [
        ("ident", '"Open"'),
        ("ident", "`Open`"),
        ("ident", "[Open]"),
        ("word", "Open"),
    ]


@pytest.mark.parametrize(
    ("named", "twin"), [("ünits", "Ünits"), ("keys", "\u212aeys")], ids=["u-umlaut", "kelvin sign"]
)
async def test_a_name_that_differs_past_ascii_in_case_is_another_table(tmp_path, named, twin):
    """SQLite folds only ASCII letters, so the two spellings name two tables."""
    store = _store(
        tmp_path / "managed.db",
        f'CREATE TABLE "{named}" (id TEXT PRIMARY KEY, label TEXT);\n'
        f'CREATE TABLE "{twin}" (id TEXT PRIMARY KEY, label TEXT, extra TEXT);\n',
    )
    with closing(sqlite3.connect(store)) as conn:
        closure = read_closure(conn, [named])
    (shape,) = closure.tables
    assert [column.name for column in shape.columns] == ["id", "label"]
    profile = CompatibilityProfile(
        name="twins",
        version=1,
        closure=closure,
        writes={named: TableWrites(insert={"id", "label"})},
        operations={"run_sql"},
    )
    db = _RunsDB(store, compatibility=profile)
    await db.open()
    try:
        await db.run_sql(f"INSERT INTO \"{named}\" (id, label) VALUES ('a', 'x')")
        with pytest.raises(StateCompatibilityRefused):
            await db.run_sql(f"INSERT INTO \"{twin}\" (id, label) VALUES ('b', 'x')")
    finally:
        await db.close()
    assert _rows(store, f'SELECT id FROM "{named}"') == [("a",)]
    assert _rows(store, f'SELECT id FROM "{twin}"') == []


@pytest.mark.parametrize(
    "body",
    [
        "INSERT INTO other (id, value) VALUES ('runs', NEW.id)",
        "UPDATE other SET value = 'runs' WHERE id = NEW.id",
        "DELETE FROM other WHERE value IN (SELECT id FROM other, 'runs')",
        "DELETE FROM other WHERE value IN 'runs'",
        "DELETE FROM other WHERE value IN (SELECT id FROM main.'runs')",
        "UPDATE OR ABORT 'runs' SET note = NEW.value WHERE id = NEW.id",
    ],
    ids=["value", "set value", "from list", "in", "after a dot", "or action"],
)
async def test_a_quoted_name_anywhere_in_a_trigger_body_counts(tmp_path, body):
    """A quoted token is a possible table name wherever it sits, so any spelling counts."""
    store = _store(
        tmp_path / "managed.db",
        _SCHEMA + f"CREATE TRIGGER mirror AFTER INSERT ON other BEGIN {body}; END;\n",
    )
    with closing(sqlite3.connect(store)) as conn:
        closure = read_closure(conn, ["runs", "events"])
    assert ("trigger", "mirror") in {(o.type, o.name) for o in closure.objects}


@pytest.mark.parametrize(
    "object_sql",
    [
        "CREATE VIEW v AS SELECT * FROM ('runs')",
        "CREATE VIEW v AS SELECT x.id FROM (other, 'runs') AS x",
        "CREATE VIEW v AS SELECT other.id FROM other JOIN ('runs') AS r ON r.id = other.id",
        "CREATE VIEW v AS SELECT id FROM other UNION SELECT id FROM ('runs')",
        "CREATE VIEW v AS SELECT n.id FROM (SELECT id FROM other), ('runs') AS n",
        'CREATE VIEW v AS SELECT * FROM ("runs")',
    ],
    ids=["from", "from list", "join", "union", "subquery", "quoted"],
)
async def test_a_name_a_parenthesised_list_hides_is_still_in_the_closure(object_sql, tmp_path):
    """A view or trigger that names a closure table only through a quoted token inside a
    parenthesised list still belongs to the closure, so a profile that does not list it refuses."""
    reference = _store(tmp_path / "reference.db", _SCHEMA)
    store = _store(tmp_path / "managed.db", _SCHEMA + object_sql + ";\n")
    profile = _profile(reference)
    await (await _open(reference, profile)).close()
    db = _RunsDB(store, compatibility=profile)
    with pytest.raises(StateCompatibilityRefused, match="is not in the profile") as caught:
        await db.open()
    assert "view v" in caught.value.reason
    assert db._engine is None


async def test_a_view_that_names_no_closure_table_still_admits(tmp_path):
    store = _store(
        tmp_path / "managed.db",
        _SCHEMA + "CREATE VIEW v AS SELECT value FROM other;\n",
    )
    db = await _open(store, _profile(_store(tmp_path / "reference.db", _SCHEMA)))
    await db.close()


_LAYOUTS = {
    "trigger": (
        "CREATE TRIGGER fill AFTER INSERT ON events BEGIN\n"
        "  INSERT INTO runs (id, status, note)\n"
        "  SELECT NEW.run_id || '-n', 'running', \"note\"; END;\n",
        "CREATE TRIGGER fill\nAFTER INSERT ON events\nBEGIN -- writes the note\n"
        "  INSERT INTO runs (id, status, note)\n"
        "  SELECT NEW.run_id || '-n', 'running', \"note\";\nEND;\n",
    ),
    "view": (
        "CREATE VIEW live AS SELECT runs.id FROM runs;\n",
        "CREATE VIEW live AS\n  SELECT runs.id\n  /* the runs a reader sees */\n  FROM runs;\n",
    ),
}


@pytest.mark.parametrize("object_type", sorted(_LAYOUTS))
async def test_a_body_that_differs_only_in_layout_is_admitted(object_type, tmp_path):
    """Whitespace, comments and line breaks are set aside when a body is compared."""
    reference_sql, store_sql = _LAYOUTS[object_type]
    reference = _store(tmp_path / "reference.db", _SCHEMA + reference_sql)
    store = _store(tmp_path / "managed.db", _SCHEMA + store_sql)
    db = await _open(store, _profile(reference))
    await db.close()


async def test_a_default_that_differs_only_in_layout_is_admitted(tmp_path):
    """Whitespace and comments are set aside when a DEFAULT is compared."""
    old = "DEFAULT 'managed'"
    assert old in _SCHEMA
    reference = _store(
        tmp_path / "reference.db", _SCHEMA.replace(old, "DEFAULT /* the owner */ 'managed'", 1)
    )
    store = _store(tmp_path / "managed.db", _SCHEMA.replace(old, "DEFAULT\n    'managed'", 1))
    db = await _open(store, _profile(reference))
    await db.close()


async def test_a_body_that_differs_only_in_the_case_of_a_keyword_is_refused(tmp_path):
    """A body is compared as written, so a keyword in another case refuses the store.

    This is the accepted cost of the rule: SQLite reads ``select`` and ``SELECT`` alike, and this
    mode cannot tell every such spelling apart without reading the body as SQLite does. A refused
    store is refused only, never written to.
    """
    reference = _store(
        tmp_path / "reference.db", _SCHEMA + "CREATE VIEW live AS SELECT runs.id FROM runs;\n"
    )
    store = _store(
        tmp_path / "managed.db", _SCHEMA + "CREATE VIEW live AS select runs.id from runs;\n"
    )
    profile = _profile(reference)
    await (await _open(reference, profile)).close()
    db = _RunsDB(store, compatibility=profile)
    with pytest.raises(StateCompatibilityRefused, match="does not match the profile") as caught:
        await db.open()
    assert "view live is missing or changed" in caught.value.reason
    assert db._engine is None


@pytest.mark.parametrize(
    ("old", "reference_new", "store_new"),
    [
        ("REFERENCES runs(id)", "REFERENCES runs(id)", 'REFERENCES "Runs"(id)'),
        ("    owner TEXT", "    owner TEXT", '    "Owner" TEXT'),
        ("CHECK (status IN", "CHECK (status IN", 'CHECK ("STATUS" IN'),
        ("    note TEXT,", "    note TEXT COLLATE NOCASE,", '    note TEXT COLLATE "NOCASE",'),
        (
            "INSERT INTO other",
            "CREATE UNIQUE INDEX open_note ON runs (note);\nINSERT INTO other",
            'CREATE UNIQUE INDEX open_note ON "Runs" ("NOTE");\nINSERT INTO other',
        ),
    ],
    ids=["references", "column", "check", "collate", "index"],
)
async def test_a_table_definition_quotes_a_name_in_another_case(
    old, reference_new, store_new, tmp_path
):
    """SQLite resolves a double-quoted token that names a table, a column or a collation as that
    name, so a table's definition quotes it in either case."""
    assert old in _SCHEMA
    reference = _store(tmp_path / "reference.db", _SCHEMA.replace(old, reference_new, 1))
    store = _store(tmp_path / "managed.db", _SCHEMA.replace(old, store_new, 1))
    await (await _open(store, _profile(reference))).close()


@pytest.mark.parametrize(
    "object_sql",
    [
        'CREATE TRIGGER finished AFTER UPDATE ON "Runs" BEGIN SELECT 1; END',
        'CREATE TRIGGER finished AFTER UPDATE ON runs BEGIN UPDATE "Events" SET body = 1; END',
        'CREATE VIEW live AS SELECT "runs".id FROM "runs"',
    ],
    ids=["table", "column", "view"],
)
async def test_a_body_that_quotes_a_name_in_another_case_is_refused(object_sql, tmp_path):
    """A body is compared as written, so a quoted name in another case refuses the store even
    where SQLite would resolve it as the profile's object does."""
    reference = _store(tmp_path / "reference.db", _SCHEMA)
    store = _store(tmp_path / "managed.db", _SCHEMA + object_sql + ";\n")
    profile = _profile(reference)
    await (await _open(reference, profile)).close()
    db = _RunsDB(store, compatibility=profile)
    with pytest.raises(StateCompatibilityRefused, match="is not in the profile"):
        await db.open()
    assert db._engine is None


# A literal keeps its quotes, a blob is one token, and a trigger's order counts (D2)


async def _refused_against(
    reference: Path, store: Path, profile: CompatibilityProfile | None = None
) -> str:
    """The reference store is admitted against its own closure; the other store is refused."""
    profile = profile or _profile(reference)
    await (await _open(reference, profile)).close()
    db = _RunsDB(store, compatibility=profile)
    with pytest.raises(StateCompatibilityRefused, match="does not match the profile") as caught:
        await db.open()
    assert db._engine is None
    return caught.value.reason


_NOTE_COLUMN = "    note TEXT,"
_STATUS_LINE = "status TEXT NOT NULL CHECK (status IN ('running', 'done'))"


def _literal_schema(kind: str, spelling: str) -> str:
    """The synthetic schema with one definition written as *spelling* for the *kind* of site."""
    if kind == "check keyword":
        return _SCHEMA.replace(_NOTE_COLUMN, f"    {spelling},", 1)
    if kind == "index predicate":
        return _SCHEMA + spelling
    return _SCHEMA.replace(_STATUS_LINE, spelling, 1)


_LITERAL_PAIRS = {
    "check keyword": ("note TEXT CHECK (note IS NOT NULL)", 'note TEXT CHECK (note IS NOT "null")'),
    "index predicate": (
        "CREATE UNIQUE INDEX once_note ON runs (note) WHERE updated_at IS NULL;\n",
        'CREATE UNIQUE INDEX once_note ON runs (note) WHERE updated_at IS "null";\n',
    ),
    "literal list": (
        'status TEXT NOT NULL CHECK (status IN ("running", "done"))',
        'status TEXT NOT NULL CHECK (status IN ("running, done"))',
    ),
}


@pytest.mark.parametrize(
    ("reference_sql", "store_sql"), _LITERAL_PAIRS.values(), ids=_LITERAL_PAIRS
)
async def test_a_double_quoted_literal_keeps_its_quotes(reference_sql, store_sql, tmp_path):
    """A double-quoted token that names no column is a string literal, so it equals only the
    same literal written the same way: a keyword, or a list a comma splits two ways, in another
    spelling is another definition."""
    kind = next(k for k, pair in _LITERAL_PAIRS.items() if pair[0] == reference_sql)
    reference = _store(tmp_path / "reference.db", _literal_schema(kind, reference_sql))
    store = _store(tmp_path / "managed.db", _literal_schema(kind, store_sql))
    reason = await _refused_against(reference, store)
    assert "runs" in reason


@pytest.mark.parametrize(
    ("spelling",), [(p[1],) for p in _LITERAL_PAIRS.values()], ids=_LITERAL_PAIRS
)
async def test_the_same_literal_on_both_sides_is_admitted(spelling, tmp_path):
    """Two stores spelling the same double-quoted literal the same way are admitted."""
    kind = next(k for k, pair in _LITERAL_PAIRS.items() if pair[1] == spelling)
    schema = _literal_schema(kind, spelling)
    reference = _store(tmp_path / "reference.db", schema)
    store = _store(tmp_path / "managed.db", schema)
    db = await _open(store, _profile(reference))
    await db.close()


_BLOB_VIEW = "CREATE VIEW live AS SELECT x{}'41' FROM runs;\n"
_WORD_VIEW = "CREATE VIEW live AS SELECT {}'41' FROM wax;\n"
_WAX = "CREATE TABLE wax (ax TEXT);\n"


@pytest.mark.parametrize("gap", ["", " "], ids=["blob first", "bare word first"])
async def test_a_blob_literal_is_not_a_word_and_a_string(gap, tmp_path):
    """``x'41'`` is one blob token, so it never equals ``x '41'``, a column aliased as ``'41'``."""
    other = " " if gap == "" else ""
    reference = _store(tmp_path / "reference.db", _SCHEMA + _BLOB_VIEW.format(gap))
    store = _store(tmp_path / "managed.db", _SCHEMA + _BLOB_VIEW.format(other))
    reason = await _refused_against(reference, store)
    assert "view live" in reason


async def test_a_word_ending_in_x_before_a_quote_is_not_a_blob(tmp_path):
    """Only a whole word of one ``x`` starts a blob, so ``ax '41'`` is the column ``ax`` and the
    string ``'41'``, and whitespace between them is set aside as for any other two tokens."""
    reference = _store(tmp_path / "reference.db", _SCHEMA + _WAX + _WORD_VIEW.format("ax "))
    store = _store(tmp_path / "managed.db", _SCHEMA + _WAX + _WORD_VIEW.format("ax"))
    db = await _open(store, _profile(reference))
    await db.close()


_TRIGGER_A = (
    "CREATE TRIGGER fa AFTER INSERT ON events BEGIN "
    "UPDATE runs SET note = 'a' WHERE id = NEW.run_id; END;\n"
)
_TRIGGER_B = (
    "CREATE TRIGGER fb AFTER INSERT ON events BEGIN "
    "UPDATE runs SET note = 'b' WHERE id = NEW.run_id; END;\n"
)


async def test_two_triggers_in_the_other_order_are_refused(tmp_path):
    """SQLite fires a table's triggers in the order it created them, so the same two triggers in
    another order are another closure."""
    reference = _store(tmp_path / "reference.db", _SCHEMA + _TRIGGER_A + _TRIGGER_B)
    store = _store(tmp_path / "managed.db", _SCHEMA + _TRIGGER_B + _TRIGGER_A)
    with closing(sqlite3.connect(reference)) as conn:
        closure = read_closure(conn, ["runs", "events"])
    assert {(o.name, o.position) for o in closure.objects} == {("fa", 0), ("fb", 1)}
    reason = await _refused_against(reference, store)
    assert "trigger fa is at position 1" in reason
    assert "trigger fb is at position 0" in reason
    # Each moved trigger is reported once, by its position, and never also as missing.
    assert "missing or changed" not in reason


async def test_two_triggers_in_the_same_order_are_admitted(tmp_path):
    reference = _store(tmp_path / "reference.db", _SCHEMA + _TRIGGER_A + _TRIGGER_B)
    store = _store(tmp_path / "managed.db", _SCHEMA + _TRIGGER_A + _TRIGGER_B)
    db = await _open(store, _profile(reference))
    await db.close()


_BEFORE_INSERT = "CREATE TRIGGER tb BEFORE INSERT ON events BEGIN SELECT 1; END;\n"
_AFTER_UPDATE = "CREATE TRIGGER tu AFTER UPDATE ON events BEGIN SELECT 2; END;\n"


async def test_triggers_for_different_events_in_the_other_order_are_admitted(tmp_path):
    """SQLite orders a table's triggers by creation only among those for one event and timing,
    so a BEFORE INSERT and an AFTER UPDATE trigger created in the other order are the same
    closure."""
    reference = _store(tmp_path / "reference.db", _SCHEMA + _BEFORE_INSERT + _AFTER_UPDATE)
    store = _store(tmp_path / "managed.db", _SCHEMA + _AFTER_UPDATE + _BEFORE_INSERT)
    with closing(sqlite3.connect(store)) as conn:
        closure = read_closure(conn, ["runs", "events"])
    assert {(o.name, o.position) for o in closure.objects} == {("tb", 0), ("tu", 0)}
    db = await _open(store, _profile(reference))
    await db.close()


async def test_a_trigger_without_a_timing_is_ordered_with_the_before_triggers(tmp_path):
    """A trigger that names no timing fires BEFORE, so it shares its place in the order with the
    BEFORE triggers for its event."""
    schema = (
        _SCHEMA
        + "CREATE TRIGGER t1 INSERT ON events BEGIN SELECT 1; END;\n"
        + "CREATE TRIGGER t2 BEFORE INSERT ON events BEGIN SELECT 2; END;\n"
    )
    with closing(sqlite3.connect(_store(tmp_path / "store.db", schema))) as conn:
        closure = read_closure(conn, ["runs", "events"])
    assert {(o.name, o.position) for o in closure.objects} == {("t1", 0), ("t2", 1)}


_LEDGER_TRIGGER = (
    "CREATE TRIGGER keep AFTER INSERT ON events BEGIN "
    "INSERT INTO ledger (entry) VALUES (NEW.body); END;\n"
)
_LEDGER = "CREATE TABLE ledger (entry TEXT);\n"


def _event_insert_succeeds(schema: str) -> bool:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.executescript(schema)
        try:
            conn.execute("INSERT INTO events (run_id, body) VALUES ('r', 'b')")
        except sqlite3.OperationalError:
            return False
        return True


async def test_a_table_only_the_stores_closure_reaches_is_refused(tmp_path):
    """A closure trigger that writes a table the profile's store lacks fails there and succeeds
    in a store that has the table, so a table only the store's closure reaches refuses it."""
    reference_schema = _SCHEMA + _LEDGER_TRIGGER
    store_schema = _SCHEMA + _LEDGER + _LEDGER_TRIGGER
    assert not _event_insert_succeeds(reference_schema)
    assert _event_insert_succeeds(store_schema)
    reference = _store(tmp_path / "reference.db", reference_schema)
    store = _store(tmp_path / "managed.db", store_schema)
    reason = await _refused_against(reference, store)
    assert "table ledger is not in the profile" in reason


# A quoted name that a bare word reads otherwise is not that word (D2)


def _with_column(definition: str) -> str:
    return _SCHEMA.replace(_NOTE_COLUMN, f"{_NOTE_COLUMN}\n    {definition},", 1)


def _profile_with_column(reference: Path, name: str) -> CompatibilityProfile:
    runs = _WRITES["runs"]
    unwritten = {**runs.unwritten, name: "the managing application writes it"}
    writes = {**_WRITES, "runs": TableWrites(runs.insert, runs.update, unwritten)}
    return _profile(reference, writes=writes)


def _runs_accepts(schema: str, column: str, value: str | None) -> bool:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.executescript(schema)
        try:
            conn.execute(
                f"INSERT INTO runs (id, status, {column}) VALUES ('r', 'running', ?)", (value,)
            )
        except sqlite3.IntegrityError:
            return False
        return True


# (column as SQL, column as PRAGMA names it, the reference's definition, the store's, a value
# the reference refuses and the store accepts)
_QUOTED_NAME_PAIRS = {
    "double-quoted null": (
        '"null"',
        "null",
        '"null" TEXT CHECK ("null" IS NULL)',
        '"null" TEXT CHECK (null IS NULL)',
        "value",
    ),
    "bracketed null": (
        '"null"',
        "null",
        "[null] TEXT CHECK ([null] IS NULL)",
        "[null] TEXT CHECK (null IS NULL)",
        "value",
    ),
    "a name holding quotes": (
        '"""x"""',
        '"x"',
        '"""x""" TEXT CHECK ("""x""" IS NOT NULL)',
        '"""x""" TEXT CHECK ("x" IS NOT NULL)',
        None,
    ),
}


@pytest.mark.parametrize(
    ("column", "name", "reference_sql", "store_sql", "value"),
    _QUOTED_NAME_PAIRS.values(),
    ids=_QUOTED_NAME_PAIRS,
)
async def test_a_quoted_name_is_not_the_bare_word_or_literal_spelled_alike(
    column, name, reference_sql, store_sql, value, tmp_path
):
    """``"null"`` naming a column is that column, the bare ``null`` is the value NULL, and
    ``"x"`` naming no column is a string, so a CHECK on the column and a CHECK on the value read
    alike once folded and still refuse different rows."""
    reference_schema, store_schema = _with_column(reference_sql), _with_column(store_sql)
    assert not _runs_accepts(reference_schema, column, value)
    assert _runs_accepts(store_schema, column, value)
    reference = _store(tmp_path / "reference.db", reference_schema)
    store = _store(tmp_path / "managed.db", store_schema)
    reason = await _refused_against(reference, store, _profile_with_column(reference, name))
    assert "runs" in reason


_SAME_NAME_PAIRS = {
    "null in two quote styles": (
        "null",
        '"null" TEXT CHECK ("null" IS NULL)',
        "[null] TEXT CHECK (`null` IS NULL)",
    ),
    "a plain name quoted and bare": (
        "label",
        '"label" TEXT CHECK ("label" IS NOT NULL)',
        "label TEXT CHECK (label IS NOT NULL)",
    ),
}


@pytest.mark.parametrize(
    ("name", "reference_sql", "store_sql"), _SAME_NAME_PAIRS.values(), ids=_SAME_NAME_PAIRS
)
async def test_one_name_quoted_two_ways_is_admitted(name, reference_sql, store_sql, tmp_path):
    reference = _store(tmp_path / "reference.db", _with_column(reference_sql))
    store = _store(tmp_path / "managed.db", _with_column(store_sql))
    db = await _open(store, _profile_with_column(reference, name))
    await db.close()


_NAMED_SPELLINGS = {
    "referenced column": (
        "run_id TEXT NOT NULL REFERENCES runs(id)",
        'run_id TEXT NOT NULL REFERENCES runs("id")',
    ),
    "constraint name": (
        "CONSTRAINT uq UNIQUE (run_id, body)",
        'CONSTRAINT "uq" UNIQUE (run_id, body)',
    ),
}
_NAMED_SITES = {
    "referenced column": "run_id TEXT NOT NULL REFERENCES runs(id)",
    "constraint name": "UNIQUE (run_id, body)",
}


@pytest.mark.parametrize("site", _NAMED_SPELLINGS)
async def test_a_quoted_token_sqlite_reads_as_a_name_is_compared_as_one(site, tmp_path):
    """The column list after ``REFERENCES <table>`` and the word after ``CONSTRAINT`` are names,
    so quoting them changes nothing."""
    reference_sql, store_sql = _NAMED_SPELLINGS[site]
    reference = _store(
        tmp_path / "reference.db", _SCHEMA.replace(_NAMED_SITES[site], reference_sql, 1)
    )
    store = _store(tmp_path / "managed.db", _SCHEMA.replace(_NAMED_SITES[site], store_sql, 1))
    db = await _open(store, _profile(reference))
    await db.close()


_GATE = "CREATE VIEW gate AS SELECT 1 AS ok{where};\n"
_GATE_TRIGGER = (
    "CREATE TRIGGER guard BEFORE INSERT ON runs WHEN EXISTS (SELECT 1 FROM gate) "
    "BEGIN SELECT RAISE(ABORT, 'frozen'); END;\n"
)


async def test_a_view_a_closure_trigger_reads_is_part_of_the_closure(tmp_path):
    """A closure trigger that reads a view the closure does not name still reads it, so what that
    view holds is compared."""
    reference = _store(tmp_path / "reference.db", _SCHEMA + _GATE.format(where="") + _GATE_TRIGGER)
    store = _store(
        tmp_path / "managed.db", _SCHEMA + _GATE.format(where=" WHERE 0") + _GATE_TRIGGER
    )
    reason = await _refused_against(reference, store)
    assert "view gate" in reason


async def test_the_same_view_a_closure_trigger_reads_is_admitted(tmp_path):
    schema = _SCHEMA + _GATE.format(where=" WHERE 1") + _GATE_TRIGGER
    reference = _store(tmp_path / "reference.db", schema)
    store = _store(tmp_path / "managed.db", schema)
    db = await _open(store, _profile(reference))
    await db.close()


async def test_a_chain_of_views_a_closure_trigger_reads_is_followed(tmp_path):
    """The closure is a fixed point: a trigger reads ``a``, ``a`` reads ``b``, and a difference in
    ``b`` refuses the store."""
    tail = (
        "CREATE VIEW b AS SELECT 1 AS ok;\n"
        "CREATE VIEW a AS SELECT ok FROM b;\n"
        "CREATE TRIGGER guard BEFORE INSERT ON runs WHEN EXISTS (SELECT 1 FROM a) "
        "BEGIN SELECT RAISE(ABORT, 'frozen'); END;\n"
    )
    other = tail.replace("SELECT 1 AS ok;", "SELECT 1 AS ok WHERE 0;", 1)
    reference = _store(tmp_path / "reference.db", _SCHEMA + tail)
    store = _store(tmp_path / "managed.db", _SCHEMA + other)
    reason = await _refused_against(reference, store)
    assert "view b" in reason


async def test_a_table_only_a_closure_trigger_reads_is_compared(tmp_path):
    """A table a closure object reads joins the closure, so a difference in it refuses the store
    even though the profile writes nothing there."""
    tail = (
        "CREATE TABLE seq (id INTEGER PRIMARY KEY, counter TEXT);\n"
        "CREATE TRIGGER bump AFTER INSERT ON runs BEGIN "
        "UPDATE seq SET counter = NEW.status; END;\n"
    )
    reference = _store(tmp_path / "reference.db", _SCHEMA + tail)
    store = _store(
        tmp_path / "managed.db",
        _SCHEMA + tail.replace("counter TEXT", "counter TEXT NOT NULL", 1),
    )
    with closing(sqlite3.connect(reference)) as conn:
        closure = read_closure(conn, ["runs", "events"])
    assert "seq" in closure.table_names
    assert set(_WRITES) == {"runs", "events"}
    reason = await _refused_against(reference, store)
    assert "column seq.counter" in reason
