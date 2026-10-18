"""A native store names lionagi as its lineage, and a store without the row is adopted only when
its shape is one lionagi produced (ADR-0128 D7)."""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import aiosqlite
import pytest
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateTable

from lionagi.state import SchemaTooNewError, StateDB, UnrecognizedStoreError
from lionagi.state.db import _LIONAGI_TABLES, _OPERATOR_TABLES, SCHEMA_VERSION
from lionagi.state.schema_meta import metadata

_REPO = Path(__file__).resolve().parents[2]
_CREATE_TABLE = re.compile(r"CREATE TABLE IF NOT EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _rows(path: Path, sql: str) -> list[tuple]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchall()


def _execute(path: Path, script: str) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(script)
        conn.commit()


def _files(path: Path) -> dict[str, tuple[bytes, int]]:
    """Every file of the store, with its bytes and modification time."""
    return {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns)
        for p in path.parent.iterdir()
        if p.name.startswith(path.name)
    }


async def _open_and_close(path: Path) -> None:
    db = StateDB(path)
    await db.open()
    await db.close()


async def test_a_new_store_records_lionagi_as_its_lineage(tmp_path):
    path = tmp_path / "new.db"
    await _open_and_close(path)
    assert _rows(path, "SELECT value FROM schema_meta WHERE key = 'lineage'") == [("lionagi",)]
    assert _rows(path, "SELECT value FROM schema_meta WHERE key = 'version'") == [(SCHEMA_VERSION,)]


@pytest.mark.parametrize("version", ["1", SCHEMA_VERSION])
async def test_a_store_from_before_the_lineage_row_is_adopted_and_stamped(tmp_path, version):
    path = tmp_path / "older.db"
    await _open_and_close(path)
    _execute(
        path,
        f"DELETE FROM schema_meta WHERE key = 'lineage';"
        f"UPDATE schema_meta SET value = '{version}' WHERE key = 'version';",
    )
    await _open_and_close(path)
    assert _rows(path, "SELECT value FROM schema_meta WHERE key = 'lineage'") == [("lionagi",)]
    assert _rows(path, "SELECT value FROM schema_meta WHERE key = 'version'") == [(SCHEMA_VERSION,)]


async def test_a_store_holding_only_tables_a_lionagi_component_creates_opens_natively(tmp_path):
    """The Studio Operator store creates its tables in the StateDB file on its own, so they can be
    there before any StateDB open has written schema_meta."""
    path = tmp_path / "operator-first.db"
    _execute(path, "CREATE TABLE studio_operator_turns (id TEXT PRIMARY KEY);")
    await _open_and_close(path)
    assert _rows(path, "SELECT value FROM schema_meta WHERE key = 'lineage'") == [("lionagi",)]
    assert _rows(path, "SELECT count(*) FROM sqlite_master WHERE name = 'sessions'") == [(1,)]


async def test_a_store_holding_a_table_lionagi_never_creates_is_refused_and_left_as_it_was(
    tmp_path,
):
    path = tmp_path / "someone-elses.db"
    _execute(
        path,
        "CREATE TABLE sessions (id TEXT PRIMARY KEY);"
        "CREATE TABLE app_users (id INTEGER PRIMARY KEY);"
        "INSERT INTO app_users VALUES (1);",
    )
    before = _files(path)
    db = StateDB(path)
    with pytest.raises(UnrecognizedStoreError, match="app_users"):
        await db.open()
    assert db._engine is None
    assert _files(path) == before
    assert _rows(path, "PRAGMA journal_mode") == [("delete",)]


@pytest.mark.parametrize(
    "rows",
    [
        "",
        "INSERT INTO schema_meta VALUES ('version', 'not-a-version');",
        "INSERT INTO schema_meta VALUES ('version', '0');",
        "INSERT INTO schema_meta VALUES ('version', '03');",
        "INSERT INTO schema_meta VALUES ('version', ' 3');",
        "INSERT INTO schema_meta VALUES ('version', '');",
    ],
    ids=["no-version", "word", "zero", "padded", "spaced", "empty"],
)
async def test_a_store_without_the_lineage_row_and_a_version_no_release_writes_is_refused(
    tmp_path, rows
):
    path = tmp_path / "unknown.db"
    _execute(
        path,
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "CREATE TABLE sessions (id TEXT PRIMARY KEY);" + rows,
    )
    before = _files(path)
    db = StateDB(path)
    with pytest.raises(UnrecognizedStoreError):
        await db.open()
    assert db._engine is None
    assert _files(path) == before
    assert _rows(path, "PRAGMA journal_mode") == [("delete",)]


async def test_a_store_without_the_lineage_row_and_a_higher_version_is_too_new(tmp_path):
    path = tmp_path / "later.db"
    _execute(
        path,
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        f"INSERT INTO schema_meta VALUES ('version', '{int(SCHEMA_VERSION) + 10}');",
    )
    before = _files(path)
    db = StateDB(path)
    with pytest.raises(SchemaTooNewError):
        await db.open()
    assert db._engine is None
    assert _files(path) == before
    assert _rows(path, "PRAGMA journal_mode") == [("delete",)]


async def test_a_lionagi_store_with_a_version_it_cannot_order_is_stamped(tmp_path):
    """The lineage row says the store is this package's, so a version that cannot be compared is
    replaced by the stamp, as before the row existed."""
    path = tmp_path / "native.db"
    await _open_and_close(path)
    _execute(path, "UPDATE schema_meta SET value = 'not-a-version' WHERE key = 'version';")
    await _open_and_close(path)
    assert _rows(path, "SELECT value FROM schema_meta WHERE key = 'version'") == [(SCHEMA_VERSION,)]


def _created_tables(path: Path) -> set[str]:
    return set(_CREATE_TABLE.findall(path.read_text(encoding="utf-8")))


def test_the_operator_tables_are_the_ones_the_operator_store_creates():
    created = _created_tables(_REPO / "lionagi" / "studio" / "operator" / "store.py")
    assert "studio_operator_turns" in created
    assert created == _OPERATOR_TABLES


def test_every_table_lionagi_creates_in_the_state_file_is_one_it_recognizes():
    """A component that starts creating a table in the StateDB file must add it here, or a store
    that component touched first would be refused."""
    sources = [_REPO / "lionagi" / "state" / "schema.sql"]
    sources += sorted((_REPO / "lionagi" / "studio").rglob("*.py"))
    created = set().union(*(_created_tables(p) for p in sources))
    assert {"sessions", "approvals", "run_tags", "studio_operator_frames"} <= created
    assert created <= _LIONAGI_TABLES


def _rows_only_in_the_log(path: Path, script: str) -> None:
    """A store at *path* whose rows are all in its write-ahead log, with no index beside it: a
    writer that stopped without a checkpoint."""
    source = path.with_name(f"writer-{path.name}")
    with closing(sqlite3.connect(source, isolation_level=None)) as conn:
        conn.execute("PRAGMA journal_mode = wal")
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.executescript(script)
        shutil.copyfile(source, path)
        shutil.copyfile(f"{source}-wal", f"{path}-wal")


@pytest.mark.parametrize(
    "script",
    [
        "CREATE TABLE app_users (id INTEGER PRIMARY KEY); INSERT INTO app_users VALUES (1);",
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "CREATE TABLE sessions (id TEXT PRIMARY KEY);",
    ],
    ids=["foreign-table", "no-version"],
)
async def test_a_store_refused_on_rows_only_in_its_log_keeps_its_log(tmp_path, script):
    """The rows are read from the log before any writable connection exists, so the refusal does
    not leave a connection whose close would fold the log into the file."""
    path = tmp_path / "logged.db"
    _rows_only_in_the_log(path, script)
    before = _files(path)
    assert set(before) == {"logged.db", "logged.db-wal"}
    db = StateDB(path)
    with pytest.raises(UnrecognizedStoreError):
        await db.open()
    assert db._engine is None
    assert _files(path) == before


async def test_a_store_with_a_legacy_table_and_no_version_is_refused_before_its_backup(tmp_path):
    """The legacy definitions rebuild copies the store aside after a checkpoint; a store refused
    on its rows gets neither."""
    path = tmp_path / "legacy.db"
    _execute(
        path,
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "CREATE TABLE sessions (id TEXT PRIMARY KEY);"
        "CREATE TABLE definitions (id TEXT PRIMARY KEY, kind TEXT NOT NULL "
        "CHECK (kind IN ('agent','playbook')));",
    )
    before = _files(path)
    with pytest.raises(UnrecognizedStoreError):
        await _open_and_close(path)
    assert _files(path) == before


async def test_a_refusal_from_a_new_connection_is_not_logged_as_a_failed_inspection(
    tmp_path, monkeypatch, caplog
):
    """A store that changed after the reads before any writable connection is refused by the
    first connection, which the column reconciliation passes on without logging it."""
    path = tmp_path / "changed.db"
    _execute(path, "CREATE TABLE app_users (id INTEGER PRIMARY KEY);")
    monkeypatch.setattr(StateDB, "_read_lineage_without_writing", lambda self, held: None)
    with caplog.at_level(logging.ERROR, logger="lionagi.state.db"):
        with pytest.raises(UnrecognizedStoreError, match="app_users"):
            await _open_and_close(path)
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []


@pytest.mark.parametrize(
    "script",
    [
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO schema_meta VALUES ('version', '1');"
        "CREATE TABLE app_users (id INTEGER PRIMARY KEY);",
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO schema_meta VALUES ('version', '1');"
        "CREATE TABLE app_users (id INTEGER PRIMARY KEY);"
        "CREATE TABLE studio_operator_turns (id TEXT PRIMARY KEY);",
    ],
    ids=["only-foreign", "with-an-operator-table"],
)
async def test_a_store_without_the_lineage_row_or_sessions_holding_a_foreign_table_is_refused(
    tmp_path, script
):
    """Every lionagi release created the sessions table on the open that wrote the version row,
    so a store with a version, no lineage row and no sessions table, holding a table lionagi never
    creates, is not one lionagi created."""
    path = tmp_path / "versioned.db"
    _execute(path, script)
    before = _files(path)
    db = StateDB(path)
    with pytest.raises(UnrecognizedStoreError, match=r"\(app_users\) but no sessions table"):
        await db.open()
    assert db._engine is None
    assert _files(path) == before


@pytest.mark.parametrize(
    "script",
    [
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO schema_meta VALUES ('version', '1');",
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO schema_meta VALUES ('version', '1');"
        "CREATE TABLE app_users (id INTEGER PRIMARY KEY);",
    ],
    ids=["no-sessions-no-foreign-table", "foreign-table-beside-sessions"],
)
async def test_a_store_without_the_lineage_row_is_adopted_by_its_sessions_or_its_tables(
    tmp_path, script
):
    """A user's own table beside lionagi's in a store lionagi created is kept, and a store whose
    first open stopped before it created the sessions table holds only lionagi's tables."""
    path = tmp_path / "versioned.db"
    _execute(path, script)
    if "app_users" in script:
        _execute(
            path, str(CreateTable(metadata.tables["sessions"]).compile(dialect=sqlite_dialect()))
        )
    await _open_and_close(path)
    assert _rows(path, "SELECT value FROM schema_meta WHERE key = 'lineage'") == [("lionagi",)]


async def _refusal(path: Path, reader: str) -> UnrecognizedStoreError:
    """The refusal of the store at *path* by one of the places a writable open reads its rows."""
    db = StateDB(path)
    with pytest.raises(UnrecognizedStoreError) as caught:
        if reader == "before-any-connection":
            db._read_lineage_without_writing(())
        elif reader == "connection":
            with closing(sqlite3.connect(path)) as conn:
                db._refuse_a_managed_store(conn)
        elif reader == "migration":
            engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
            try:
                async with engine.connect() as conn:
                    await db._refuse_newer_schema(conn)
            finally:
                await engine.dispose()
        else:
            async with aiosqlite.connect(path) as driver:
                await db._refuse_newer_sqlite_schema(driver)
    return caught.value


_READERS = ["before-any-connection", "connection", "migration", "rebuild"]


@pytest.mark.parametrize("reader", _READERS)
async def test_each_reader_refuses_a_table_whose_name_only_looks_like_sqlites_own(tmp_path, reader):
    """SQLite's own tables are named ``sqlite_``; the underscore is matched literally, so a table
    named ``sqliteXapp`` is someone's."""
    path = tmp_path / "lookalike.db"
    _execute(path, "CREATE TABLE sqliteXapp (id INTEGER PRIMARY KEY);")
    assert "sqliteXapp" in str(await _refusal(path, reader))


@pytest.mark.parametrize("reader", _READERS)
async def test_each_reader_names_three_foreign_tables_and_marks_the_rest(tmp_path, reader):
    path = tmp_path / "many.db"
    long_name = "t_" + "x" * 60
    _execute(
        path,
        "".join(f"CREATE TABLE {name} (id INTEGER);" for name in ("t_a", "t_b", "t_c", long_name)),
    )
    message = str(await _refusal(path, reader))
    assert "(t_a, t_b, t_c, ...)" in message
    assert long_name[:40] not in message


@pytest.mark.parametrize("reader", _READERS)
async def test_each_reader_refuses_a_schema_meta_without_key_and_value_columns(tmp_path, reader):
    path = tmp_path / "other-schema-meta.db"
    _execute(
        path, "CREATE TABLE schema_meta (version INTEGER); INSERT INTO schema_meta VALUES (3);"
    )
    assert "without the key and value columns" in str(await _refusal(path, reader))


async def test_the_backup_before_a_rebuild_refuses_a_store_first(tmp_path, monkeypatch):
    """The backup checkpoints the store, so it reads the rows itself rather than relying on the
    reads that ran before it; here those reads are switched off to stand for a store that changed
    after them."""
    path = tmp_path / "legacy.db"
    _execute(
        path,
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "CREATE TABLE definitions (id TEXT PRIMARY KEY, kind TEXT NOT NULL "
        "CHECK (kind IN ('agent','playbook')));",
    )

    async def nothing(self):
        return None

    monkeypatch.setattr(StateDB, "_read_lineage_without_writing", lambda self, held: None)
    monkeypatch.setattr(StateDB, "_refuse_a_managed_store", lambda self, dbapi_conn: None)
    monkeypatch.setattr(StateDB, "_reconcile_columns", nothing)
    with pytest.raises(UnrecognizedStoreError, match="no lineage or version row"):
        await _open_and_close(path)
    assert sorted(p.name for p in tmp_path.iterdir() if ".bak" in p.name) == []
