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

from lionagi.state import (
    SchemaTooNewError,
    StateDB,
    UnreadableStoreError,
    UnrecognizedStoreError,
)
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


def _lineage_store(path: Path, *, journal: bool) -> None:
    """A native store whose interrupted rollback-journal transaction wrote another application's
    lineage row into the file, beside its journal; or a store whose file names that application and
    whose log has committed lionagi's own row, with no index."""
    source = path.with_name(f"source-{path.name}")
    shutil.copyfile(path, source)
    with closing(sqlite3.connect(source, isolation_level=None)) as conn:
        if journal:
            conn.execute("PRAGMA journal_mode = delete")
            conn.execute("PRAGMA cache_size = -64")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('lineage', 'manager')"
            )
            conn.execute("CREATE TABLE unfinished (v TEXT)")
            for _ in range(2000):
                conn.execute("INSERT INTO unfinished (v) VALUES (?)", ("x" * 4000,))
            shutil.copyfile(source, path)
            shutil.copyfile(f"{source}-journal", f"{path}-journal")
            conn.execute("ROLLBACK")
        else:
            conn.execute("PRAGMA journal_mode = wal")
            conn.execute("PRAGMA wal_autocheckpoint = 0")
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES "
                f"('version', '{SCHEMA_VERSION}'), ('lineage', 'manager')"
            )
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('lineage', 'lionagi')"
            )
            shutil.copyfile(source, path)
            shutil.copyfile(f"{source}-wal", f"{path}-wal")
            Path(f"{source}-shm").unlink(missing_ok=True)
    assert (Path(f"{path}-journal") if journal else Path(f"{path}-wal")).stat().st_size


@pytest.mark.parametrize("journal", [True, False], ids=["journal", "log"])
async def test_a_store_whose_journal_or_log_cannot_be_read_is_refused_before_any_connection(
    tmp_path, monkeypatch, journal
):
    """The file's own lineage row can be part of an unfinished transaction, or stale beside a log
    that holds the committed one, so when that file cannot be read the open refuses the store as
    unreadable rather than naming the file's row, and leaves every file of the store as it was."""
    from lionagi.state import db as db_mod

    path = tmp_path / "store.db"
    await _open_and_close(path)
    _lineage_store(path, journal=journal)
    before = _files(path)
    monkeypatch.setattr(db_mod, "_read_lineage_from_a_copy", lambda p, suffix: db_mod._UNREAD)
    db = StateDB(path)
    with pytest.raises(UnreadableStoreError, match="could not be read"):
        await db.open()
    assert db._engine is None
    assert _files(path) == before


async def test_a_journal_a_commit_removes_after_it_was_seen_reads_the_store_again(
    tmp_path, monkeypatch
):
    """A commit removes the rollback journal between the open seeing it and copying it. The open
    reads the store again, now without a journal, rather than refusing it as unreadable."""
    from lionagi.state import db as db_mod

    path = tmp_path / "store.db"
    await _open_and_close(path)
    Path(f"{path}-journal").write_bytes(db_mod._JOURNAL_HEADER + bytes(504))
    real = db_mod._journal_has_a_header

    def committed_after_the_check(opened: str) -> bool:
        seen = real(opened)
        Path(f"{opened}-journal").unlink(missing_ok=True)
        return seen

    monkeypatch.setattr(db_mod, "_journal_has_a_header", committed_after_the_check)
    await _open_and_close(path)


async def test_a_log_a_closing_writer_removes_while_the_store_is_copied_reads_the_store_again(
    tmp_path, monkeypatch
):
    """Another process's last connection closes while the open copies the store, so SQLite folds
    the log into the file and removes it before the log is copied. The open reads the store again
    rather than refusing it as unreadable."""
    from lionagi.state import db as db_mod

    path = tmp_path / "store.db"
    await _open_and_close(path)
    # Closed from the open's worker thread, as another process's close would land at any time.
    writer = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute("INSERT INTO schema_meta (key, value) VALUES ('written', '1')")
        assert Path(f"{path}-wal").stat().st_size > 0
        # The read-only read of the log through its index fails, so the log is read from a copy.
        read = db_mod._read_lineage
        monkeypatch.setattr(
            db_mod,
            "_read_lineage",
            lambda target: None if "readonly_shm" in str(target) else read(target),
        )
        real = db_mod._copy_store_file

        def closed_during_the_copy(source: str, copy: str) -> None:
            real(source, copy)
            writer.close()
            Path(f"{source}-wal").unlink(missing_ok=True)

        monkeypatch.setattr(db_mod, "_copy_store_file", closed_during_the_copy)
        await _open_and_close(path)
    finally:
        writer.close()


def _wal_reopened(path: Path, script: str) -> None:
    """Run *script* on a store in its log, leaving the log beside the file uncheckpointed: a
    reader holds the store open, so the writer's close does not fold the log into the file."""
    with closing(sqlite3.connect(path)) as reader:
        assert reader.execute("PRAGMA journal_mode = wal").fetchone() == ("wal",)
        with closing(sqlite3.connect(path, isolation_level=None)) as conn:
            conn.execute("PRAGMA wal_autocheckpoint = 0")
            conn.execute(script)
        assert Path(f"{path}-wal").stat().st_size > 0


async def test_a_kept_engine_reads_the_store_before_it_reuses_a_connection(tmp_path, monkeypatch):
    """An open that fails before its schema transaction keeps its engine. Its next open reads the
    store before it runs any step on a connection, so a store another writer moved to a later
    version is refused without the schema transaction or a new writable connection."""
    from lionagi.state import db as db_mod

    path = tmp_path / "kept.db"
    await _open_and_close(path)
    db = StateDB(path)
    monkeypatch.setattr(
        StateDB,
        "_refuse_newer_schema",
        lambda self, conn: _raise(RuntimeError("before the schema")),
    )
    with pytest.raises(RuntimeError, match="before the schema"):
        await db.open()
    assert db._engine is not None and not db._schema_applied
    monkeypatch.undo()
    await db._engine.dispose()
    _wal_reopened(
        path, f"UPDATE schema_meta SET value = '{int(SCHEMA_VERSION) + 1}' WHERE key = 'version'"
    )
    before = _files(path)
    connections = []
    reads = []
    real = StateDB._read_lineage_without_writing
    monkeypatch.setattr(
        StateDB, "_refuse_a_managed_store", lambda self, conn: connections.append(conn)
    )
    monkeypatch.setattr(
        StateDB,
        "_read_lineage_without_writing",
        lambda self, held: (reads.append(self.url), real(self, held))[1],
    )
    with pytest.raises(SchemaTooNewError):
        await db.open()
    assert reads, "the open read the store before its schema"
    assert connections == []
    assert db._engine is None
    assert _files(path) == before


async def _raise(exc):
    raise exc


async def test_a_kept_engine_whose_schema_committed_reads_a_replaced_store_as_an_open_does(
    tmp_path, monkeypatch
):
    """An open that fails after its schema transaction committed keeps its engine, and its next
    open runs that transaction again. Until it commits, that open's connections read the store as
    an open's do, so a store another writer put in its place is refused before a connection's
    settings change it, and left as it was."""
    path = tmp_path / "kept.db"
    db = StateDB(path)
    monkeypatch.setattr(
        StateDB,
        "_backfill_session_ended_at_once",
        lambda self: _raise(RuntimeError("after the schema")),
    )
    with pytest.raises(RuntimeError, match="after the schema"):
        await db.open()
    assert db._engine is not None and db._schema_committed and not db._schema_applied
    monkeypatch.undo()
    await db._engine.dispose()
    replaced = {}

    def replaced_after_the_read(self, held) -> None:
        for name in (path.name, f"{path.name}-wal", f"{path.name}-shm"):
            (tmp_path / name).unlink(missing_ok=True)
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute("PRAGMA journal_mode = delete").fetchone() == ("delete",)
            conn.execute("CREATE TABLE app_users (id TEXT PRIMARY KEY)")
            conn.commit()
        replaced.update(_files(path))

    monkeypatch.setattr(StateDB, "_read_lineage_without_writing", replaced_after_the_read)
    with pytest.raises(UnrecognizedStoreError, match="app_users"):
        await db.open()
    assert db._engine is None
    assert _rows(path, "PRAGMA journal_mode") == [("delete",)]
    assert _files(path) == replaced


async def test_a_connection_opened_after_the_schema_is_applied_does_not_refuse_a_newer_version(
    tmp_path, monkeypatch
):
    """The schema transaction committed and its lineage row is written, so a version another
    writer records afterwards is refused by an open, never by an ordinary call: a connection this
    open makes later reads the store and refuses only a lineage row naming another application."""
    path = tmp_path / "applied.db"
    db = StateDB(path)
    monkeypatch.setattr(
        StateDB, "_backfill_session_ended_at_once", lambda self: _raise(RuntimeError("later"))
    )
    with pytest.raises(RuntimeError, match="later"):
        await db.open()
    assert db._engine is not None and not db._schema_applied
    monkeypatch.undo()
    await db._engine.dispose()
    _execute(
        path,
        f"UPDATE schema_meta SET value = '{int(SCHEMA_VERSION) + 1}' WHERE key = 'version';"
        "CREATE TABLE app_users (id TEXT PRIMARY KEY);",
    )
    rows = await db.fetch_all("SELECT value FROM schema_meta WHERE key = 'lineage'")
    assert rows == [{"value": "lionagi"}]
    await db.close()


async def test_a_connection_opened_after_the_open_does_not_read_the_store_tables(
    tmp_path, monkeypatch
):
    """A table another writer adds after the open is met by that writer's own connection, not by
    an ordinary call of this handle: a connection made after the schema is applied reads the
    ``schema_meta`` rows alone."""
    from lionagi.state import db as db_mod

    path = tmp_path / "added-later.db"
    db = StateDB(path)
    await db.open()
    try:
        reads = []
        real = db_mod._sqlite_store_rows

        def recording(execute, **kwargs):
            reads.append(kwargs.get("tables", True))
            return real(execute, **kwargs)

        monkeypatch.setattr(db_mod, "_sqlite_store_rows", recording)
        await db._engine.dispose()
        _execute(path, "CREATE TABLE app_users (id INTEGER PRIMARY KEY);")
        assert await db.fetch_all("SELECT value FROM schema_meta WHERE key = 'lineage'") == [
            {"value": "lionagi"}
        ]
        assert reads == [False]
    finally:
        await db.close()


async def test_a_connection_opened_after_the_open_refuses_a_foreign_lineage_row(tmp_path):
    """A lineage row written after the open reaches a connection the open makes later, which
    refuses it; the store is not migrated to it."""
    from lionagi.state import ForeignLineageError

    path = tmp_path / "foreign-later.db"
    db = StateDB(path)
    await db.open()
    try:
        await db._engine.dispose()
        _execute(
            path,
            "UPDATE schema_meta SET value = 'manager' WHERE key = 'lineage';"
            "CREATE TABLE app_users (id TEXT PRIMARY KEY);",
        )
        with pytest.raises(ForeignLineageError, match="'manager' as the application"):
            await db.fetch_all("SELECT 1")
    finally:
        await db.close()
