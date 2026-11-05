# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Compatibility-mode ownership (ADR-0128 D5) and a run's creating operations (Appendix A).

The managed store here is lionagi's own native schema with an owner column added to the tables
that carry one, a principals table, and another lineage's version stamp and lineage row: the shape
of a store a newer schema manages, built synthetically.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import text

from lionagi.state import compat
from lionagi.state.compat import (
    CompatibilityProfile,
    OwnerRule,
    StateCompatibilityRefused,
    TableWrites,
    _column_collation,
    _operation,
    _ordered_collation,
    _trigger_event,
    _trigger_owner_problems,
    read_closure,
)
from lionagi.state.db import ForeignLineageError, StateDB, UnreadableStoreError

OWNER = "owner_principal_id"
_OWNED = (
    "sessions",
    "branches",
    "progressions",
    "messages",
    "status_transitions",
    "invocations",
    "session_signals",
    "admin_events",
)
_TABLES = (*_OWNED, "message_types", "principals")

_INSERTS = {
    "progressions": {"id", "created_at", "collection", OWNER},
    "sessions": {
        "id",
        "cc_session_id",
        "run_id",
        "created_at",
        "node_metadata",
        "name",
        "user",
        "progression_id",
        "first_msg_id",
        "last_msg_id",
        "updated_at",
        "playbook_name",
        "agent_name",
        "invocation_kind",
        "show_topic",
        "show_play_name",
        "artifacts_path",
        "artifact_contract_json",
        "artifact_verification_json",
        "source_kind",
        "status",
        "started_at",
        "ended_at",
        "ended_at_is_approximate",
        "last_message_at",
        "invocation_id",
        "model",
        "provider",
        "effort",
        "agent_hash",
        "project",
        "project_source",
        "duration_ms",
        OWNER,
    },
    "branches": {
        "id",
        "created_at",
        "node_metadata",
        "user",
        "name",
        "session_id",
        "progression_id",
        "system_msg_id",
        "model",
        "provider",
        "agent_name",
        OWNER,
    },
    "messages": {
        "id",
        "created_at",
        "node_metadata",
        "content",
        "embedding",
        "sender",
        "recipient",
        "channel",
        "role",
        "lion_class",
        OWNER,
    },
    "message_types": {"lion_class"},
    "status_transitions": {
        "id",
        "entity_type",
        "entity_id",
        "previous_status",
        "status",
        "reason_code",
        "reason_summary",
        "evidence_refs",
        "source",
        "actor",
        "created_at",
        "metadata",
        OWNER,
    },
    "session_signals": {"id", "session_id", "seq", "kind", "op_id", "ts", "payload", OWNER},
    "admin_events": {"id", "created_at", "action", "target_id", "details", "actor", OWNER},
}
_UPDATES = {
    "sessions": {
        "status",
        "status_reason_code",
        "status_reason_summary",
        "status_evidence_refs",
        "last_message_at",
        "updated_at",
        "first_msg_id",
        "last_msg_id",
        "node_metadata",
        "artifact_verification_json",
        "ended_at",
        "ended_at_is_approximate",
        "duration_ms",
    },
    "progressions": {"collection"},
    "branches": {"system_msg_id"},
    "messages": {
        "node_metadata",
        "content",
        "embedding",
        "sender",
        "recipient",
        "channel",
        "role",
        "lion_class",
    },
    "invocations": {"session_count", "updated_at"},
}
_OPERATIONS = {
    "create_progression",
    "create_session",
    "create_branch",
    "insert_message",
    "persist_live_message",
    "insert_session_signal",
    "update_session",
    "update_artifact_verification",
    "merge_session_node_metadata",
    "update_status",
}


_OWNER_COLUMN = "TEXT NOT NULL DEFAULT 'principal:default'"


async def _managed_store(
    path: Path, owner_sql: str = _OWNER_COLUMN, owner_name: str = OWNER
) -> Path:
    native = StateDB(path)
    await native.open()
    await native.close()
    return _manage(path, owner_sql, owner_name)


def _manage(path: Path, owner_sql: str = _OWNER_COLUMN, owner_name: str = OWNER) -> Path:
    # The managing application's own columns, principals and version, and the lineage row it writes
    # when it creates the store (ADR-0128 D7). The store was built by a native open, which wrote
    # lionagi's own row; the manager's row replaces it.
    with closing(sqlite3.connect(path)) as conn:
        for table in _OWNED:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {owner_name} {owner_sql}")
        conn.executescript(
            """
            CREATE TABLE principals (id TEXT PRIMARY KEY, kind TEXT NOT NULL);
            INSERT INTO principals VALUES
                ('principal:a', 'human'), ('principal:b', 'human'), ('principal:default', 'human');
            UPDATE schema_meta SET value = '900' WHERE key = 'version';
            INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('lineage', 'manager');
            """
        )
        conn.commit()
    return path


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f'PRAGMA table_xinfo("{table}")')]


def _profile(store: Path, inserts: dict | None = None, **changes) -> CompatibilityProfile:
    inserts = _INSERTS if inserts is None else inserts
    with closing(sqlite3.connect(store)) as conn:
        closure = read_closure(conn, _TABLES)
        writes = {}
        for table in set(inserts) | set(_UPDATES):
            insert = inserts.get(table, set())
            unwritten = (
                {c: "left to the store's default" for c in _columns(conn, table) if c not in insert}
                if insert
                else {}
            )
            writes[table] = TableWrites(
                insert=insert, update=_UPDATES.get(table, set()), unwritten=unwritten
            )
    fields = {
        "name": "owned",
        "version": 1,
        "closure": closure,
        "writes": writes,
        "operations": _OPERATIONS,
        "owner": OwnerRule(column=OWNER, principals="principals"),
    }
    fields.update(changes)
    return CompatibilityProfile(**fields)


def _rows(store: Path, sql: str, params: tuple = ()) -> list:
    with closing(sqlite3.connect(store)) as conn:
        return conn.execute(sql, params).fetchall()


def _execute(store: Path, sql: str, params: tuple = ()) -> None:
    with closing(sqlite3.connect(store)) as conn:
        conn.execute(sql, params)
        conn.commit()


@pytest_asyncio.fixture
async def store(tmp_path: Path) -> Path:
    return await _managed_store(tmp_path / "managed.db")


@pytest_asyncio.fixture
async def db(store: Path):
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")
    await handle.open()
    try:
        yield handle
    finally:
        await handle.close()


def _session(session_id: str = "s1", **fields) -> dict:
    return {"id": session_id, "progression_id": "sp", "status": "running", **fields}


def _system_message(message_id: str = "m-sys", content: str = "you are a test") -> dict:
    return {
        "id": message_id,
        "created_at": 1.0,
        "content": {"text": content},
        "role": "system",
        "node_metadata": {"lion_class": "System"},
    }


async def _setup_run(db: StateDB) -> None:
    """The writes a fresh agent run's setup makes, in its order."""
    await db.create_progression("sp")
    await db.create_progression("bp")
    await db.create_session(_session())
    await db.insert_message(_system_message())
    await db.create_branch(
        {"id": "b1", "session_id": "s1", "progression_id": "bp", "system_msg_id": "m-sys"}
    )


async def test_the_shared_lookup_refuses_a_store_held_for_several_owners(store):
    """Handles for two owners write different runs' rows; a caller naming only the store does
    not say whose, so neither is handed out and no native handle is opened."""
    from lionagi.state import db as db_mod

    first = StateDB(store, compatibility=_profile(store), owner="principal:a")
    second = StateDB(store, compatibility=_profile(store), owner="principal:b")
    try:
        await db_mod.register_shared_db(first)
        await db_mod.register_shared_db(second)
        with pytest.raises(StateCompatibilityRefused, match="2 compatibility handles"):
            await db_mod.get_shared_db(store)
        assert db_mod._SHARED.get(first.url) is None
    finally:
        await db_mod.close_shared_db()


async def test_the_shared_lookup_hands_out_the_compatibility_handle_before_a_native_one(store):
    """A native handle registered for the store first is not handed out once a compatibility
    handle is registered for it: what the caller writes must meet the profile."""
    from lionagi.state import db as db_mod

    native = StateDB(store)
    compat = StateDB(store, compatibility=_profile(store), owner="principal:a")
    try:
        await db_mod.register_shared_db(native)
        await db_mod.register_shared_db(compat)
        assert await db_mod.get_shared_db(store) is compat
    finally:
        await db_mod.close_shared_db()


async def test_a_native_open_of_a_store_held_in_compatibility_mode_is_refused(db, store):
    """A native open would apply this package's schema to the managed store; a read-only open
    applies nothing and is left alone."""
    native = StateDB(store)
    with pytest.raises(StateCompatibilityRefused, match="a native open would apply"):
        await native.open()
    assert native._engine is None
    assert _rows(store, "SELECT value FROM schema_meta WHERE key = 'version'") == [("900",)]

    reader = StateDB(store, readonly=True)
    await reader.open()
    await reader.close()

    await db.close()
    from lionagi.state import db as db_mod

    assert db_mod._open_in_compatibility_mode(db.url) is None


async def test_the_shared_lookup_refuses_a_store_held_open_without_sharing_the_handle(db, store):
    """The caller cannot be given an unregistered compatibility handle, and a native one would
    write outside the profile, so the lookup refuses and registers nothing."""
    from lionagi.state import db as db_mod

    try:
        with pytest.raises(StateCompatibilityRefused, match="without sharing that handle"):
            await db_mod.get_shared_db(store)
        assert db_mod._SHARED.get(db.url) is None
        with pytest.raises(StateCompatibilityRefused, match="without sharing that handle"):
            await db_mod.register_shared_db(StateDB(store))
        assert db_mod._SHARED.get(db.url) is None
    finally:
        await db_mod.close_shared_db()


# Opens from another process (ADR-0128 D7): the in-process refusals above cover this process only.
# The managed store's lineage row, written by its manager, is what a second process's native open
# reads before it opens any writable connection.

_SNAPSHOT = """
import hashlib, json, os, sys

files = {}
for suffix in ("", "-wal", "-shm", "-journal"):
    path = sys.argv[1] + suffix
    if os.path.exists(path):
        with open(path, "rb") as handle:
            files[suffix] = [hashlib.sha256(handle.read()).hexdigest(), os.stat(path).st_mtime_ns]
print(json.dumps(files))
"""


def _snapshot(store: Path) -> dict:
    """The digest and modification time of the store file and of the journal, log and index beside
    it, read by another process: closing a descriptor this process holds on the file drops every
    POSIX lock this process holds on it, so a connection elsewhere could take itself for the store's
    last one and checkpoint the log under this process's connections."""
    result = subprocess.run(
        [sys.executable, "-c", _SNAPSHOT, str(store)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(result.stdout)


_SECOND_PROCESS = """
import asyncio
import sys

from lionagi.state import db as db_mod


async def main(path):
    try:
        await db_mod.StateDB(path).open()
    except db_mod.ForeignLineageError as exc:
        print("refused", str(exc))
    else:
        print("opened")


asyncio.run(main(sys.argv[1]))
"""


async def test_a_native_open_in_another_process_is_refused_before_its_connection_settings(
    db, store
):
    """This process holds the store in compatibility mode; a second process opening the same file
    natively is refused on the lineage row it reads before opening any writable connection, so the
    store, its log and its index keep their bytes and times, and this process goes on writing."""
    before = _snapshot(store)
    assert {"", "-wal", "-shm"} <= set(before)
    result = subprocess.run(
        [sys.executable, "-c", _SECOND_PROCESS, str(store)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=store.parent,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("refused "), result.stdout
    assert "'manager' as the application that manages its schema" in result.stdout
    assert _snapshot(store) == before
    meta = "SELECT key, value FROM schema_meta WHERE key IN ('lineage', 'version') ORDER BY key"
    assert _rows(store, meta) == [("lineage", "manager"), ("version", "900")]
    await _setup_run(db)
    assert _rows(store, f"SELECT {OWNER} FROM sessions WHERE id = 's1'") == [("principal:a",)]


_HOLDER = """
import sqlite3, sys

conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA wal_autocheckpoint = 0")
if sys.argv[2] == "only-in-the-log":
    # Not read again after the write, so the log's index records no reader at its last frame and
    # a read-only reader that records its own would write the index.
    conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('lineage', 'manager')")
else:
    conn.execute("SELECT count(*) FROM schema_meta").fetchone()
print("holding", flush=True)
sys.stdin.read()
conn.close()
"""


@pytest.mark.parametrize("lineage", ["in-the-file", "only-in-the-log"])
async def test_a_refused_native_open_leaves_a_store_another_process_holds_as_it_was(
    tmp_path, lineage
):
    """The manager has the store open in another process, its row in the file or written by that
    process and still only in the log, which that process has not read since; the refused open
    leaves the file, the log and the index with their bytes and times."""
    store = await _managed_store(tmp_path / "managed.db")
    if lineage == "only-in-the-log":
        _execute(store, "DELETE FROM schema_meta WHERE key = 'lineage'")
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(store), lineage],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=store.parent,
    )
    try:
        assert holder.stdout.readline().strip() == "holding"
        before = _snapshot(store)
        assert {"", "-wal", "-shm"} <= set(before)
        assert (lineage == "only-in-the-log") == (Path(f"{store}-wal").stat().st_size > 0)
        with pytest.raises(ForeignLineageError, match="'manager' as the application"):
            await StateDB(store).open()
        assert _snapshot(store) == before
    finally:
        holder.stdin.close()
        try:
            holder.wait(timeout=60)
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait()
            holder.stdout.close()


@pytest.mark.parametrize("name", ["managed.db", "managed #1.db"], ids=["plain", "uri-characters"])
async def test_a_refused_native_open_leaves_the_store_and_its_journal_mode_as_they_were(
    tmp_path, name
):
    """The writable connection's settings would switch a rollback-journal store to WAL, so the
    connection reads the lineage before them, from the file it opened, whatever characters its path
    holds."""
    store = await _managed_store(tmp_path / name)
    _execute(store, "PRAGMA journal_mode = delete")
    before = store.read_bytes()
    native = StateDB(store)
    with pytest.raises(ForeignLineageError, match="'manager' as the application"):
        await native.open()
    assert native._engine is None
    assert _rows(store, "PRAGMA journal_mode") == [("delete",)]
    assert store.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == [name]


async def test_the_lineage_refuses_before_the_version_on_either_guard(store):
    """The managed store records version 900, above this package's. The lineage decides the
    refusal before any writable connection, when the writable connection opens, and again inside
    the migration's version check under the write lock, which reads the lineage first, so the
    guards never disagree."""
    with pytest.raises(ForeignLineageError):
        await StateDB(store).open()

    schema = _rows(store, "SELECT type, name, sql FROM sqlite_master ORDER BY type, name")
    late = StateDB(store)
    # As if the lineage row landed after the connection's own read: its manager still creating
    # the store.
    with (
        patch.object(StateDB, "_read_lineage_without_writing", lambda self, held: None),
        patch.object(StateDB, "_refuse_a_managed_store", lambda self, dbapi_conn: None),
    ):
        try:
            with pytest.raises(ForeignLineageError):
                await late.open()
        finally:
            await late.close()
    assert _rows(store, "SELECT type, name, sql FROM sqlite_master ORDER BY type, name") == schema


async def test_a_store_whose_lineage_names_this_package_opens_natively(tmp_path):
    path = tmp_path / "native.db"
    first = StateDB(path)
    await first.open()
    await first.close()
    # Native initialization writes the row itself (ADR-0128 D7).
    assert _rows(path, "SELECT value FROM schema_meta WHERE key = 'lineage'") == [("lionagi",)]
    again = StateDB(path)
    await again.open()
    await again.close()


def _journal_mode(store: Path) -> str:
    return _rows(store, "PRAGMA journal_mode")[0][0]


def _spelled(tmp_path: Path, store: Path, spelling: str) -> str:
    if spelling == "dot-dot-through-a-symlink":
        # The file system resolves link/.. to a/; SQLAlchemy resolves it lexically to the store.
        (tmp_path / "a" / "b").mkdir(parents=True)
        (tmp_path / "link").symlink_to(tmp_path / "a" / "b", target_is_directory=True)
        return f"sqlite+aiosqlite:///{tmp_path}/link/../{store.name}"
    return f"sqlite+aiosqlite:///file:{store}?uri=true"


@pytest.mark.parametrize("spelling", ["dot-dot-through-a-symlink", "file-uri"])
async def test_a_native_open_is_refused_on_the_file_it_opens_however_the_url_spells_it(
    tmp_path, monkeypatch, spelling
):
    """The lineage is read on the connection that opened the file, so a path the file system
    resolves differently from SQLite, or SQLite's own URI form, reads the managed store's row."""
    monkeypatch.chdir(tmp_path)
    store = await _managed_store(tmp_path / "managed.db")
    _execute(store, "PRAGMA journal_mode = delete")
    before = store.read_bytes()
    native = StateDB(url=_spelled(tmp_path, store, spelling))
    with pytest.raises(ForeignLineageError, match="'manager' as the application"):
        await native.open()
    assert native._engine is None
    assert _journal_mode(store) == "delete"
    assert store.read_bytes() == before
    assert not Path(f"{store}-wal").exists()


async def test_a_lineage_row_written_just_before_the_writable_open_is_refused(
    tmp_path, monkeypatch
):
    """A manager that writes its row while a native open is starting is read by the writable
    connection before its settings, and the refused open leaves no engine to write through."""
    from lionagi.state import db as db_mod

    store = tmp_path / "managed.db"
    native = StateDB(store)
    await native.open()
    await native.close()
    _execute(store, "PRAGMA journal_mode = delete")
    make_engine = db_mod.make_engine

    def landing(url, **kwargs):
        _execute(
            store, "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('lineage', 'manager')"
        )
        return make_engine(url, **kwargs)

    monkeypatch.setattr(db_mod, "make_engine", landing)
    late = StateDB(store)
    with pytest.raises(ForeignLineageError, match="'manager' as the application"):
        await late.open()
    assert late._engine is None
    assert _journal_mode(store) == "delete"
    assert not Path(f"{store}-wal").exists()


def _hot_copy(store: Path, copy: Path, first: str | None = None) -> Path:
    """A copy of *store* taken mid-transaction, with its rollback journal: a hot journal. The
    transaction runs *first* before enough inserts that the page it changed is written to the
    file."""
    with closing(sqlite3.connect(store, isolation_level=None)) as conn:
        conn.execute("PRAGMA cache_size = -64")
        conn.execute("BEGIN IMMEDIATE")
        if first is not None:
            conn.execute(first)
        conn.execute("CREATE TABLE unfinished (v TEXT)")
        for _ in range(2000):
            conn.execute("INSERT INTO unfinished (v) VALUES (?)", ("x" * 4000,))
        shutil.copyfile(store, copy)
        shutil.copyfile(Path(f"{store}-journal"), Path(f"{copy}-journal"))
        conn.execute("ROLLBACK")
    return copy


async def test_a_store_with_a_hot_journal_and_no_lineage_row_opens_natively(tmp_path):
    """The connection's first read rolls the interrupted transaction back, as SQLite does on any
    writable open, and the store then opens natively."""
    store = tmp_path / "native.db"
    first = StateDB(store)
    await first.open()
    await first.close()
    _execute(store, "PRAGMA journal_mode = delete")
    copy = _hot_copy(store, tmp_path / "copy.db")
    assert Path(f"{copy}-journal").stat().st_size > 0
    again = StateDB(copy)
    await again.open()
    await again.close()
    assert _rows(copy, "SELECT name FROM sqlite_master WHERE name = 'unfinished'") == []
    assert _journal_mode(copy) == "wal"


async def test_a_refused_native_open_leaves_a_hot_journal_that_took_the_row_out_of_the_file(
    tmp_path,
):
    """The interrupted transaction deleted the row and its page reached the file, so the file alone
    shows no row; a private copy of the file and the journal is rolled back and shows it, and the
    store, its journal and the absence of a log are left as they were."""
    from lionagi.state.db import _read_lineage

    store = await _managed_store(tmp_path / "managed.db")
    _execute(store, "PRAGMA journal_mode = delete")
    copy = _hot_copy(store, tmp_path / "copy.db", "DELETE FROM schema_meta WHERE key = 'lineage'")
    assert _read_lineage(f"{copy.as_uri()}?immutable=1")[1].lineage is None
    before = _snapshot(copy)
    assert set(before) == {"", "-journal"}
    with pytest.raises(ForeignLineageError, match="'manager' as the application"):
        await StateDB(copy).open()
    assert _snapshot(copy) == before


async def test_a_lineage_row_an_interrupted_transaction_wrote_does_not_refuse_the_open(tmp_path):
    """The file shows a row the interrupted transaction wrote over lionagi's own, which rolling the
    journal back undoes; the store opens natively, and the writable connection rolls the journal
    back."""
    from lionagi.state.db import _read_lineage

    store = tmp_path / "native.db"
    first = StateDB(store)
    await first.open()
    await first.close()
    _execute(store, "PRAGMA journal_mode = delete")
    copy = _hot_copy(
        store,
        tmp_path / "copy.db",
        "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('lineage', 'manager')",
    )
    assert _read_lineage(f"{copy.as_uri()}?immutable=1")[1].lineage == "manager"
    again = StateDB(copy)
    await again.open()
    await again.close()
    assert _rows(copy, "SELECT value FROM schema_meta WHERE key = 'lineage'") == [("lionagi",)]


@pytest.mark.parametrize("spelling", ["dot", "symlink"])
async def test_a_native_open_of_the_held_file_by_another_path_is_refused_whatever_its_row_says(
    db, store, tmp_path, monkeypatch, spelling
):
    """The file a compatibility handle holds is known by its device and inode, so a native open in
    this process is refused for it with its lineage row gone and its path spelled another way,
    before any writable connection is made."""
    from lionagi.state import db as db_mod

    def no_writable_engine(url, **kwargs):
        raise AssertionError("a writable engine was made for the held file")

    monkeypatch.setattr(db_mod, "make_engine", no_writable_engine)
    _execute(store, "DELETE FROM schema_meta WHERE key = 'lineage'")
    if spelling == "dot":
        url = f"sqlite+aiosqlite:///{store.parent}/./{store.name}"
    else:
        alias = tmp_path / "alias.db"
        alias.symlink_to(store)
        url = f"sqlite+aiosqlite:///{alias}"
    native = StateDB(url=url)
    with pytest.raises(StateCompatibilityRefused, match="a native open would apply"):
        await native.open()
    assert native._engine is None
    assert _rows(store, "SELECT value FROM schema_meta WHERE key = 'version'") == [("900",)]


@pytest.mark.parametrize("spelling", ["same-path", "symlink", "query", "file-uri"])
async def test_compatibility_admission_is_refused_while_a_native_handle_has_the_store_open(
    tmp_path, spelling
):
    """A writable native handle opened before the managing application wrote its lineage row keeps
    connections that never read it, so admission in this process is refused for that file, however
    its path is spelled, until the handle closes. A read-only handle writes nothing and is left
    alone."""
    store = tmp_path / "managed.db"
    urls = {
        "query": f"sqlite+aiosqlite:///{store}?timeout=5",
        "file-uri": f"sqlite+aiosqlite:///file:{store}?uri=true",
    }
    native = StateDB(urls.get(spelling, store))
    await native.open()
    _manage(store)
    path = store
    if spelling == "symlink":
        path = tmp_path / "alias.db"
        path.symlink_to(store)
    compat = StateDB(path, compatibility=_profile(store), owner="principal:a")
    try:
        with pytest.raises(StateCompatibilityRefused, match="holds the store open natively"):
            await compat.open()
        assert compat._engine is None
    finally:
        await native.close()

    reader = StateDB(store, readonly=True)
    await reader.open()
    try:
        await compat.open()
        await compat.close()
    finally:
        await reader.close()


async def test_a_native_handles_later_connections_read_a_lineage_row_written_after_it_opened(
    tmp_path,
):
    """The row is read by each new connection, before its settings; a connection the handle
    already has open is not asked again."""
    store = tmp_path / "state.db"
    native = StateDB(store)
    await native.open()
    try:
        _execute(
            store, "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('lineage', 'manager')"
        )
        await native._engine.dispose()
        with pytest.raises(ForeignLineageError, match="'manager' as the application"):
            async with native._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    finally:
        await native.close()


async def test_an_open_whose_connection_never_read_the_store_leaves_no_engine(tmp_path):
    """Opened again once the file is a database, the handle applies its schema rather than
    returning early on the engine the failed open made."""
    from sqlalchemy.exc import DatabaseError

    path = tmp_path / "state.db"
    path.write_bytes(b"not a database file " * 256)
    handle = StateDB(path)
    with pytest.raises(DatabaseError, match="file is not a database"):
        await handle.open()
    assert handle._engine is None
    path.unlink()
    await handle.open()
    try:
        assert _rows(path, "SELECT 1 FROM schema_meta WHERE key = 'version'") == [(1,)]
    finally:
        await handle.close()


async def test_an_open_that_fails_after_its_connection_read_the_store_keeps_its_engine(
    tmp_path, monkeypatch
):
    """Only an open whose connection never read the store, or one that refuses the store, is
    undone; after any other later failure the engine stays, as it always has, and the next open
    completes on it."""
    path = tmp_path / "state.db"
    handle = StateDB(path)

    async def fail(self):
        raise RuntimeError("after the connection read the store")

    monkeypatch.setattr(StateDB, "_backfill_session_ended_at_once", fail)
    try:
        with pytest.raises(RuntimeError, match="after the connection read the store"):
            await handle.open()
        assert handle._engine is not None
        monkeypatch.undo()
        await handle.open()
        assert handle._schema_applied
    finally:
        await handle.close()


@pytest.mark.parametrize("refusal", ["too-new", "unrecognized"])
async def test_a_kept_engine_whose_store_is_refused_on_the_next_open_is_disposed(
    tmp_path, monkeypatch, refusal
):
    """The open that completes a kept engine refuses the store as a fresh open does, and the
    refusal leaves no engine and no registration."""
    from lionagi.state.db import (
        _OPEN_NATIVE,
        SCHEMA_VERSION,
        SchemaTooNewError,
        UnrecognizedStoreError,
    )

    path = tmp_path / "state.db"
    handle = StateDB(path)

    async def fail(self):
        raise RuntimeError("after the connection read the store")

    monkeypatch.setattr(StateDB, "_backfill_session_ended_at_once", fail)
    with pytest.raises(RuntimeError):
        await handle.open()
    monkeypatch.undo()
    assert handle._engine is not None
    if refusal == "too-new":
        stamp, expected = str(int(SCHEMA_VERSION) + 1), SchemaTooNewError
    else:
        stamp, expected = "not-a-version", UnrecognizedStoreError
    _execute(path, "DELETE FROM schema_meta WHERE key = 'lineage'")
    _execute(path, "UPDATE schema_meta SET value = ? WHERE key = 'version'", (stamp,))
    try:
        with pytest.raises(expected):
            await handle.open()
        assert handle._engine is None
        assert handle not in _OPEN_NATIVE
    finally:
        await handle.close()


def _wal_copy(store: Path, copy: Path, *, lineage: bool = False, index: bool = False) -> Path:
    """A copy of *store* whose last committed rows, the lineage row when *lineage*, are still only
    in its write-ahead log, with the log's index beside it when *index*: a manager that stopped
    without a checkpoint."""
    with closing(sqlite3.connect(store, isolation_level=None)) as conn:
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute("CREATE TABLE unfolded (v TEXT)")
        conn.execute("INSERT INTO unfolded (v) VALUES ('kept')")
        if lineage:
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('lineage', 'manager')"
            )
        shutil.copyfile(store, copy)
        shutil.copyfile(Path(f"{store}-wal"), Path(f"{copy}-wal"))
        if index:
            shutil.copyfile(Path(f"{store}-shm"), Path(f"{copy}-shm"))
    return copy


async def _unattended(tmp_path: Path, shape: str) -> Path:
    store = await _managed_store(tmp_path / "managed.db")
    if shape == "log-left-behind":
        return _wal_copy(store, tmp_path / "copy.db")
    if shape == "hot-journal":
        _execute(store, "PRAGMA journal_mode = delete")
        return _hot_copy(store, tmp_path / "copy.db")
    return store


@pytest.mark.parametrize("shape", ["checkpointed", "log-left-behind", "hot-journal"])
async def test_a_refused_native_open_leaves_a_store_nobody_has_open_as_it_was(tmp_path, shape):
    """With the row in the file, nothing is opened for writing: a log is not checkpointed, a hot
    journal is not rolled back, and no log or index is created."""
    store = await _unattended(tmp_path, shape)
    before = _snapshot(store)
    expected = {
        "checkpointed": {""},
        "log-left-behind": {"", "-wal"},
        "hot-journal": {"", "-journal"},
    }
    assert set(before) == expected[shape]
    with pytest.raises(ForeignLineageError, match="'manager' as the application"):
        await StateDB(store).open()
    assert _snapshot(store) == before


@pytest.mark.parametrize("index", [True, False], ids=["with-its-index", "without-an-index"])
async def test_a_refused_native_open_of_a_store_nobody_has_open_whose_row_is_in_its_log(
    tmp_path, index
):
    """The row is only in the log. With the log's index beside it, the row is read through the
    index without writing to it; without one, from a private copy of the file and the log, so no
    index is built beside the store."""
    store = await _managed_store(tmp_path / "managed.db")
    _execute(store, "DELETE FROM schema_meta WHERE key = 'lineage'")
    copy = _wal_copy(store, tmp_path / "copy.db", lineage=True, index=index)
    before = _snapshot(copy)
    assert set(before) == ({"", "-wal", "-shm"} if index else {"", "-wal"})
    with pytest.raises(ForeignLineageError, match="'manager' as the application"):
        await StateDB(copy).open()
    assert _snapshot(copy) == before


async def test_a_refused_native_open_of_a_store_whose_file_was_cut_short_beside_its_log(tmp_path):
    """Every page is in the log and the file holds only its first, as a crash during a checkpoint
    can leave it: the file cannot be read alone, so the row is read from a private copy of the
    file and the log."""
    from lionagi.state.db import _UNREAD, _read_lineage

    store = await _managed_store(tmp_path / "managed.db")
    _execute(store, "DELETE FROM schema_meta WHERE key = 'lineage'")
    copy = tmp_path / "copy.db"
    with closing(sqlite3.connect(store, isolation_level=None)) as conn:
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('lineage', 'manager')"
        )
        conn.execute("VACUUM")
        shutil.copyfile(store, copy)
        shutil.copyfile(Path(f"{store}-wal"), Path(f"{copy}-wal"))
    with open(copy, "r+b") as handle:
        handle.truncate(4096)
    assert _read_lineage(f"{copy.as_uri()}?immutable=1")[1] is _UNREAD
    before = _snapshot(copy)
    with pytest.raises(ForeignLineageError, match="'manager' as the application"):
        await StateDB(copy).open()
    assert _snapshot(copy) == before


def _first_page_beside_its_log(directory: Path, *, whole: bool = False) -> Path:
    """A store whose file stops after its first page, beside a log that holds the pages written
    since the last checkpoint, the managing row among them, and no index; or, with *whole*, the
    whole file beside the same log."""
    store = directory / "source.db"
    with closing(sqlite3.connect(store, isolation_level=None)) as conn:
        conn.execute("PRAGMA journal_mode = wal")
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO schema_meta (key, value) VALUES ('version', '900')")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('lineage', 'manager')"
        )
        copy = directory / "copy.db"
        data = store.read_bytes()
        copy.write_bytes(data if whole else data[:4096])
        Path(f"{copy}-wal").write_bytes(Path(f"{store}-wal").read_bytes())
    return copy


async def test_a_refused_native_open_of_a_store_whose_file_stops_at_its_first_page(tmp_path):
    """Only the pages written since the last checkpoint are in the log, so neither the file alone
    nor a copy that stops where its pages stop holds the row; a byte copy of the file and the log
    does, and the open is refused with no file of the store changed."""
    from lionagi.state.db import _UNREAD, _read_lineage

    copy = _first_page_beside_its_log(tmp_path)
    assert _read_lineage(f"{copy.as_uri()}?immutable=1")[1] is _UNREAD
    before = _snapshot(copy)
    assert set(before) == {"", "-wal"}
    with pytest.raises(ForeignLineageError, match="'manager' as the application"):
        await StateDB(copy).open()
    assert _snapshot(copy) == before


_TEAR_THE_FIRST_PAGE = """
import os, sqlite3, sys

conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA journal_mode = delete")
conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
conn.execute("CREATE TABLE filler (id INTEGER PRIMARY KEY, x TEXT)")
conn.execute("INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('lineage', 'manager')")
conn.execute("BEGIN IMMEDIATE")
conn.executemany("INSERT INTO filler (x) VALUES (?)", [("y" * 200,)] * 3000)
conn.execute("COMMIT")
conn.execute("PRAGMA cache_size = -16")
conn.execute("BEGIN IMMEDIATE")
conn.execute("UPDATE schema_meta SET value = 'lionagi' WHERE key = 'lineage'")
conn.executemany("INSERT INTO filler (x) VALUES (?)", [("z" * 200,)] * 3000)
with open(sys.argv[1], "r+b") as handle:
    handle.write(bytes(100))
os._exit(0)
"""


async def test_a_refused_native_open_of_a_store_whose_first_page_a_crash_tore(tmp_path):
    """A process stopped inside a transaction that had written into the file, its first page torn,
    with the journal holding the pages as they were. SQLite cannot read the file alone, so the row
    is read from a byte copy of the file and the journal, rolled back there, and the open is refused
    before any writable connection could roll the store's own journal back."""
    from lionagi.state.db import _UNREAD, _journal_has_a_header, _read_lineage

    store = tmp_path / "torn.db"
    subprocess.run(
        [sys.executable, "-c", _TEAR_THE_FIRST_PAGE, str(store)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert _journal_has_a_header(str(store))
    assert _read_lineage(f"{store.as_uri()}?immutable=1")[1] is _UNREAD
    before = _snapshot(store)
    with pytest.raises(ForeignLineageError, match="'manager' as the application"):
        await StateDB(store).open()
    assert _snapshot(store) == before


async def test_a_store_that_moves_under_every_copy_is_refused_as_unreadable(tmp_path, monkeypatch):
    """Every copy sees the store commit or checkpoint under it, so no read of the file and its
    log is of one state. After the bounded number of copies the store is refused before any
    writable connection exists, leaving the log in place, rather than by a connection that folds
    the log into the file on closing (ADR-0128 D7)."""
    from lionagi.state import db as db_mod

    copy = _first_page_beside_its_log(tmp_path, whole=True)
    copies = []
    real = db_mod._copy_store_file
    moves = iter(range(1_000))

    def counting(path, target):
        copies.append(path)
        real(path, target)

    monkeypatch.setattr(db_mod, "_copy_store_file", counting)
    monkeypatch.setattr(db_mod, "_copy_state", lambda path, suffix: next(moves))
    with pytest.raises(UnreadableStoreError, match="could not be read"):
        StateDB(copy)._read_lineage_without_writing(())
    assert len(copies) == db_mod._COPY_ATTEMPTS
    assert Path(f"{copy}-wal").exists()
    before = _snapshot(copy)
    with pytest.raises(UnreadableStoreError, match="could not be read"):
        await StateDB(copy).open()
    assert len(copies) == 2 * db_mod._COPY_ATTEMPTS
    assert _snapshot(copy) == before


async def test_close_from_another_event_loop_during_an_open_is_refused(tmp_path, monkeypatch):
    """The open lock wakes a waiter only on the loop that holds it, so a close from another loop
    while an open is in progress is refused at once, rather than waiting on a wake that never comes
    while the open goes on to keep its engine; the open finishes, and closes on its own loop."""
    store = tmp_path / "plain.db"
    first = StateDB(store)
    await first.open()
    await first.close()
    original = StateDB._apply_schema
    started = asyncio.Event()

    async def slow(self):
        started.set()
        await asyncio.sleep(0.5)
        await original(self)

    monkeypatch.setattr(StateDB, "_apply_schema", slow)
    handle = StateDB(store)
    opening = asyncio.ensure_future(handle.open())
    await started.wait()
    outcome = {}

    def close_on_another_loop():
        try:
            asyncio.run(asyncio.wait_for(handle.close(), timeout=5))
            outcome["result"] = "returned"
        except BaseException as exc:  # noqa: BLE001 -- the test asserts what close raised
            outcome["result"] = exc

    worker = threading.Thread(target=close_on_another_loop)
    worker.start()
    await asyncio.get_running_loop().run_in_executor(None, worker.join, 30)
    assert not worker.is_alive()
    assert isinstance(outcome["result"], RuntimeError), outcome
    assert "another event loop" in str(outcome["result"])
    await opening
    assert handle._engine is not None
    await handle.close()
    assert handle._engine is None


async def test_a_store_with_rows_only_in_its_log_and_no_lineage_row_opens_natively(tmp_path):
    store = tmp_path / "native.db"
    first = StateDB(store)
    await first.open()
    await first.close()
    copy = _wal_copy(store, tmp_path / "copy.db")
    again = StateDB(copy)
    await again.open()
    await again.close()
    assert _rows(copy, "SELECT v FROM unfolded") == [("kept",)]


@pytest.mark.parametrize("index", [True, False], ids=["with-its-index", "without-an-index"])
async def test_a_row_in_the_log_is_read_whatever_the_files_row_says(tmp_path, index):
    """The file's row names this package and the log, not yet folded into it, names the manager:
    the log's row is the store's, so the open is refused before any writable connection exists
    and the file, the log and the index keep their bytes and times."""
    from lionagi.state.db import _read_lineage

    store = tmp_path / "native.db"
    first = StateDB(store)
    await first.open()
    await first.close()
    assert _rows(store, "SELECT value FROM schema_meta WHERE key = 'lineage'") == [("lionagi",)]
    copy = tmp_path / "copy.db"
    with closing(sqlite3.connect(store, isolation_level=None)) as conn:
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute("UPDATE schema_meta SET value = 'manager' WHERE key = 'lineage'")
        shutil.copyfile(store, copy)
        shutil.copyfile(Path(f"{store}-wal"), Path(f"{copy}-wal"))
        if index:
            shutil.copyfile(Path(f"{store}-shm"), Path(f"{copy}-shm"))
    assert _read_lineage(f"{copy.as_uri()}?immutable=1")[1].lineage == "lionagi"
    before = _snapshot(copy)
    assert set(before) == ({"", "-wal", "-shm"} if index else {"", "-wal"})
    with pytest.raises(ForeignLineageError, match="'manager' as the application"):
        await StateDB(copy).open()
    assert _snapshot(copy) == before


def test_a_checkpoint_between_the_two_copies_takes_the_read_again(tmp_path, monkeypatch):
    """The row is only in the log and there is no index, so it is read from a private copy. Another
    connection truncates the log into the file after the file is copied and before the log is:
    the copy would show no row, so the read is taken again, from the file that now holds it."""
    from lionagi.state import db as db_mod

    store = tmp_path / "managed.db"
    with closing(sqlite3.connect(store)) as conn:
        conn.execute("PRAGMA journal_mode = wal")
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.commit()
    copy = tmp_path / "copy.db"
    with closing(sqlite3.connect(store, isolation_level=None)) as conn:
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('lineage', 'manager')"
        )
        shutil.copyfile(store, copy)
        shutil.copyfile(Path(f"{store}-wal"), Path(f"{copy}-wal"))
    holders: list[sqlite3.Connection] = []
    real = db_mod._copy_store_file
    copies = []

    def checkpoint_after_the_first_copy(path, target):
        real(path, target)
        copies.append(path)
        if len(copies) == 1:
            # Kept open, so the truncated log stays beside the file, as under a live manager.
            holder = sqlite3.connect(copy, isolation_level=None)
            holder.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            holders.append(holder)

    monkeypatch.setattr(db_mod, "_copy_store_file", checkpoint_after_the_first_copy)
    try:
        with pytest.raises(ForeignLineageError, match="'manager' as the application"):
            StateDB(copy)._read_lineage_without_writing(())
        assert len(copies) == 1
        assert Path(f"{copy}-wal").stat().st_size == 0
    finally:
        for holder in holders:
            holder.close()


def test_without_the_check_a_checkpoint_between_the_copies_hides_the_row(tmp_path, monkeypatch):
    """The control for the test above: with the state of the store not compared across the two
    copies, the same race leaves a copy that shows no row, and nothing is refused."""
    from lionagi.state import db as db_mod

    store = tmp_path / "managed.db"
    with closing(sqlite3.connect(store)) as conn:
        conn.execute("PRAGMA journal_mode = wal")
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.commit()
    with closing(sqlite3.connect(store, isolation_level=None)) as conn:
        conn.execute("PRAGMA wal_autocheckpoint = 0")
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('lineage', 'manager')"
        )
        copy = tmp_path / "copy.db"
        shutil.copyfile(store, copy)
        shutil.copyfile(Path(f"{store}-wal"), Path(f"{copy}-wal"))
    holders: list[sqlite3.Connection] = []
    real = db_mod._copy_store_file

    def checkpoint_after_the_copy(path, target):
        real(path, target)
        if not holders:
            holder = sqlite3.connect(copy, isolation_level=None)
            holder.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            holders.append(holder)

    monkeypatch.setattr(db_mod, "_copy_store_file", checkpoint_after_the_copy)
    monkeypatch.setattr(db_mod, "_copy_state", lambda path, suffix: ())
    try:
        assert db_mod._read_lineage_from_a_copy(str(copy), "-wal").lineage is None
    finally:
        for holder in holders:
            holder.close()


_RESERVE = """
import sqlite3, sys

conn = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None)
try:
    conn.execute("BEGIN IMMEDIATE")
    print("acquired")
    conn.execute("ROLLBACK")
except sqlite3.OperationalError as exc:
    print("locked" if "locked" in str(exc) else f"error: {exc}")
finally:
    conn.close()
"""


def _another_process_reserves(store: Path) -> str:
    result = subprocess.run(
        [sys.executable, "-c", _RESERVE, str(store)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return result.stdout.strip()


def test_the_private_copy_keeps_the_locks_this_process_holds_on_the_store(tmp_path, monkeypatch):
    """A connection here is inside a write transaction beside a hot-looking journal when the
    lineage is read from a copy. Closing any descriptor this process opened on the file would drop
    that connection's lock and let another process write into its transaction; the copy reads the
    file through a descriptor this process keeps open, so the lock stays. Copied with a plain file
    copy instead, the lock is lost."""
    from lionagi.state import db as db_mod

    store = tmp_path / "rollback.db"
    with closing(sqlite3.connect(store)) as conn:
        conn.execute("PRAGMA journal_mode = delete")
        conn.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE TABLE filler (v TEXT)")
        conn.commit()
    writer = sqlite3.connect(store, isolation_level=None)
    try:
        # A transaction larger than the page cache spills into the file, so SQLite syncs the
        # journal and writes its header first: the journal looks hot.
        writer.execute("PRAGMA cache_size = -64")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('lineage', 'manager')"
        )
        for _ in range(2000):
            writer.execute("INSERT INTO filler (v) VALUES (?)", ("x" * 4000,))
        assert db_mod._journal_has_a_header(str(store))
        assert _another_process_reserves(store) == "locked"
        assert db_mod._read_lineage_from_a_copy(str(store), "-journal").lineage is None
        assert _another_process_reserves(store) == "locked"
        monkeypatch.setattr(db_mod, "_copy_store_file", shutil.copyfile)
        db_mod._read_lineage_from_a_copy(str(store), "-journal")
        assert _another_process_reserves(store) == "acquired"
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_a_copy_of_a_store_larger_than_one_chunk_is_byte_identical(tmp_path):
    """The copy loop reads a fixed-size chunk at a time, so a store past that size takes more than
    one read: the whole file is copied, byte for byte, as shutil.copyfile copies it."""
    from lionagi.state import db as db_mod

    store = tmp_path / "store.db"
    store.write_bytes(bytes(range(256)) * (((1 << 20) + 4096) // 256) + b"tail")
    copy = tmp_path / "copy.db"
    db_mod._copy_store_file(str(store), str(copy))
    assert copy.stat().st_size > 1 << 20, "the fixture store must exceed one read"
    assert copy.read_bytes() == store.read_bytes()

    reference = tmp_path / "reference.db"
    shutil.copyfile(store, reference)
    assert copy.read_bytes() == reference.read_bytes()


async def test_a_handle_whose_open_failed_after_its_connection_read_the_store_is_registered(
    tmp_path, monkeypatch
):
    """The engine stays after a failure past the first read, and its connections write without a
    profile, so compatibility admission in this process refuses the file while it is open."""
    from lionagi.state import db as db_mod

    store = tmp_path / "state.db"

    async def read_then_fail(self):
        async with self._engine.connect():
            pass
        raise RuntimeError("the schema could not be applied")

    monkeypatch.setattr(StateDB, "_apply_schema", read_then_fail)
    handle = StateDB(store)
    try:
        with pytest.raises(RuntimeError, match="could not be applied"):
            await handle.open()
        assert handle._connection_read
        assert handle._engine is not None
        assert handle in db_mod._OPEN_NATIVE
    finally:
        await handle.close()
    assert handle not in db_mod._OPEN_NATIVE


async def test_close_during_an_open_waits_for_it_and_leaves_no_engine(tmp_path):
    import asyncio

    handle = StateDB(tmp_path / "state.db")
    opening = asyncio.ensure_future(handle.open())
    await asyncio.sleep(0)
    await handle.close()
    await opening
    assert handle._engine is None


def _row_lands_before_the_writable_engine(monkeypatch, store: Path) -> None:
    from lionagi.state import db as db_mod

    make_engine = db_mod.make_engine

    def landing(url, **kwargs):
        _execute(
            store, "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('lineage', 'manager')"
        )
        return make_engine(url, **kwargs)

    monkeypatch.setattr(db_mod, "make_engine", landing)


async def test_a_writable_connection_that_refuses_is_closed(tmp_path, monkeypatch):
    """A row written after the read-only reads is read by the writable connection, which is closed
    when it refuses: no connection of this process is left on the store."""
    store = tmp_path / "managed.db"
    native = StateDB(store)
    await native.open()
    await native.close()
    _row_lands_before_the_writable_engine(monkeypatch, store)
    with pytest.raises(ForeignLineageError, match="'manager' as the application"):
        await StateDB(store).open()
    with closing(sqlite3.connect(store, timeout=0)) as conn:
        assert conn.execute("PRAGMA journal_mode = delete").fetchone() == ("delete",)


async def test_a_refusal_on_the_writable_connection_is_not_logged_as_a_failed_inspection(
    tmp_path, monkeypatch, caplog
):
    store = tmp_path / "managed.db"
    native = StateDB(store)
    await native.open()
    await native.close()
    _row_lands_before_the_writable_engine(monkeypatch, store)
    with pytest.raises(ForeignLineageError):
        await StateDB(store).open()
    assert "failed to inspect migration columns" not in caplog.text


async def test_compatibility_mode_checks_the_path_sqlite_opens_and_creates_nothing(tmp_path):
    """SQLAlchemy opens a URL's path resolved lexically, so `link/..` names the file beside the
    link, not the one the file system resolves it to."""
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "a" / "b", target_is_directory=True)
    store = await _managed_store(tmp_path / "a" / "managed.db")
    url = f"sqlite+aiosqlite:///{tmp_path}/link/../managed.db"
    compat = StateDB(url=url, compatibility=_profile(store), owner="principal:a")
    with pytest.raises(StateCompatibilityRefused, match="no store at"):
        await compat.open()
    assert compat._engine is None
    assert not (tmp_path / "managed.db").exists()


async def test_a_run_writes_its_owner_on_every_row_it_creates(db, store):
    await _setup_run(db)

    for table, row_id in (
        ("progressions", "sp"),
        ("progressions", "bp"),
        ("sessions", "s1"),
        ("messages", "m-sys"),
        ("branches", "b1"),
    ):
        assert _rows(store, f"SELECT {OWNER} FROM {table} WHERE id = ?", (row_id,)) == [
            ("principal:a",)
        ], table
    transitions = _rows(
        store,
        f"SELECT previous_status, status, {OWNER} FROM status_transitions WHERE entity_id = 's1'",
    )
    assert transitions == [(None, "running", "principal:a")]


async def test_project_registration_is_left_to_the_managing_application(db, store):
    await db.create_progression("sp")
    await db.create_session(_session(project="acme/widgets", project_source="git_remote"))

    assert _rows(store, "SELECT project FROM sessions WHERE id = 's1'") == [("acme/widgets",)]
    assert _rows(store, "SELECT COUNT(*) FROM projects") == [(0,)]


async def test_an_owner_that_is_not_a_principal_is_refused_at_open(store):
    handle = StateDB(store, compatibility=_profile(store), owner="principal:nobody")

    with pytest.raises(StateCompatibilityRefused, match="not in principals") as refused:
        await handle.open()

    assert refused.value.operation == "open"
    assert handle._engine is None


def test_a_profile_that_records_owners_needs_an_owner_and_only_it_takes_one(tmp_path):
    reference = tmp_path / "reference.db"
    with closing(sqlite3.connect(reference)) as conn:
        conn.executescript(
            "CREATE TABLE principals (id TEXT PRIMARY KEY);"
            f"CREATE TABLE runs (id TEXT PRIMARY KEY, {OWNER} TEXT NOT NULL DEFAULT 'x');"
        )
        closure = read_closure(conn, ["principals", "runs"])
    owned = CompatibilityProfile(
        name="owned",
        version=1,
        closure=closure,
        writes={"runs": TableWrites(insert={"id", OWNER})},
        operations={"start"},
        owner=OwnerRule(column=OWNER, principals="principals"),
    )
    plain = CompatibilityProfile(
        name="plain",
        version=1,
        closure=closure,
        writes={"runs": TableWrites(insert={"id", OWNER})},
        operations={"start"},
    )

    with pytest.raises(ValueError, match="an owner is required"):
        StateDB(reference, compatibility=owned)
    with pytest.raises(ValueError, match="records no owner"):
        StateDB(reference, compatibility=plain, owner="principal:a")
    with pytest.raises(ValueError, match="compatibility mode only"):
        StateDB(reference, owner="principal:a")


@pytest.mark.parametrize(
    "writes, owner, message",
    [
        (
            {"runs": TableWrites(insert={"id"}, unwritten={OWNER: "the default names one"})},
            OwnerRule(column=OWNER, principals="principals"),
            "a default is never the owner",
        ),
        (
            {"runs": TableWrites(insert={"id", OWNER}, update={OWNER})},
            OwnerRule(column=OWNER, principals="principals"),
            "never updated",
        ),
        (
            {"runs": TableWrites(insert={"id", OWNER})},
            OwnerRule(column=OWNER, principals="people"),
            "not in the closure",
        ),
        (
            {"runs": TableWrites(insert={"id", OWNER})},
            OwnerRule(column=OWNER, principals="principals", key="handle"),
            "no column handle",
        ),
    ],
    ids=["default-owner", "updated-owner", "no-principals-table", "no-principals-key"],
)
def test_a_profile_that_could_leave_or_change_the_owner_is_refused(
    tmp_path, writes, owner, message
):
    reference = tmp_path / "reference.db"
    with closing(sqlite3.connect(reference)) as conn:
        conn.executescript(
            "CREATE TABLE principals (id TEXT PRIMARY KEY);"
            f"CREATE TABLE runs (id TEXT PRIMARY KEY, {OWNER} TEXT NOT NULL DEFAULT 'x');"
        )
        closure = read_closure(conn, ["principals", "runs"])

    with pytest.raises(ValueError, match=message):
        CompatibilityProfile(
            name="owned",
            version=1,
            closure=closure,
            writes=writes,
            operations={"start"},
            owner=owner,
        )


@pytest.mark.parametrize("table", ["progressions", "sessions", "branches"])
async def test_an_id_that_already_exists_is_refused_and_left_as_it_is(db, store, table):
    await db.create_progression("sp")
    await db.create_progression("bp")
    if table == "progressions":
        _execute(
            store, "INSERT INTO progressions (id, created_at, collection) VALUES ('p0', 0, '[]')"
        )
        call = db.create_progression("p0")
    elif table == "sessions":
        _execute(
            store,
            f"INSERT INTO sessions (id, created_at, updated_at, progression_id, {OWNER}) "
            "VALUES ('s0', 0, 0, 'sp', 'principal:b')",
        )
        call = db.create_session(_session("s0"))
    else:
        await db.create_session(_session())
        _execute(
            store,
            "INSERT INTO branches (id, created_at, session_id, progression_id) "
            "VALUES ('b0', 0, 's1', 'bp')",
        )
        call = db.create_branch({"id": "b0", "session_id": "s1", "progression_id": "bp"})
    before = _rows(store, f"SELECT * FROM {table} ORDER BY id")

    with pytest.raises(StateCompatibilityRefused, match="already exists"):
        await call

    assert _rows(store, f"SELECT * FROM {table} ORDER BY id") == before


@pytest.mark.parametrize(
    "link, value",
    [("session_id", "s-other"), ("progression_id", "p-other"), ("system_msg_id", "m-other")],
)
async def test_a_branch_links_only_rows_its_run_created(db, store, link, value):
    await db.create_progression("sp")
    await db.create_progression("bp")
    await db.create_session(_session())
    _execute(
        store, "INSERT INTO progressions (id, created_at, collection) VALUES ('p-other', 0, '[]')"
    )
    _execute(
        store,
        f"INSERT INTO sessions (id, created_at, updated_at, progression_id, {OWNER}) "
        "VALUES ('s-other', 0, 0, 'p-other', 'principal:a')",
    )
    _execute(
        store,
        "INSERT INTO messages (id, created_at, content, role, lion_class) "
        "VALUES ('m-other', 0, '{}', 'system', 1)",
    )
    branch = {"id": "b1", "session_id": "s1", "progression_id": "bp", link: value}

    with pytest.raises(StateCompatibilityRefused, match="was not created by this run"):
        await db.create_branch(branch)

    assert _rows(store, "SELECT COUNT(*) FROM branches") == [(0,)]


def _foreign_rows(store: Path) -> None:
    """A progression and a message another principal's run wrote."""
    _execute(
        store,
        f"INSERT INTO progressions (id, created_at, collection, {OWNER}) "
        "VALUES ('p-other', 0, '[]', 'principal:b')",
    )
    _execute(
        store,
        f"INSERT INTO messages (id, created_at, content, role, lion_class, {OWNER}) "
        "VALUES ('m-other', 0, '{}', 'system', 1, 'principal:b')",
    )


@pytest.mark.parametrize(
    "link, value",
    [("progression_id", "p-other"), ("first_msg_id", "m-other"), ("last_msg_id", "m-other")],
)
async def test_a_session_links_only_rows_its_run_created(db, store, link, value):
    await db.create_progression("sp")
    await db.insert_message(_system_message())
    _foreign_rows(store)

    with pytest.raises(StateCompatibilityRefused, match="was not created by this run"):
        await db.create_session(_session(**{link: value}))
    await db.create_session(_session("s2", first_msg_id="m-sys", last_msg_id="m-sys"))

    assert _rows(store, "SELECT id FROM sessions ORDER BY id") == [("s2",)]
    assert _rows(store, "SELECT entity_id FROM status_transitions") == [("s2",)]


async def test_a_progression_lists_only_messages_its_run_created(db, store):
    await db.insert_message(_system_message())
    _foreign_rows(store)

    with pytest.raises(StateCompatibilityRefused, match="'m-other' was not created by this run"):
        await db.create_progression("p1", ["m-sys", "m-other"])
    await db.create_progression("p2", ["m-sys"])

    assert _rows(store, "SELECT id, collection FROM progressions ORDER BY id") == [
        ("p-other", "[]"),
        ("p2", '["m-sys"]'),
    ]


@pytest.mark.parametrize(
    "taken",
    ["DELETE FROM messages WHERE id = 'm-sys'", f"UPDATE messages SET {OWNER} = 'principal:b'"],
    ids=["deleted", "given to another owner"],
)
async def test_a_message_the_store_took_back_is_never_rewritten(db, store, taken):
    await db.insert_message(_system_message(content="first"))
    _execute(store, taken)
    before = _rows(store, f"SELECT id, content, {OWNER} FROM messages")

    with pytest.raises(StateCompatibilityRefused, match="is gone or no longer belongs"):
        await db.insert_message(_system_message(content="second"))

    assert _rows(store, f"SELECT id, content, {OWNER} FROM messages") == before


async def test_a_message_this_run_wrote_is_rewritten_and_any_other_refused(db, store):
    await db.insert_message(_system_message(content="first"))
    await db.insert_message(_system_message(content="second"))
    _execute(
        store,
        f"INSERT INTO messages (id, created_at, content, role, lion_class, {OWNER}) "
        "VALUES ('m-other', 0, '{\"text\": \"theirs\"}', 'system', 1, 'principal:a')",
    )

    with pytest.raises(StateCompatibilityRefused, match="already exists"):
        await db.insert_message(_system_message("m-other", content="mine"))

    assert _rows(store, "SELECT content FROM messages WHERE id = 'm-sys'") == [
        ('{"text": "second"}',)
    ]
    assert _rows(store, "SELECT content FROM messages WHERE id = 'm-other'") == [
        ('{"text": "theirs"}',)
    ]


async def test_an_invocation_must_belong_to_the_owner(db, store):
    for invocation, owner in (("inv-a", "principal:a"), ("inv-b", "principal:b")):
        _execute(
            store,
            f"INSERT INTO invocations (id, skill, started_at, created_at, updated_at, {OWNER}) "
            "VALUES (?, 'review', 0, 0, 0, ?)",
            (invocation, owner),
        )
    await db.create_progression("sp")

    with pytest.raises(StateCompatibilityRefused, match="does not belong to 'principal:a'"):
        await db.create_session(_session("s-b", invocation_id="inv-b"))
    await db.create_session(_session("s-a", invocation_id="inv-a"))

    assert _rows(store, "SELECT id FROM sessions ORDER BY id") == [("s-a",)]
    assert _rows(store, "SELECT id, session_count FROM invocations ORDER BY id") == [
        ("inv-a", 1),
        ("inv-b", 0),
    ]


async def test_an_empty_invocation_id_is_checked_like_any_other(db, store):
    _execute(
        store,
        f"INSERT INTO invocations (id, skill, started_at, created_at, updated_at, {OWNER}) "
        "VALUES ('', 'review', 0, 0, 0, 'principal:b')",
    )
    await db.create_progression("sp")

    with pytest.raises(StateCompatibilityRefused, match="does not belong to 'principal:a'"):
        await db.create_session(_session(invocation_id=""))

    assert _rows(store, "SELECT COUNT(*) FROM sessions") == [(0,)]
    assert _rows(store, "SELECT session_count FROM invocations WHERE id = ''") == [(0,)]


async def test_a_link_by_an_id_that_is_not_a_string_is_refused(db, store):
    """A set lookup by Python equality would take 1.0 for the 1 this run created."""
    await db.insert_message(_system_message(1))
    _execute(
        store,
        f"INSERT INTO messages (id, created_at, content, role, lion_class, {OWNER}) "
        "VALUES ('1.0', 0, '{}', 'system', 1, 'principal:b')",
    )

    with pytest.raises(StateCompatibilityRefused, match="1.0 is not a string id"):
        await db.create_progression("pn", [1.0])

    assert _rows(store, "SELECT COUNT(*) FROM progressions") == [(0,)]


async def test_an_owner_removed_after_open_is_refused_at_the_next_write(db, store):
    await db.create_progression("sp")
    _execute(store, "DELETE FROM principals WHERE id = 'principal:a'")

    with pytest.raises(StateCompatibilityRefused, match="the write transaction: the owner"):
        await db.create_progression("sp2")

    assert _rows(store, "SELECT id FROM progressions") == [("sp",)]


@pytest.mark.parametrize(
    "default", ["", " NOT NULL DEFAULT 'principal:default'"], ids=["nullable", "defaulted"]
)
async def test_an_owned_message_type_is_written_with_the_owner(tmp_path, default):
    store = await _managed_store(tmp_path / "managed.db")
    _execute(store, f"ALTER TABLE message_types ADD COLUMN {OWNER} TEXT{default}")
    profile = _profile(store, inserts={**_INSERTS, "message_types": {"lion_class", OWNER}})
    handle = StateDB(store, compatibility=profile, owner="principal:a")
    await handle.open()
    try:
        await handle.insert_message(_system_message())
    finally:
        await handle.close()

    assert _rows(store, f"SELECT {OWNER} FROM message_types WHERE lion_class = 'System'") == [
        ("principal:a",)
    ]


class _LeavesOutTheOwner(StateDB):
    @_operation("create_progression")
    async def create_progression(self, progression_id: str, collection=None) -> None:
        async with self._tx() as conn:
            await conn.execute(text(self.statement), {"id": progression_id})


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO progressions (id, created_at, collection) VALUES (:id, 1.0, '[]')",
        "INSERT INTO progressions VALUES (:id, 1.0, '[]', 'principal:a')",
        "INSERT INTO 'progressions' (id, created_at, collection) VALUES (:id, 1.0, '[]')",
        "INSERT INTO main.'progressions' (id, created_at, collection) VALUES (:id, 1.0, '[]')",
    ],
    ids=["columns without the owner", "no column list", "string name", "schema and string name"],
)
async def test_an_insert_into_an_owned_table_that_leaves_out_the_owner_is_refused(store, statement):
    """The authorizer sees the table an INSERT writes, never its columns."""
    handle = _LeavesOutTheOwner(store, compatibility=_profile(store), owner="principal:a")
    handle.statement = statement
    await handle.open()
    try:
        with pytest.raises(StateCompatibilityRefused, match="leaves out the owner column"):
            await handle.create_progression("p1")
    finally:
        await handle.close()

    assert _rows(store, "SELECT COUNT(*) FROM progressions") == [(0,)]


class _RawWrite(StateDB):
    @_operation("create_progression")
    async def create_progression(self, progression_id: str, collection=None) -> None:
        async with self._tx() as conn:
            await conn.execute(text(self.statement), {"id": progression_id, **self.params})


def _invocation(store: Path, owner: str) -> None:
    _execute(
        store,
        f"INSERT INTO invocations (id, skill, started_at, created_at, updated_at, {OWNER}) "
        "VALUES ('p1', 'skill', 0.0, 0.0, 0.0, ?)",
        (owner,),
    )


async def _raw_write(store: Path, statement: str, params: dict) -> None:
    handle = _RawWrite(store, compatibility=_profile(store), owner="principal:a")
    handle.statement, handle.params = statement, params
    await handle.open()
    try:
        await handle.create_progression("p1")
    finally:
        await handle.close()


_PROGRESSION = f"INSERT INTO progressions (id, created_at, collection, {OWNER}) "


@pytest.mark.parametrize(
    ("statement", "params", "reason"),
    [
        (_PROGRESSION + "VALUES (:id, 1.0, '[]', 'principal:b')", {}, "other than the owner"),
        (_PROGRESSION + "VALUES (:id, 1.0, '[]', NULL)", {}, "other than the owner"),
        (
            _PROGRESSION
            + "VALUES (:id, 1.0, '[]', (SELECT id FROM principals WHERE id = 'principal:b'))",
            {},
            "other than the owner",
        ),
        (
            _PROGRESSION + "VALUES (:id, 1.0, '[]', :o)",
            {"o": "principal:b"},
            "other than the owner",
        ),
        # The owner sits in another column's slot: the owner's own slot is what is read.
        (
            _PROGRESSION + "VALUES (:id, 1.0, :col, :o)",
            {"col": "principal:a", "o": "principal:b"},
            "other than the owner",
        ),
        (
            _PROGRESSION + "VALUES (:id, 1.0, '[]', :a), ('p2', 1.0, '[]', :b)",
            {"a": "principal:a", "b": "principal:b"},
            "other than the owner",
        ),
        (_PROGRESSION + "SELECT :id, 1.0, '[]', 'principal:b'", {}, "from a query"),
        (
            _PROGRESSION + "VALUES (:id, 1.0, '[]', :o) ON CONFLICT (id) DO UPDATE SET "
            "collection = excluded.collection",
            {"o": "principal:a"},
            "upsert",
        ),
        (
            "UPDATE invocations SET updated_at = 1.0 WHERE id = :id",
            {},
            "does not restrict its rows",
        ),
        (
            f"UPDATE invocations SET updated_at = 1.0 WHERE id = :id AND {OWNER} = :o",
            {"o": "principal:b"},
            "does not restrict its rows",
        ),
        (
            f"UPDATE invocations SET updated_at = 1.0 WHERE id = :id OR {OWNER} = :o",
            {"o": "principal:a"},
            "not a plain conjunction",
        ),
        (
            f"UPDATE invocations SET updated_at = 1.0 WHERE session_count BETWEEN 0 AND {OWNER} = :o",
            {"o": "principal:a"},
            "not a plain conjunction",
        ),
        (
            f"UPDATE invocations SET updated_at = 1.0 WHERE id = :id AND NOT {OWNER} = :o",
            {"o": "principal:a"},
            "does not restrict its rows",
        ),
        (
            f"UPDATE 'invocations' SET updated_at = 1.0 WHERE {OWNER} = :o",
            {"o": "principal:b"},
            "does not restrict its rows",
        ),
    ],
    ids=[
        "owner literal",
        "owner null",
        "owner from a query",
        "owner bound to another",
        "owner slot holds another",
        "one row of two another",
        "rows from a query",
        "upsert that updates",
        "update without the owner",
        "update bound to another",
        "update with or",
        "update with between",
        "update with not",
        "update of a string name",
    ],
)
async def test_a_write_that_does_not_bind_the_owner_is_refused(store, statement, params, reason):
    """The authorizer sees neither the values a write binds nor the rows its WHERE clause picks."""
    _invocation(store, "principal:b")
    with pytest.raises(StateCompatibilityRefused, match=reason):
        await _raw_write(store, statement, params)

    assert _rows(store, "SELECT COUNT(*) FROM progressions") == [(0,)]
    assert _rows(store, "SELECT updated_at FROM invocations WHERE id = 'p1'") == [(0.0,)]


@pytest.mark.parametrize(
    ("statement", "written", "expected"),
    [
        (
            _PROGRESSION + "VALUES (:id, 1.0, '[]', :o)",
            f"SELECT {OWNER} FROM progressions WHERE id = 'p1'",
            [("principal:a",)],
        ),
        (
            f"UPDATE invocations SET updated_at = 1.0 WHERE id = :id AND {OWNER} = :o",
            "SELECT updated_at FROM invocations WHERE id = 'p1'",
            [(1.0,)],
        ),
    ],
    ids=["insert", "update"],
)
async def test_a_write_that_binds_the_owner_is_admitted(store, statement, written, expected):
    _invocation(store, "principal:a")
    await _raw_write(store, statement, {"o": "principal:a"})

    assert _rows(store, written) == expected


_MIRROR = "CREATE TRIGGER mirror AFTER INSERT ON messages BEGIN {body}; END"


@pytest.mark.parametrize(
    "body",
    [
        "INSERT INTO progressions (id, created_at, collection) VALUES (NEW.id, 1.0, '[]')",
        f"INSERT INTO progressions (id, created_at, collection, {OWNER}) "
        "VALUES (NEW.id, 1.0, '[]', 'principal:b')",
        f"INSERT INTO 'progressions' (id, created_at, collection, {OWNER}) "
        f"SELECT NEW.id, 1.0, '[]', NEW.{OWNER}",
        "UPDATE progressions SET collection = '[]' WHERE id = NEW.id",
        f"UPDATE progressions SET collection = '[]' WHERE id = NEW.id OR {OWNER} = NEW.{OWNER}",
    ],
    ids=["owner left out", "owner literal", "rows from a query", "update by id", "update with or"],
)
async def test_a_trigger_that_writes_an_owned_table_without_the_owner_is_refused(tmp_path, body):
    """A trigger's statements pass no statement check, so admission reads them."""
    store = await _managed_store(tmp_path / "managed.db")
    _execute(store, _MIRROR.format(body=body))
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")
    with pytest.raises(StateCompatibilityRefused, match="trigger mirror"):
        await handle.open()
    assert handle._engine is None


@pytest.mark.parametrize(
    "body",
    [
        f"INSERT INTO progressions (id, created_at, collection, {OWNER}) "
        f"VALUES (NEW.id, 1.0, '[]', NEW.{OWNER})",
        f"UPDATE progressions SET collection = '[]' WHERE id = NEW.id AND {OWNER} = NEW.{OWNER}",
    ],
    ids=["insert", "update"],
)
async def test_a_trigger_that_copies_the_owner_is_admitted(tmp_path, body):
    store = await _managed_store(tmp_path / "managed.db")
    _execute(store, _MIRROR.format(body=body))
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")
    await handle.open()
    await handle.close()


async def test_an_invocation_is_refused_when_the_profile_records_no_owner(store):
    profile = _profile(store)
    plain = CompatibilityProfile(
        name="plain",
        version=1,
        closure=profile.closure,
        writes={
            name: spec
            for name, spec in profile.writes.items()
            if name not in ("sessions", "status_transitions")
        }
        | {
            "sessions": TableWrites(
                insert=_INSERTS["sessions"] - {OWNER},
                update=_UPDATES["sessions"],
                unwritten={
                    c: "left to the store's default"
                    for c in profile.writes["sessions"].unwritten.keys() | {OWNER}
                },
            )
        },
        operations=_OPERATIONS,
    )
    handle = StateDB(store, compatibility=plain)
    await handle.open()
    try:
        await handle.create_progression("sp")
        with pytest.raises(StateCompatibilityRefused, match="records no owner"):
            await handle.create_session(_session(invocation_id="inv-a"))
    finally:
        await handle.close()

    assert _rows(store, "SELECT COUNT(*) FROM sessions") == [(0,)]


async def test_a_row_is_the_runs_only_once_its_transaction_commits(db, store, monkeypatch):
    await db.create_progression("sp")
    await db.create_progression("bp")

    async def fail(*_args, **_kwargs):
        raise RuntimeError("the lifecycle write failed")

    with monkeypatch.context() as patched:
        patched.setattr(db, "_initialize_managed_entity_in_tx", fail)
        with pytest.raises(RuntimeError, match="lifecycle write failed"):
            await db.create_session(_session())

    assert _rows(store, "SELECT COUNT(*) FROM sessions") == [(0,)]
    with pytest.raises(StateCompatibilityRefused, match="session 's1' was not created"):
        await db.create_branch({"id": "b1", "session_id": "s1", "progression_id": "bp"})


async def test_another_handle_on_the_store_did_not_create_this_runs_rows(db, store):
    await db.create_progression("sp")
    await db.create_progression("bp")
    await db.create_session(_session())
    other = StateDB(store, compatibility=db.compatibility, owner="principal:a")
    await other.open()
    try:
        with pytest.raises(StateCompatibilityRefused, match="was not created by this run"):
            await other.create_branch({"id": "b1", "session_id": "s1", "progression_id": "bp"})
    finally:
        await other.close()


def _live_message(message_id: str = "m1", content: str = "hello", role: str = "user") -> dict:
    return {
        "id": message_id,
        "created_at": 2.0,
        "content": {"text": content},
        "role": role,
        "node_metadata": {"lion_class": "Instruction"},
    }


async def test_a_live_message_joins_the_runs_session_branch_and_progressions(db, store):
    await _setup_run(db)
    ((started,),) = _rows(store, "SELECT last_message_at FROM sessions WHERE id = 's1'")

    await db._persist_live_message(
        _live_message(),
        session_id="s1",
        branch_progression_id="bp",
        session_progression_id="sp",
        activity_at=started + 10,
    )
    await db._persist_live_message(
        _live_message("m-sys2", role="system"),
        session_id="s1",
        branch_progression_id="bp",
        system_branch_id="b1",
        activity_at=started + 5,
    )

    assert _rows(store, f"SELECT {OWNER} FROM messages WHERE id IN ('m1', 'm-sys2')") == [
        ("principal:a",),
        ("principal:a",),
    ]
    assert _rows(store, "SELECT id, collection FROM progressions ORDER BY id") == [
        ("bp", '["m1","m-sys2"]'),
        ("sp", '["m1"]'),
    ]
    assert _rows(store, "SELECT system_msg_id FROM branches WHERE id = 'b1'") == [("m-sys2",)]
    assert _rows(store, "SELECT last_message_at FROM sessions WHERE id = 's1'") == [(started + 10,)]


async def test_a_live_message_written_again_is_rewritten_and_listed_once(db, store):
    await _setup_run(db)
    for content in ("first", "second"):
        await db._persist_live_message(
            _live_message(content=content), session_id="s1", branch_progression_id="bp"
        )

    assert _rows(store, "SELECT content FROM messages WHERE id = 'm1'") == [('{"text": "second"}',)]
    assert _rows(store, "SELECT collection FROM progressions WHERE id = 'bp'") == [('["m1"]',)]


def _foreign_run(store: Path) -> None:
    """A session, branch and progression another principal's run wrote."""
    _foreign_rows(store)
    _execute(
        store,
        f"INSERT INTO sessions (id, created_at, updated_at, progression_id, {OWNER}) "
        "VALUES ('s-other', 0, 0, 'p-other', 'principal:b')",
    )
    _execute(
        store,
        f"INSERT INTO branches (id, created_at, session_id, progression_id, {OWNER}) "
        "VALUES ('b-other', 0, 's-other', 'p-other', 'principal:b')",
    )


@pytest.mark.parametrize(
    "link, value",
    [
        ("session_id", "s-other"),
        ("branch_progression_id", "p-other"),
        ("session_progression_id", "p-other"),
        ("system_branch_id", "b-other"),
    ],
)
async def test_a_live_message_links_only_rows_its_run_created(db, store, link, value):
    await _setup_run(db)
    _foreign_run(store)
    links = {"session_id": "s1", "branch_progression_id": "bp", "session_progression_id": "sp"}
    links[link] = value
    before = {
        table: _rows(store, f"SELECT * FROM {table} ORDER BY id")
        for table in ("sessions", "branches", "progressions", "messages")
    }

    with pytest.raises(StateCompatibilityRefused, match="was not created by this run"):
        await db._persist_live_message(_live_message(role="system"), **links)

    for table, rows in before.items():
        assert _rows(store, f"SELECT * FROM {table} ORDER BY id") == rows, table


@pytest.mark.parametrize(
    "table, row_id", [("sessions", "s1"), ("progressions", "bp"), ("branches", "b1")]
)
async def test_a_live_message_refuses_a_row_that_no_longer_carries_the_owner(
    db, store, table, row_id
):
    await _setup_run(db)
    # The managing application handed one of the run's rows to another principal.
    _execute(store, f"UPDATE {table} SET {OWNER} = 'principal:b' WHERE id = ?", (row_id,))
    before = {
        name: _rows(store, f"SELECT * FROM {name} ORDER BY id")
        for name in ("sessions", "branches", "progressions", "messages")
    }

    with pytest.raises(StateCompatibilityRefused, match="does not belong to 'principal:a'"):
        await db._persist_live_message(
            _live_message(role="system"),
            session_id="s1",
            branch_progression_id="bp",
            system_branch_id="b1",
        )

    for name, rows in before.items():
        assert _rows(store, f"SELECT * FROM {name} ORDER BY id") == rows, name


async def test_every_update_a_live_message_makes_carries_the_owner(db, store):
    """Beside the check before them, the updates themselves carry the owner predicate (D5)."""
    from sqlalchemy import event

    await _setup_run(db)
    updates = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE"):
            updates.append((statement, parameters))

    event.listen(db._engine.sync_engine, "before_cursor_execute", spy)
    try:
        await db._persist_live_message(
            _live_message(role="system"),
            session_id="s1",
            branch_progression_id="bp",
            session_progression_id="sp",
            system_branch_id="b1",
        )
    finally:
        event.remove(db._engine.sync_engine, "before_cursor_execute", spy)

    assert sorted(statement.split()[1] for statement, _ in updates) == [
        "branches",
        "progressions",
        "progressions",
        "sessions",
    ]
    for statement, parameters in updates:
        values = parameters.values() if isinstance(parameters, dict) else parameters
        assert f'"{OWNER}" = ' in statement, statement
        assert "principal:a" in values, statement


async def test_a_refused_setup_stops_the_run(store, monkeypatch):
    """A refusal at setup is raised, not turned into a run without persistence (ADR-0128 D6)."""
    from lionagi.cli import _runs
    from lionagi.session.branch import Branch

    profile = _profile(store, operations=_OPERATIONS - {"create_session"})
    handle = StateDB(store, compatibility=profile, owner="principal:a")
    await handle.open()

    async def compatibility_store(*args, **kwargs):
        return handle

    monkeypatch.setattr(_runs, "_open_shared_db", compatibility_store)
    degraded = []
    monkeypatch.setattr(_runs, "_record_persistence_degraded", lambda *a, **k: degraded.append(a))

    with pytest.raises(StateCompatibilityRefused, match="create_session refused"):
        await _runs.setup_agent_persist(Branch(), share_db=False)

    assert degraded == []
    assert _rows(store, "SELECT COUNT(*) FROM sessions") == [(0,)]


def _dump(store: Path) -> list[str]:
    with closing(sqlite3.connect(store)) as conn:
        return list(conn.iterdump())


async def test_a_refused_signal_write_fails_the_teardown_with_it(db, store):
    """A signal the store refused is not a dropped one to read past: the run fails with the
    refusal, and the drop counter does not absorb it (ADR-0128 D6)."""
    from lionagi.cli import _runs
    from lionagi.session.session import Session
    from lionagi.session.signal import Signal

    await _setup_run(db)
    session = Session()
    session.observer.bind_db_persistence("s1", db=db)
    _execute(store, f"UPDATE sessions SET {OWNER} = 'principal:b' WHERE id = 's1'")
    await session.observer.emit(Signal(data={"step": 1}))
    _execute(store, f"UPDATE sessions SET {OWNER} = 'principal:a' WHERE id = 's1'")
    ctx = {
        "db": db,
        "session_id": "s1",
        "session_prog_id": "sp",
        "session": session,
        "shared_db_registered": False,
    }

    result = await _runs.teardown_persist(ctx, status="completed")

    assert result == "failed"
    status, reason = ctx["terminal_reason"]
    assert status == "failed"
    assert "The store refused a write" in reason and "does not belong to" in reason
    # The refusal itself is kept beside the reason: an Operator turn reads it to record
    # `store_refused` rather than whatever outcome the turn had recorded.
    assert isinstance(ctx["store_refusal"], StateCompatibilityRefused)
    assert "does not belong to" in str(ctx["store_refusal"])
    assert ctx["signal_persist_dropped"] == 0
    # The refused write is not a dropped one: it was not persisted, and nothing retried it.
    assert _rows(store, "SELECT COUNT(*) FROM session_signals WHERE session_id = 's1'") == [(0,)]


async def test_a_signal_emitted_during_teardown_that_is_refused_fails_the_run(db, store):
    """The same refusal raised while teardown itself runs, after the signal sink's own emit, also
    fails the run rather than reading back a status the store could not take with it."""
    from lionagi.cli import _runs
    from lionagi.session.session import Session
    from lionagi.session.signal import Signal

    await _setup_run(db)
    session = Session()
    session.observer.bind_db_persistence("s1", db=db)
    original = db.update_artifact_verification

    async def owner_moves_then_signal(session_id, verification):
        _execute(store, f"UPDATE sessions SET {OWNER} = 'principal:b' WHERE id = 's1'")
        await session.observer.emit(Signal(data={"late": 1}))
        _execute(store, f"UPDATE sessions SET {OWNER} = 'principal:a' WHERE id = 's1'")
        await original(session_id, verification)

    db.update_artifact_verification = owner_moves_then_signal
    ctx = {
        "db": db,
        "session_id": "s1",
        "session_prog_id": "sp",
        "session": session,
        "shared_db_registered": False,
    }

    result = await _runs.teardown_persist(ctx, status="completed")

    assert result == "failed"
    assert ctx["terminal_reason"][0] == "failed"
    assert "The store refused a write" in ctx["terminal_reason"][1]


async def test_a_teardown_reason_names_the_operations_that_already_committed(db, store):
    """A refusal from a later write says nothing committed, because each operation opens its own
    scope. The run's reason names what this teardown did commit (ADR-0128 D6)."""
    from lionagi.cli import _runs

    await _setup_run(db)
    original = db.update_artifact_verification

    async def owner_moves_then_write(session_id, verification):
        _execute(store, f"UPDATE sessions SET {OWNER} = 'principal:b' WHERE id = 's1'")
        await original(session_id, verification)

    db.update_artifact_verification = owner_moves_then_write
    ctx = {
        "db": db,
        "session_id": "s1",
        "session_prog_id": "sp",
        "shared_db_registered": False,
    }

    result = await _runs.teardown_persist(ctx, status="completed", extras={"run": 1})

    assert result == "failed"
    status, reason = ctx["terminal_reason"]
    assert status == "failed"
    assert "already committed in this teardown: update_session" in reason
    assert _stored_node_metadata(store) == {"run": 1}


async def test_a_teardown_reason_without_a_commit_names_none(db, store):
    """The first write refused leaves the reason as it was: nothing to name."""
    from lionagi.cli import _runs

    await _setup_run(db)
    _execute(store, f"UPDATE sessions SET {OWNER} = 'principal:b' WHERE id = 's1'")
    ctx = {
        "db": db,
        "session_id": "s1",
        "session_prog_id": "sp",
        "shared_db_registered": False,
    }

    result = await _runs.teardown_persist(ctx, status="completed", extras={"run": 1})

    assert result == "failed"
    assert "already committed in this teardown" not in ctx["terminal_reason"][1]


def _stored_node_metadata(store: Path, session_id: str = "s1") -> dict:
    ((value,),) = _rows(store, "SELECT node_metadata FROM sessions WHERE id = ?", (session_id,))
    return json.loads(value)


def _set_node_metadata(store: Path, value: str, session_id: str = "s1") -> None:
    _execute(store, "UPDATE sessions SET node_metadata = ? WHERE id = ?", (value, session_id))


# --- A status change merges node_metadata by key (ADR-0128 Appendix A, teardown update_session) ---


async def test_a_status_change_merges_node_metadata_by_key(db, store):
    """A key another writer stored survives a status write that carries its own metadata."""
    from lionagi.state.reasons import RunReasons

    await _setup_run(db)
    _set_node_metadata(store, '{"managed": "kept"}')

    await db.update_session(
        "s1",
        status="completed",
        reason_code=RunReasons.COMPLETED_OK,
        reason_actor="s1",
        node_metadata={"run": 1},
    )

    assert _stored_node_metadata(store) == {"managed": "kept", "run": 1}
    assert _rows(store, "SELECT status FROM sessions WHERE id = 's1'") == [("completed",)]


@pytest.mark.parametrize(
    "given", ["[1]", "not json", None, 7], ids=["array", "text", "null", "int"]
)
async def test_a_status_change_refuses_node_metadata_that_is_not_an_object(db, store, given):
    """The by-key contract a status write now shares with the merge refuses the same values."""
    from lionagi.state.reasons import RunReasons

    await _setup_run(db)
    before = _dump(store)

    with pytest.raises(StateCompatibilityRefused, match="is not a JSON object"):
        await db.update_session(
            "s1",
            status="completed",
            reason_code=RunReasons.COMPLETED_OK,
            reason_actor="s1",
            node_metadata=given,
        )

    assert _dump(store) == before


async def test_a_status_change_honours_set_if_null_for_node_metadata(db, store):
    """``set_if_null`` reaches a status write's node_metadata, so a stored object is kept."""
    from lionagi.state.reasons import RunReasons

    await _setup_run(db)
    _set_node_metadata(store, '{"managed": "kept"}')

    await db.update_session(
        "s1",
        status="completed",
        reason_code=RunReasons.COMPLETED_OK,
        reason_actor="s1",
        node_metadata={"run": 1},
        set_if_null=frozenset({"node_metadata"}),
    )

    assert _stored_node_metadata(store) == {"managed": "kept"}


async def test_a_status_change_fills_node_metadata_over_a_stored_json_null(db, store):
    """A stored JSON null is an absence, so ``set_if_null`` writes the given keys into it."""
    from lionagi.state.reasons import RunReasons

    await _setup_run(db)
    _set_node_metadata(store, "null")

    await db.update_session(
        "s1",
        status="completed",
        reason_code=RunReasons.COMPLETED_OK,
        reason_actor="s1",
        node_metadata={"run": 1},
        set_if_null=frozenset({"node_metadata"}),
    )

    assert _stored_node_metadata(store) == {"run": 1}


async def test_a_set_if_null_write_fills_node_metadata_over_a_stored_json_null(db, store):
    """The write that carries no status merges through the store's own SQL, which treats a stored
    JSON null as an absence just as the status path does."""
    await _setup_run(db)
    _set_node_metadata(store, "null")

    await db.update_session(
        "s1", node_metadata={"run": 1}, set_if_null=frozenset({"node_metadata"})
    )

    assert _stored_node_metadata(store) == {"run": 1}


async def test_a_status_change_replaces_a_nested_object_under_a_key(db, store):
    """The merge is by top-level key, so a nested object replaces the stored one under that key."""
    from lionagi.state.reasons import RunReasons

    await _setup_run(db)
    _set_node_metadata(store, '{"khive_injection": {"other": 1}}')

    await db.update_session(
        "s1",
        status="completed",
        reason_code=RunReasons.COMPLETED_OK,
        reason_actor="s1",
        node_metadata={"khive_injection": {"hits": 2}},
    )

    assert _stored_node_metadata(store) == {"khive_injection": {"hits": 2}}


# --- Links are checked against the target row's current owner ------------------------------------


@pytest.mark.parametrize(
    "link, table, target",
    [
        ("first_msg_id", "messages", "m-sys"),
        ("last_msg_id", "messages", "m-sys"),
        ("invocation_id", "invocations", "inv-1"),
    ],
)
async def test_a_session_update_refuses_a_link_reowned_after_creation(
    db, store, link, table, target
):
    """A row this run created but that since changed hands is not this run's to link."""
    await _setup_run(db)
    if table == "invocations":
        _execute(
            store,
            f"INSERT INTO invocations (id, skill, started_at, created_at, updated_at, {OWNER}) "
            "VALUES ('inv-1', 'review', 0, 0, 0, 'principal:a')",
        )
    _execute(store, f"UPDATE {table} SET {OWNER} = 'principal:b' WHERE id = ?", (target,))
    before = _dump(store)

    with pytest.raises(StateCompatibilityRefused, match="does not belong to 'principal:a'"):
        await db.update_session("s1", **{link: target})

    assert _dump(store) == before


async def test_a_session_update_links_a_row_this_run_still_owns(db, store):
    await _setup_run(db)

    await db.update_session("s1", first_msg_id="m-sys", last_msg_id="m-sys")

    assert _rows(store, "SELECT first_msg_id, last_msg_id FROM sessions WHERE id = 's1'") == [
        ("m-sys", "m-sys")
    ]


async def test_an_invocation_id_update_is_refused_by_the_profile_allowlist(db, store):
    """The link is checked like the others, and the write behind it is then refused: the profile's
    update allowlist does not admit sessions.invocation_id, so no writer moves that column here."""
    await _setup_run(db)
    _execute(
        store,
        f"INSERT INTO invocations (id, skill, started_at, created_at, updated_at, {OWNER}) "
        "VALUES ('inv-1', 'review', 0, 0, 0, 'principal:a')",
    )
    before = _dump(store)

    with pytest.raises(StateCompatibilityRefused, match="authorizer denied"):
        await db.update_session("s1", invocation_id="inv-1")

    assert _dump(store) == before


async def test_a_refused_link_leaves_the_status_it_was_asked_for_unchanged(db, store):
    """A status change was written before this call's link checks, so a refused link left a
    committed status behind. The links are now checked before the status is routed, so a refused
    link leaves the stored status as it was and the refusal names no committed status write."""
    from lionagi.state.reasons import RunReasons

    await _setup_run(db)
    _execute(store, f"UPDATE messages SET {OWNER} = 'principal:b' WHERE id = 'm-sys'")
    before = _dump(store)

    with pytest.raises(
        StateCompatibilityRefused, match="does not belong to 'principal:a'"
    ) as caught:
        await db.update_session(
            "s1",
            status="completed",
            reason_code=RunReasons.COMPLETED_OK,
            reason_actor="s1",
            first_msg_id="m-sys",
        )

    assert "already committed" not in str(caught.value), str(caught.value)
    assert _rows(store, "SELECT status FROM sessions WHERE id = 's1'") == [("running",)]
    assert _dump(store) == before


async def test_a_refused_invocation_link_leaves_the_status_unchanged(db, store):
    """The same holds for an invocation link that names no row: the refusal is raised before the
    status write, so the stored status is unchanged."""
    from lionagi.state.reasons import RunReasons

    await _setup_run(db)
    before = _dump(store)

    with pytest.raises(StateCompatibilityRefused, match="invocation") as caught:
        await db.update_session(
            "s1",
            status="completed",
            reason_code=RunReasons.COMPLETED_OK,
            reason_actor="s1",
            invocation_id="inv-missing",
        )

    assert "already committed" not in str(caught.value), str(caught.value)
    assert _rows(store, "SELECT status FROM sessions WHERE id = 's1'") == [("running",)]
    assert _dump(store) == before


async def test_a_link_whose_target_table_records_no_owner_is_refused(tmp_path):
    """A linked table with no owner column cannot show the row is this run's, so the link is
    refused, as ``create_session`` refuses an invocation link there."""
    from lionagi.state.compat import TableWrites

    store = await _managed_store(tmp_path / "managed.db")
    with closing(sqlite3.connect(store)) as conn:
        conn.execute(f"ALTER TABLE invocations DROP COLUMN {OWNER}")
        conn.execute(
            "INSERT INTO invocations (id, skill, started_at, created_at, updated_at) "
            "VALUES ('inv-1', 'review', 0, 0, 0)"
        )
        conn.commit()
    base = _profile(store)
    assert "invocations" not in base.owned_tables
    writes = dict(base.writes)
    sessions = writes["sessions"]
    writes["sessions"] = TableWrites(
        insert=sessions.insert,
        update=sessions.update | {"invocation_id"},
        unwritten=sessions.unwritten,
    )
    handle = StateDB(store, compatibility=_profile(store, writes=writes), owner="principal:a")
    await handle.open()
    try:
        await _setup_run(handle)

        with pytest.raises(StateCompatibilityRefused, match="records no owner column"):
            await handle.update_session("s1", invocation_id="inv-1")
    finally:
        await handle.close()


# --- The merge keeps a stored non-object under _discarded_node_metadata ---------------------------


async def test_a_merge_keeps_a_stored_non_object_under_a_discarded_key(db, store):
    """``update_session`` refuses a non-object payload, while the merge keeps a stored one."""
    await _setup_run(db)
    _set_node_metadata(store, "[1]")

    await db.merge_session_node_metadata("s1", {"linked": "engine-1"})

    stored = _stored_node_metadata(store)
    assert stored["linked"] == "engine-1"
    assert stored["_discarded_node_metadata"] == [1]


async def test_a_merge_over_a_stored_non_object_is_refused_through_update_session(db, store):
    """The two entry points agree: a non-object value handed in is refused, whether it is given
    to the merge or to a status write."""
    await _setup_run(db)
    _set_node_metadata(store, "[1]")
    before = _dump(store)

    with pytest.raises(StateCompatibilityRefused, match="is not a JSON object"):
        await db.update_session("s1", node_metadata=[1])

    assert _dump(store) == before


class _Message:
    """What a branch hands its live hook: a message with ``to_dict(mode=...)``."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def to_dict(self, mode: str = "python") -> dict:
        return dict(self._data)


async def test_the_runs_live_hook_raises_a_refusal(db, store):
    """The CLI's live hook lets a refusal through instead of logging it (ADR-0128 D6)."""
    from lionagi.cli._runs import _make_message_handler

    await _setup_run(db)
    _execute(store, f"UPDATE sessions SET {OWNER} = 'principal:b' WHERE id = 's1'")
    queues: list = []
    on_message = _make_message_handler(db, "b1", "s1", "bp", "sp", message_retry_queues=queues)

    with pytest.raises(StateCompatibilityRefused, match="does not belong to 'principal:a'"):
        await on_message(_Message(_live_message()))

    assert queues[0].pending_count == 1
    assert _rows(store, "SELECT COUNT(*) FROM messages WHERE id = 'm1'") == [(0,)]


async def test_a_refused_teardown_fails_the_run_with_the_refusal(db, store):
    """Teardown neither swallows a refusal nor reads back a status it could not write."""
    from lionagi.cli._runs import _make_message_handler, teardown_persist
    from lionagi.hooks import route_message_persistence
    from lionagi.hooks.bus import HookPoint
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session

    await _setup_run(db)
    _execute(store, f"UPDATE sessions SET {OWNER} = 'principal:b' WHERE id = 's1'")
    queues: list = []
    on_message = _make_message_handler(db, "b1", "s1", "bp", "sp", message_retry_queues=queues)
    with pytest.raises(StateCompatibilityRefused):
        await on_message(_Message(_live_message()))
    session, branch = Session(), Branch()
    hook = route_message_persistence(session, branch, on_message)
    ctx = {
        "db": db,
        "session_id": "s1",
        "session_prog_id": "sp",
        "branch": branch,
        "hook": hook,
        "message_retry_queues": queues,
        "shared_db_registered": False,
    }

    assert await teardown_persist(ctx, status="completed") == "failed"

    status, reason = ctx["terminal_reason"]
    assert status == "failed"
    assert "persist_live_message refused" in reason
    assert isinstance(ctx["store_refusal"], StateCompatibilityRefused)
    assert str(ctx["store_refusal"]) in reason
    assert hook not in session.hooks.handlers_for(HookPoint.MESSAGE_ADD)
    assert _rows(store, "SELECT status FROM sessions WHERE id = 's1'") == [("running",)]


class _FlushQueue:
    def __init__(self, refuse: bool) -> None:
        self.refuse = refuse
        self.attempts = 0

    async def flush_final(self) -> bool:
        self.attempts += 1
        if self.refuse:
            raise StateCompatibilityRefused("persist_live_message", "owned@1", "refused")
        return True


async def test_teardown_flushes_every_queue_before_it_raises_a_refusal():
    from lionagi.cli._runs import _flush_pending_message_events

    first, second = _FlushQueue(refuse=True), _FlushQueue(refuse=False)

    with pytest.raises(StateCompatibilityRefused):
        await _flush_pending_message_events({"message_retry_queues": [first, second]})

    assert (first.attempts, second.attempts) == (1, 1)


class _RefusingFlushQueue:
    def __init__(self, target: str) -> None:
        self.target = target

    async def flush_final(self) -> bool:
        raise StateCompatibilityRefused("persist_live_message", self.target, "refused")


async def test_teardown_logs_every_refusal_after_the_one_it_raises(caplog):
    from lionagi.cli._runs import _flush_pending_message_events

    queues = [_RefusingFlushQueue("messages@1"), _RefusingFlushQueue("messages@2")]

    with pytest.raises(StateCompatibilityRefused, match="messages@1"):
        await _flush_pending_message_events({"message_retry_queues": queues})

    (record,) = [r for r in caplog.records if "messages@2" in r.getMessage()]
    assert record.levelname == "ERROR"


@pytest.mark.parametrize("entry", ["dependency-aware", "reactive", "stream"])
async def test_a_refused_write_in_a_flow_operation_stops_the_flow(entry):
    """A refusal in one operation is raised from the flow, instead of failing that operation while
    the others run on (ADR-0128 D6)."""
    import anyio

    from lionagi.operations import flow
    from lionagi.operations.builder import OperationGraphBuilder
    from lionagi.session.branch import Branch
    from lionagi.session.session import Session

    ran = []

    async def refused(**kw):
        raise StateCompatibilityRefused("persist_live_message", "messages@1", "refused")

    async def slow(**kw):
        await anyio.sleep(0.5)
        ran.append("slow")
        return "slow"

    session = Session()
    branch = Branch(name="root")
    session.include_branches(branch)
    session.default_branch = branch
    session.register_operation("refused", refused)
    session.register_operation("slow", slow)
    builder = OperationGraphBuilder()
    builder.add_operation("refused", depends_on=[])
    builder.add_operation("slow", depends_on=[])

    graph = builder.get_graph()

    with pytest.raises(StateCompatibilityRefused, match="messages@1"):
        if entry == "stream":
            async for _ in session.flow_stream(graph):
                pass
        else:
            await flow(session, graph, reactive=entry == "reactive")
    assert ran == []


async def test_a_refused_message_write_in_a_stream_run_keeps_its_type(monkeypatch):
    """A refusal is no provider failure: a stream run raises it as itself (ADR-0128 D6)."""
    import types
    from unittest.mock import AsyncMock

    from lionagi.operations.run.run import RunParam, run
    from lionagi.service.imodel import iModel
    from lionagi.service.types.stream_chunk import StreamChunk
    from lionagi.session.branch import Branch

    model = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    model.endpoint = types.SimpleNamespace(
        is_cli=True, session_id=None, to_dict=lambda: {"type": "fake_cli", "session_id": None}
    )
    model.streaming_process_func = None

    async def create_event(**kw):
        return object()

    async def stream(api_call=None):
        yield StreamChunk(type="text", content="reply")

    model.create_event = create_event
    model.executor = types.SimpleNamespace(append=AsyncMock(), config={})
    model.stream = stream
    branch = Branch()
    branch.chat_model = model
    add_message = branch.msgs.a_add_message

    async def refusing(**kwargs):
        if "assistant_response" in kwargs:
            raise StateCompatibilityRefused("persist_live_message", "messages@1", "refused")
        return await add_message(**kwargs)

    monkeypatch.setattr(branch.msgs, "a_add_message", refusing)

    with pytest.raises(StateCompatibilityRefused, match="messages@1"):
        async for _ in run(branch, "do something", RunParam()):
            pass


async def test_a_refused_setup_finishes_the_runs_record(tmp_path, monkeypatch):
    """`li agent` stops at a refused setup, and its run.json reads `failed` with the refusal
    instead of `running` (ADR-0128 D6)."""
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import lionagi.cli._runs as runs_mod
    import lionagi.cli.agent as agent_mod
    from lionagi.cli._runs import allocate_run
    from lionagi.service.manager import iModelManager

    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "codex/model")
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}", agent_definition_hash=lambda n: "abc"
        ),
    )
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")
    run = allocate_run(run_id="refused-setup")
    monkeypatch.setattr(agent_mod, "allocate_run", lambda: run)

    async def refused_setup(*args, **kwargs):
        raise StateCompatibilityRefused("create_session", "sessions@1", "refused")

    monkeypatch.setattr(agent_mod, "setup_agent_persist", refused_setup)

    from lionagi.cli.agent import _run_agent

    with pytest.raises(StateCompatibilityRefused):
        await _run_agent("codex/model", "do the thing")

    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["status"] == "failed"
    assert manifest["ended_at"] is not None
    assert manifest["error"].startswith("The store refused a write: create_session refused")


@pytest.mark.parametrize(
    ("compatibility", "error"),
    [(None, None), ("profile", "The store refused a write: x")],
    ids=["native", "compatibility"],
)
async def test_an_orchestrate_run_json_names_the_reason_in_compatibility_mode(
    monkeypatch, compatibility, error
):
    from types import SimpleNamespace

    from lionagi.cli.orchestrate import _orchestration

    async def teardown(ctx, **kwargs):
        ctx["terminal_reason"] = ("failed", "The store refused a write: x")
        return "failed"

    from lionagi.state.compat import Closure

    monkeypatch.setattr(_orchestration, "teardown_persist", teardown)
    written: list = []
    profile = (
        CompatibilityProfile(
            name="orchestrate-records",
            version=1,
            closure=Closure(tables=()),
            writes={},
            operations={"run"},
        )
        if compatibility == "profile"
        else None
    )
    env = SimpleNamespace(
        _live_persist={"db": SimpleNamespace(compatibility=profile)},
        cwd=None,
        _run_manifest={},
        run=SimpleNamespace(write_manifest=written.append),
    )

    assert await _orchestration.stop_live_persist(env, status="failed") == "failed"
    assert written[0]["status"] == "failed"
    assert written[0].get("error") == error


@pytest.mark.parametrize(
    ("compatibility", "recorded"),
    [(None, "completed"), ("profile", "failed")],
    ids=["native", "compatibility"],
)
async def test_an_operator_turn_records_the_status_its_teardown_decided_in_compatibility_mode(
    tmp_path, monkeypatch, compatibility, recorded
):
    """A turn the engine finished whose teardown the store refused reads `failed` in its run.json
    in compatibility mode (ADR-0128 D6); natively run.json keeps the turn's own status."""
    import json
    import time
    from types import SimpleNamespace

    import anyio

    import lionagi.cli._runs as runs_mod
    from lionagi.state.compat import Closure
    from lionagi.studio.operator.coordinator import OperatorCoordinator
    from lionagi.studio.operator.store import OperatorStore
    from lionagi.studio.operator.types import OperatorEngineEvent

    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(runs_mod, "RUNS_ROOT", tmp_path / "runs")

    profile = (
        CompatibilityProfile(
            name="operator-records",
            version=1,
            closure=Closure(tables=()),
            writes={},
            operations={"run"},
        )
        if compatibility == "profile"
        else None
    )

    async def setup(*args, **kwargs):
        return {"session_id": "s1", "db": SimpleNamespace(compatibility=profile)}

    async def teardown(ctx, *, status="completed", **kwargs):
        return "failed"

    monkeypatch.setattr(runs_mod, "setup_agent_persist", setup)
    monkeypatch.setattr(runs_mod, "teardown_agent_persist", teardown)

    class _Engine:
        async def _stream(self, _turn):
            yield OperatorEngineEvent(
                "text", {"content": "reply", "format": "plain", "role": "assistant"}
            )

        def stream(self, turn):
            return self._stream(turn)

    coordinator = OperatorCoordinator(
        store=OperatorStore(tmp_path / "state.db"), engine_factory=_Engine
    )
    await coordinator.startup()
    try:
        cid = (await coordinator.create_conversation())["conversation"]["id"]
        await coordinator.submit(
            cid,
            instruction="hello",
            context={"space": "mission", "route": "/", "filters": {}},
            expected_last_sequence=0,
        )
        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline:
            manifests = list((tmp_path / "runs").glob("operator-*/run.json"))
            if manifests:
                status = json.loads(manifests[0].read_text()).get("status")
                if status != "running":
                    break
            await anyio.sleep(0.02)
    finally:
        await coordinator.shutdown()

    assert status == recorded


class _DriverWrite(StateDB):
    """Runs one statement with its parameters exactly as given, never re-spelled by the driver."""

    @_operation("create_progression")
    async def create_progression(self, progression_id: str, collection=None) -> None:
        async with self._tx() as conn:
            await conn.exec_driver_sql(self.statement, self.params)


async def _driver_write(store: Path, statement: str, params) -> None:
    handle = _DriverWrite(store, compatibility=_profile(store), owner="principal:a")
    handle.statement, handle.params = statement, params
    await handle.open()
    try:
        await handle.create_progression("p-probe")
    finally:
        await handle.close()


_NAMED_INSERT = "INSERT INTO progressions (id, created_at, collection, owner_principal_id) "


@pytest.mark.parametrize(
    "named", ["#id", ":id", "@id", "$id", "?1"], ids=["hash", "colon", "at", "dollar", "numbered"]
)
async def test_an_insert_whose_owner_slot_follows_a_named_parameter_is_refused(store, named):
    """SQLite numbers a named parameter like ``?``, so the ``?`` slot is not the first argument."""
    _foreign_rows(store)
    before = _rows(store, f"SELECT id, {OWNER} FROM progressions ORDER BY id")

    with pytest.raises(StateCompatibilityRefused, match="binds its values in a form"):
        await _driver_write(
            store, _NAMED_INSERT + f"VALUES ({named}, 1.0, '[]', ?)", ("principal:a", "principal:b")
        )

    assert _rows(store, f"SELECT id, {OWNER} FROM progressions ORDER BY id") == before
    assert before == [("p-other", "principal:b")]


async def test_an_update_whose_owner_term_follows_a_named_parameter_is_refused(store):
    """The ``?`` after ``#c`` is the second argument, not the first the guard reads it as."""
    _foreign_rows(store)
    before = _rows(store, f"SELECT id, content, {OWNER} FROM messages ORDER BY id")

    with pytest.raises(StateCompatibilityRefused, match="binds its values in a form"):
        await _driver_write(
            store,
            "UPDATE messages SET content = #c WHERE owner_principal_id = ?",
            ("principal:a", "principal:b"),
        )

    assert _rows(store, f"SELECT id, content, {OWNER} FROM messages ORDER BY id") == before
    assert before == [("m-other", "{}", "principal:b")]


async def test_an_update_that_binds_a_column_other_than_the_owner_is_refused(store):
    """A WHERE term on another column, even bound to the owner, does not restrict the owner."""
    _first_owned_message(store)

    with pytest.raises(StateCompatibilityRefused, match="does not restrict its rows"):
        await _driver_write(
            store, "UPDATE messages SET content = ? WHERE sender = ?", ("taken", "principal:a")
        )

    assert _rows(store, "SELECT content FROM messages WHERE id = 'm-sys'") == [("first",)]


async def test_an_executemany_whose_later_row_writes_another_owner_is_refused(store):
    with pytest.raises(StateCompatibilityRefused, match="other than the owner"):
        await _driver_write(
            store,
            _NAMED_INSERT + "VALUES (?, 1.0, '[]', ?)",
            [("p1", "principal:a"), ("p2", "principal:b")],
        )

    assert _rows(store, "SELECT COUNT(*) FROM progressions") == [(0,)]


async def test_an_executemany_bound_to_the_owner_in_every_row_is_admitted(store):
    await _driver_write(
        store,
        _NAMED_INSERT + "VALUES (?, 1.0, '[]', ?)",
        [("p1", "principal:a"), ("p2", "principal:a")],
    )

    assert _rows(store, f"SELECT id, {OWNER} FROM progressions ORDER BY id") == [
        ("p1", "principal:a"),
        ("p2", "principal:a"),
    ]


def _first_owned_message(store: Path) -> None:
    _execute(
        store,
        f"INSERT INTO messages (id, created_at, content, role, lion_class, {OWNER}) "
        "VALUES ('m-sys', 0, 'first', 'system', 1, 'principal:a')",
    )


async def test_a_trigger_named_begin_is_read_from_the_begin_after_its_table(tmp_path):
    """The trigger's name is the BEGIN keyword, so the first BEGIN is not the body's."""
    store = await _managed_store(tmp_path / "managed.db")
    _execute(
        store,
        "CREATE TRIGGER begin AFTER UPDATE ON messages BEGIN "
        "INSERT INTO progressions (id, created_at, collection) VALUES (NEW.id || '-x', 1.0, '[]'); "
        "END",
    )
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")

    with pytest.raises(StateCompatibilityRefused, match="trigger begin"):
        await handle.open()
    assert handle._engine is None


async def test_a_trigger_named_begin_that_copies_the_owner_is_admitted(tmp_path):
    store = await _managed_store(tmp_path / "managed.db")
    _execute(
        store,
        "CREATE TRIGGER begin AFTER UPDATE ON messages BEGIN "
        f"INSERT INTO progressions (id, created_at, collection, {OWNER}) "
        f"VALUES (NEW.id || '-x', 1.0, '[]', NEW.{OWNER}); END",
    )
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")
    await handle.open()
    await handle.close()


async def test_a_trigger_that_reads_old_in_an_insert_is_refused(tmp_path):
    """An INSERT trigger has no OLD row, so a body that reads ``OLD.<column>`` names no column
    of the trigger at all and the insert that fires it fails."""
    store = await _managed_store(tmp_path / "managed.db", owner_sql="TEXT")
    _execute(
        store,
        "CREATE TRIGGER copy_old AFTER INSERT ON messages BEGIN "
        f"INSERT INTO progressions (id, created_at, collection, {OWNER}) "
        f"VALUES (NEW.id || '-p', 1.0, '[]', OLD.{OWNER}); END",
    )
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")

    with pytest.raises(StateCompatibilityRefused, match="trigger copy_old"):
        await handle.open()
    assert handle._engine is None


async def test_a_trigger_that_reads_new_in_a_delete_is_refused(tmp_path):
    """A DELETE trigger has no NEW row, so a body that reads ``NEW.<column>`` names no column of
    the trigger at all and copies nothing."""
    store = await _managed_store(tmp_path / "managed.db", owner_sql="TEXT")
    _execute(
        store,
        "CREATE TRIGGER copy_new AFTER DELETE ON messages BEGIN "
        f"INSERT INTO progressions (id, created_at, collection, {OWNER}) "
        f"VALUES (OLD.id || '-p', 1.0, '[]', NEW.{OWNER}); END",
    )
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")

    with pytest.raises(StateCompatibilityRefused, match="trigger copy_new"):
        await handle.open()
    assert handle._engine is None


_NOCASE_OWNER = "TEXT NOT NULL DEFAULT 'principal:default' COLLATE NOCASE"


async def test_an_owner_column_that_does_not_compare_under_binary_is_refused(tmp_path):
    """A NOCASE owner compares a case variant as the owner, so the WHERE term does not hold."""
    store = await _managed_store(tmp_path / "managed.db", owner_sql=_NOCASE_OWNER)
    before = _rows(store, "SELECT COUNT(*) FROM messages")
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")

    with pytest.raises(StateCompatibilityRefused, match="nocase") as caught:
        await handle.open()

    assert "collation" in caught.value.reason.lower()
    assert handle._engine is None
    assert _rows(store, "SELECT COUNT(*) FROM messages") == before


async def test_an_owner_column_that_compares_under_binary_is_admitted(tmp_path):
    store = await _managed_store(tmp_path / "managed.db", owner_sql="TEXT COLLATE BINARY")
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")
    await handle.open()
    await handle.close()


async def test_a_trigger_whose_owner_term_is_reversed_is_admitted(tmp_path):
    store = await _managed_store(tmp_path / "managed.db")
    _execute(
        store,
        "CREATE TRIGGER rev AFTER INSERT ON messages BEGIN "
        "UPDATE progressions SET collection = '[]' WHERE id = NEW.sender "
        f"AND NEW.{OWNER} = {OWNER}; END",
    )
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")
    await handle.open()
    await handle.close()


async def test_a_trigger_whose_owner_term_is_parenthesised_is_admitted(tmp_path):
    store = await _managed_store(tmp_path / "managed.db")
    _execute(
        store,
        "CREATE TRIGGER par AFTER INSERT ON messages BEGIN "
        "UPDATE progressions SET collection = '[]' WHERE id = NEW.sender "
        f"AND ({OWNER} = NEW.{OWNER}); END",
    )
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")
    await handle.open()
    await handle.close()


_COLLATION_DEFINITIONS = [
    ("TEXT", "binary", "no clause"),
    ("TEXT COLLATE NOCASE", "nocase", "own clause"),
    ("TEXT COLLATE RTRIM", "rtrim", "another collation"),
    (
        "TEXT CHECK (col COLLATE BINARY <> '') COLLATE NOCASE",
        "nocase",
        "a CHECK before the clause",
    ),
    ("TEXT CHECK (col COLLATE NOCASE <> '')", "binary", "a CHECK and no clause of its own"),
    ("TEXT CHECK (col COLLATE RTRIM <> '')", "binary", "a CHECK of a third collation"),
    ("TEXT COLLATE NOCASE COLLATE RTRIM", "rtrim", "two clauses, NOCASE then RTRIM"),
    ("TEXT COLLATE RTRIM COLLATE NOCASE", "nocase", "two clauses, RTRIM then NOCASE"),
    ('TEXT COLLATE "NOCASE"', "nocase", "a quoted name"),
    ("TEXT collate nocase", "nocase", "a lower-case name"),
    ("TEXT Collate NoCase", "nocase", "a mixed-case name"),
    ("TEXT COLLATE RTRIM COLLATE RTRIM", "rtrim", "the same clause twice"),
]


def _collation_sqlite_applied(definition: str) -> str:
    """The collation SQLite compares a column declared *definition* under, measured.

    The column takes the value ``'a'``; a match for ``'A'`` is NOCASE's case folding and a match
    for ``'a '`` is RTRIM's folding of trailing spaces, neither of which binary has. Neither
    reaches a body comparison through :func:`_fold` for some other collation, which is why the
    answer is the folded name of the collation the column declares.
    """
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute(f"CREATE TABLE t (col {definition})")
        conn.execute("INSERT INTO t VALUES ('a')")
        folded = conn.execute("SELECT COUNT(*) FROM t WHERE col = 'A'").fetchone()[0]
        trimmed = conn.execute("SELECT COUNT(*) FROM t WHERE col = 'a '").fetchone()[0]
    if folded:
        return "nocase"
    if trimmed:
        return "rtrim"
    return "binary"


@pytest.mark.parametrize(
    ("definition", "expected", "what"),
    _COLLATION_DEFINITIONS,
    ids=[what for _, _, what in _COLLATION_DEFINITIONS],
)
def test_a_column_collation_is_read_from_the_clause_sqlite_uses(definition, expected, what):
    """SQLite takes the collation of a column's own clause, the last one when it declares
    several, and a COLLATE inside a CHECK or a DEFAULT is part of that expression, not the
    column's."""
    measured = _collation_sqlite_applied(definition)
    assert measured == expected, f"{what}: SQLite applied {measured}"
    assert _column_collation(definition) == expected


def test_column_collation_folds_the_name_as_sqlite_folds_it():
    """``_fold`` maps ASCII letters only, so the dotless ``ı`` is not the ASCII ``i`` and the
    name it appears in is not the folded ``binary``."""
    assert _column_collation("TEXT COLLATE bınary") == "bınary"
    assert _column_collation("TEXT COLLATE BINARY") == "binary"
    assert _column_collation("TEXT COLLATE NOCASE") == "nocase"
    assert _column_collation("TEXT COLLATE Bınary") == "bınary"
    assert _column_collation("TEXT CHECK (col COLLATE BINARY <> '') COLLATE NOCASE") == "nocase"


async def test_an_owner_column_whose_check_names_another_collation_is_refused(tmp_path):
    """A CHECK's COLLATE is not the column's, so the clause after it still governs."""
    store = await _managed_store(
        tmp_path / "managed.db",
        owner_sql="TEXT NOT NULL CHECK (owner_principal_id COLLATE BINARY <> '') COLLATE NOCASE",
    )
    before = _rows(store, "SELECT COUNT(*) FROM messages")
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")

    with pytest.raises(StateCompatibilityRefused, match="nocase") as caught:
        await handle.open()

    assert "collation" in caught.value.reason.lower()
    assert handle._engine is None
    assert _rows(store, "SELECT COUNT(*) FROM messages") == before


_OWNER_COLUMN_SHAPES = [
    ("c TEXT COLLATE NOCASE", "c", "nocase", "a type first"),
    ("c PRIMARY KEY COLLATE NOCASE", "c", "nocase", "no type, PRIMARY KEY first"),
    ("c UNIQUE COLLATE NOCASE", "c", "nocase", "no type, UNIQUE first"),
    ("c CHECK (c <> '') COLLATE NOCASE", "c", "nocase", "no type, CHECK first"),
    ("c CONSTRAINT k NOT NULL COLLATE NOCASE", "c", "nocase", "no type, a named constraint first"),
    ("c PRIMARY KEY COLLATE RTRIM", "c", "rtrim", "no type, another collation"),
    ("c PRIMARY KEY", "c", "binary", "no type and no clause"),
    ("'c' TEXT COLLATE NOCASE", "c", "nocase", "a name written as a string"),
    ('"C" TEXT COLLATE NOCASE', "c", "nocase", "a quoted name in another case"),
    ("d TEXT, [check] TEXT COLLATE NOCASE, CHECK (d <> '')", "check", "nocase", "named CHECK"),
    ('d TEXT, "unique" TEXT COLLATE NOCASE, UNIQUE (d)', "unique", "nocase", "named UNIQUE"),
    ('d TEXT, "primary" COLLATE NOCASE, PRIMARY KEY (d)', "primary", "nocase", "named PRIMARY"),
    (
        "d TEXT, \"constraint\" TEXT COLLATE NOCASE, CONSTRAINT k CHECK (d <> '')",
        "constraint",
        "nocase",
        "named CONSTRAINT",
    ),
    ('"c a" TEXT, c TEXT COLLATE NOCASE', "c", "nocase", "a sibling named with the name and more"),
    ('"c a" TEXT, c TEXT COLLATE NOCASE', "c a", "binary", "that sibling itself"),
    ('"c a" TEXT COLLATE NOCASE, c TEXT', "c", "binary", "a sibling of another collation"),
    ('"primary" KEY(10)', "primary", "binary", "named PRIMARY, a type spelled KEY"),
    ('"foreign" KEY(10)', "foreign", "binary", "named FOREIGN, a type spelled KEY"),
    ('"constraint" TEXT CHECK (1)', "constraint", "binary", "named CONSTRAINT, then a CHECK"),
    ('"c d" TEXT', "c d", "binary", "a name with a space"),
    ('"collate" TEXT', "collate", "binary", "named COLLATE"),
    ('"collate" TEXT COLLATE NOCASE', "collate", "nocase", "named COLLATE, with a clause"),
]


def test_an_owner_column_whose_definition_cannot_be_read_is_not_taken_as_binary():
    """A column the lookup cannot find a definition for reads as no collation at all, never as
    binary."""
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE t (c TEXT COLLATE NOCASE)")
        table = next(shape for shape in read_closure(conn, ["t"]).tables if shape.name == "t")

    assert _ordered_collation(table, "c") == "nocase"
    assert _ordered_collation(table, "absent") is None


async def test_an_owner_column_whose_collation_cannot_be_read_is_refused_as_unread(
    tmp_path, monkeypatch
):
    """An owner column whose collation the table's SQL does not show is refused, and the refusal
    says the collation could not be read rather than naming one."""
    store = await _managed_store(tmp_path / "managed.db", owner_sql="TEXT")
    profile = _profile(store)
    monkeypatch.setattr(compat, "_ordered_collation", lambda table, column: None)
    handle = StateDB(store, compatibility=profile, owner="principal:a")

    with pytest.raises(StateCompatibilityRefused) as caught:
        await handle.open()

    assert "could not be read from the table's SQL" in caught.value.reason
    assert f"messages.{OWNER}" in caught.value.reason
    assert "compares under" not in caught.value.reason
    assert handle._engine is None


@pytest.mark.parametrize(
    ("body", "column", "expected", "what"),
    _OWNER_COLUMN_SHAPES,
    ids=[what for *_, what in _OWNER_COLUMN_SHAPES],
)
def test_the_owner_column_collation_is_read_however_its_definition_starts(
    body, column, expected, what
):
    """The lookup finds the column's own definition whether a type, a constraint or nothing
    follows its name, never reads a table constraint whose first word is the column's name as
    that column, and never reads a quoted name as a keyword or as the start of a longer name."""
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute(f"CREATE TABLE t ({body})")
        conn.execute(f"INSERT INTO t (\"{column}\") VALUES ('a')")
        folded = conn.execute(f"SELECT COUNT(*) FROM t WHERE \"{column}\" = 'A'").fetchone()[0]
        trimmed = conn.execute(f"SELECT COUNT(*) FROM t WHERE \"{column}\" = 'a '").fetchone()[0]
        table = next(shape for shape in read_closure(conn, ["t"]).tables if shape.name == "t")
    measured = "nocase" if folded else "rtrim" if trimmed else "binary"
    assert measured == expected, f"{what}: SQLite applied {measured}"
    assert _ordered_collation(table, column) == expected


_NOCASE_OWNER_SHAPES = [
    (
        OWNER,
        "CHECK (owner_principal_id <> '') NOT NULL DEFAULT 'principal:default' COLLATE NOCASE",
        "no type, CHECK first",
    ),
    (
        OWNER,
        "CONSTRAINT k NOT NULL DEFAULT 'principal:default' COLLATE NOCASE",
        "no type, a named constraint first",
    ),
    (f"'{OWNER}'", _NOCASE_OWNER, "a name written as a string"),
]


@pytest.mark.parametrize(
    ("owner_name", "owner_sql", "what"),
    _NOCASE_OWNER_SHAPES,
    ids=[what for *_, what in _NOCASE_OWNER_SHAPES],
)
async def test_a_nocase_owner_column_is_refused_however_its_definition_starts(
    tmp_path, owner_name, owner_sql, what
):
    store = await _managed_store(
        tmp_path / "managed.db", owner_sql=owner_sql, owner_name=owner_name
    )
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")

    with pytest.raises(StateCompatibilityRefused, match="nocase") as caught:
        await handle.open()

    assert "collation" in caught.value.reason.lower()
    assert handle._engine is None


def _nocase_principals(store: Path) -> None:
    """A principals key that compares case-insensitively, holding only the lower-case spelling."""
    with closing(sqlite3.connect(store)) as conn:
        conn.executescript(
            "DROP TABLE principals;"
            "CREATE TABLE principals (id TEXT PRIMARY KEY COLLATE NOCASE, kind TEXT NOT NULL);"
            "INSERT INTO principals VALUES ('principal:a', 'human');"
        )
        conn.commit()


async def test_an_owner_that_another_case_spelling_names_is_refused(tmp_path):
    """The principals key compares under NOCASE, so ``PRINCIPAL:A`` finds ``principal:a`` there
    while the owner predicate, which is BINARY, then treats the row it wrote as another
    owner's."""
    store = await _managed_store(tmp_path / "managed.db")
    _nocase_principals(store)
    handle = StateDB(store, compatibility=_profile(store), owner="PRINCIPAL:A")

    with pytest.raises(StateCompatibilityRefused, match="not in principals") as caught:
        await handle.open()

    assert handle._engine is None
    assert caught.value.operation == "open"


async def test_an_owner_the_nocase_principals_key_names_exactly_still_opens(tmp_path):
    store = await _managed_store(tmp_path / "managed.db")
    _nocase_principals(store)
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")
    await handle.open()
    try:
        await handle.create_progression("sp")
    finally:
        await handle.close()

    assert _rows(store, f"SELECT id, {OWNER} FROM progressions") == [("sp", "principal:a")]


async def test_an_insert_that_names_a_column_twice_is_refused(store):
    """Two slots for one column leave the value SQLite writes to the order it takes them in."""
    _foreign_rows(store)
    before = _rows(store, f"SELECT id, {OWNER} FROM progressions ORDER BY id")

    message = "names the column owner_principal_id twice"
    with pytest.raises(StateCompatibilityRefused, match=message):
        await _raw_write(
            store,
            _PROGRESSION[:-2] + f", {OWNER}) VALUES (:id, 1.0, '[]', 'principal:b', 'principal:a')",
            {},
        )

    assert _rows(store, f"SELECT id, {OWNER} FROM progressions ORDER BY id") == before


async def test_an_insert_that_names_a_non_owner_column_twice_is_refused(store):
    _foreign_rows(store)
    before = _rows(store, f"SELECT id, {OWNER} FROM progressions ORDER BY id")

    with pytest.raises(StateCompatibilityRefused, match="names the column id twice"):
        await _raw_write(
            store,
            "INSERT INTO progressions (id, id, created_at, collection, "
            f"{OWNER}) VALUES (:id, 'x', 1.0, '[]', 'principal:a')",
            {},
        )

    assert _rows(store, f"SELECT id, {OWNER} FROM progressions ORDER BY id") == before


async def test_an_insert_into_an_owned_table_that_names_the_owner_twice_is_refused(store):
    """SQLite takes the first value bound for a column it names twice and raises no error, so
    the slot the guard reads as the owner need not be the one the row is written with."""
    _foreign_rows(store)
    before = _rows(store, f"SELECT id, {OWNER} FROM progressions ORDER BY id")

    with pytest.raises(StateCompatibilityRefused, match="the INSERT names the column"):
        await _raw_write(
            store,
            _PROGRESSION[:-2] + f", {OWNER}) VALUES (:id, 1.0, '[]', :o, :o)",
            {"o": "principal:a"},
        )

    assert _rows(store, f"SELECT id, {OWNER} FROM progressions ORDER BY id") == before


async def test_an_insert_that_names_each_column_once_is_admitted(store):
    await _raw_write(store, _PROGRESSION + "VALUES (:id, 1.0, '[]', :o)", {"o": "principal:a"})

    assert _rows(store, f"SELECT id, {OWNER} FROM progressions") == [("p1", "principal:a")]


async def test_a_trigger_that_deletes_from_a_closure_table_is_refused(tmp_path):
    """A trigger's DELETE runs inside the write that fires it and reaches the authorizer as no
    admitted action, so admission refuses it."""
    store = await _managed_store(tmp_path / "managed.db")
    _execute(
        store,
        "CREATE TRIGGER sweep AFTER INSERT ON messages BEGIN "
        "DELETE FROM progressions WHERE id = NEW.id; END",
    )
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")

    with pytest.raises(StateCompatibilityRefused, match="trigger sweep") as caught:
        await handle.open()

    assert "DELETE FROM progressions" in caught.value.reason
    assert handle._engine is None


@pytest.mark.parametrize(
    ("body", "names"),
    [
        ("DELETE FROM progressions WHERE id = NEW.id", "DELETE FROM progressions"),
        (
            "DELETE FROM message_types WHERE lion_class = NEW.lion_class",
            "DELETE FROM message_types",
        ),
        ("DELETE FROM 'progressions'", "DELETE FROM progressions"),
        ("DELETE FROM [progressions]", "DELETE FROM progressions"),
        (
            "DELETE FROM progressions WHERE id IN (SELECT id FROM progressions)",
            "DELETE FROM progressions",
        ),
    ],
    ids=["closure table", "another table", "a string name", "a bracketed name", "a subquery"],
)
async def test_a_trigger_that_deletes_is_refused_whatever_it_deletes(tmp_path, body, names):
    """The authorizer refuses every DELETE on a compatibility connection, so a trigger that
    runs one never survives the write that fires it."""
    store = await _managed_store(tmp_path / "managed.db")
    _execute(store, f"CREATE TRIGGER sweep AFTER INSERT ON messages BEGIN {body}; END")
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")

    with pytest.raises(StateCompatibilityRefused, match="trigger sweep") as caught:
        await handle.open()

    assert names in caught.value.reason
    assert handle._engine is None


def test_what_sqlite_makes_of_a_multi_event_trigger_and_of_new_and_old():
    """A trigger names one event, and the row a statement makes readable is the one its event
    carries: a DELETE trigger reads OLD and an INSERT trigger reads NEW, while the other is not
    a column at all. This pins SQLite's behaviour, the premise of the trigger reader;
    test_a_delete_trigger_is_held_to_old_and_only_old holds the reader to it."""
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE t (id TEXT PRIMARY KEY, col TEXT)")
        conn.execute("CREATE TABLE log (v TEXT)")
        with pytest.raises(sqlite3.OperationalError, match="syntax error"):
            conn.execute(
                "CREATE TRIGGER m AFTER DELETE OR INSERT ON t BEGIN "
                "INSERT INTO log VALUES (NEW.col); END"
            )

    def fired(sql: str, statements: list[str]) -> tuple:
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("CREATE TABLE t (id TEXT PRIMARY KEY, col TEXT)")
            conn.execute("CREATE TABLE log (col TEXT)")
            conn.execute(sql)
            for statement in statements:
                try:
                    conn.execute(statement)
                except sqlite3.Error as exc:
                    return ("error", str(exc))
            return ("rows", conn.execute("SELECT col FROM log").fetchall())

    deleted = ["INSERT INTO t VALUES ('1', 'a')", "DELETE FROM t WHERE id = '1'"]
    inserted = ["INSERT INTO t VALUES ('1', 'a')"]
    cases = [
        ("DELETE", "NEW", deleted, ("error", "no such column: NEW.col")),
        ("INSERT", "OLD", inserted, ("error", "no such column: OLD.col")),
        ("DELETE", "OLD", deleted, ("rows", [("a",)])),
        ("INSERT", "NEW", inserted, ("rows", [("a",)])),
    ]
    for event, row, statements, outcome in cases:
        sql = (
            f"CREATE TRIGGER x AFTER {event} ON t BEGIN "
            f"INSERT INTO log (col) VALUES ({row}.col); END"
        )
        assert fired(sql, statements) == outcome, (event, row)
        # The reader admits exactly the copies SQLite fills with the row that fired the trigger.
        admitted = _trigger_owner_problems(sql, "col", frozenset({"log"})) == []
        assert admitted == (outcome[0] == "rows"), (event, row)


_DELETE_SWEEP = (
    "CREATE TRIGGER sweep AFTER DELETE ON messages BEGIN "
    f"UPDATE progressions AS d SET collection = '[]' WHERE {OWNER} = d.{OWNER}; END"
)


def test_a_delete_trigger_is_held_to_old_and_only_old():
    """In a DELETE trigger only OLD is the row that fired it; a table alias that shares a letter
    with it is another row."""
    owned = frozenset({"progressions"})
    copies_old = (
        "CREATE TRIGGER d AFTER DELETE ON messages BEGIN "
        f"UPDATE progressions SET collection = '[]' WHERE {OWNER} = OLD.{OWNER}; END"
    )

    assert _trigger_owner_problems(copies_old, OWNER, owned) == []
    problems = _trigger_owner_problems(_DELETE_SWEEP, OWNER, owned)
    assert problems == [
        f"updates progressions with an owner other than OLD.{OWNER}, the owner of the row that "
        "fired it"
    ]


# The same sweep without the alias: the owner compared with the updated row's own column.
_DELETE_SWEEP_BY_NAME = (
    "CREATE TRIGGER sweep AFTER DELETE ON messages BEGIN "
    f"UPDATE progressions SET collection = '[]' WHERE {OWNER} = progressions.{OWNER}; END"
)


def _sqlite_aliases_a_trigger_update() -> bool:
    """Whether this SQLite accepts ``UPDATE t AS a`` in a trigger body.

    Older releases refuse the alias there, so no store they open can hold such a trigger.
    """
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute(f"CREATE TABLE messages ({OWNER} TEXT)")
        conn.execute(f"CREATE TABLE progressions ({OWNER} TEXT, collection TEXT)")
        try:
            conn.execute(_DELETE_SWEEP)
        except sqlite3.OperationalError:
            return False
    return True


@pytest.mark.parametrize(
    "trigger",
    [
        _DELETE_SWEEP_BY_NAME,
        pytest.param(
            _DELETE_SWEEP,
            marks=pytest.mark.skipif(
                not _sqlite_aliases_a_trigger_update(),
                reason=f"SQLite {sqlite3.sqlite_version} refuses a table alias in a trigger's UPDATE",
            ),
        ),
    ],
    ids=["table-name", "alias"],
)
async def test_a_delete_trigger_comparing_the_owner_with_the_updated_row_is_refused(
    tmp_path, trigger
):
    store = await _managed_store(tmp_path / "managed.db")
    _execute(store, trigger)
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")

    with pytest.raises(StateCompatibilityRefused, match="trigger sweep") as caught:
        await handle.open()

    assert f"OLD.{OWNER}" in caught.value.reason
    assert handle._engine is None


_ALIASED_UPDATES = [
    ("INSERT", "new"),
    ("UPDATE", "old"),
    ("UPDATE", "new"),
    ("DELETE", "old"),
    ("INSERT", '"NEW"'),
    ("INSERT", "[new]"),
    ("DELETE", "'old'"),
]


@pytest.mark.parametrize(("event", "alias"), _ALIASED_UPDATES)
def test_a_trigger_update_under_the_alias_new_or_old_is_refused(event, alias):
    """``UPDATE t AS new`` names the updated row NEW inside the statement, so a term comparing
    the owner with NEW.<column> compares each row with itself and holds for every owner's row;
    OLD likewise."""
    sql = (
        f"CREATE TRIGGER t AFTER {event} ON messages BEGIN "
        f"UPDATE messages AS {alias} SET content = '' WHERE {OWNER} = {alias}.{OWNER}; END"
    )
    shown = alias.strip("\"'[]").upper()

    assert _trigger_owner_problems(sql, OWNER, frozenset({"messages"})) == [
        f"updates messages under the alias {shown}, which hides the row that fired it"
    ]


@pytest.mark.skipif(
    not _sqlite_aliases_a_trigger_update(),
    reason=f"SQLite {sqlite3.sqlite_version} refuses a table alias in a trigger's UPDATE",
)
async def test_a_store_whose_trigger_updates_under_the_alias_new_is_refused(tmp_path):
    """Run on its own, the trigger rewrites every owner's message when one owner writes one."""
    store = await _managed_store(tmp_path / "managed.db")
    _execute(
        store,
        "CREATE TRIGGER blank AFTER INSERT ON messages BEGIN "
        f"UPDATE messages AS new SET content = '\"\"' WHERE {OWNER} = new.{OWNER}; END",
    )
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")

    with pytest.raises(StateCompatibilityRefused, match="trigger blank") as caught:
        await handle.open()

    assert "under the alias NEW" in caught.value.reason
    assert handle._engine is None


@pytest.mark.parametrize("column", ["before", "after", "instead", '"delete"', '"update"'])
def test_the_event_of_an_update_of_trigger_is_read_whatever_its_column_is_named(column):
    """BEFORE, AFTER and INSTEAD name a column bare; the event word before them is the event."""
    sql = f"CREATE TRIGGER t AFTER UPDATE OF {column} ON m BEGIN SELECT 1; END"
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute(f"CREATE TABLE m (id TEXT, {column} TEXT)")
        conn.execute(sql)

    assert _trigger_event(sql) == "update"


@pytest.mark.parametrize(
    ("sql", "event"),
    [
        ("CREATE TRIGGER after AFTER INSERT ON m BEGIN SELECT 1; END", "insert"),
        ("CREATE TRIGGER before BEFORE DELETE ON m BEGIN SELECT 1; END", "delete"),
        ('CREATE TRIGGER "insert" INSTEAD OF UPDATE ON v BEGIN SELECT 1; END', "update"),
        ("CREATE TRIGGER 'delete' AFTER INSERT ON m BEGIN SELECT 1; END", "insert"),
        ("CREATE TRIGGER t UPDATE ON m BEGIN SELECT 1; END", "update"),
    ],
)
def test_the_event_of_a_trigger_is_not_read_from_its_name(sql, event):
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE m (id TEXT)")
        conn.execute("CREATE VIEW v AS SELECT id FROM m")
        conn.execute(sql)

    assert _trigger_event(sql) == event


_NEW_OWNER_WRITE = (
    "INSERT INTO progressions (id, created_at, collection, " + OWNER + ") "
    f"VALUES (NEW.id, 1.0, '[]', NEW.{OWNER})"
)
_OTHER_OWNER_WRITE = (
    "INSERT INTO progressions (id, created_at, collection, " + OWNER + ") "
    "VALUES (NEW.id, 1.0, '[]', 'principal:b')"
)


_KEYWORD_NAMES = ["begin", "insert", "update", "delete", "after", "on"]
# SQLite reads a bare name only for some of these; the others have to be quoted to be one.
_BARE_KEYWORD_NAMES = ["begin", "after", "before"]


@pytest.mark.parametrize("name", _KEYWORD_NAMES + _BARE_KEYWORD_NAMES)
async def test_a_trigger_the_keyword_it_is_named_after_is_read_from_its_own_event(tmp_path, name):
    """The trigger's name is a keyword, so the reader takes the event word after the name and
    its timing, never the first one in the statement, which is the name here."""
    store = await _managed_store(tmp_path / "managed.db")
    quoted = name in _KEYWORD_NAMES
    spelled = f'"{name}"' if quoted else name
    _execute(
        store,
        f"CREATE TRIGGER {spelled} AFTER INSERT ON messages BEGIN {_NEW_OWNER_WRITE}; END",
    )
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")
    await handle.open()
    try:
        await handle.insert_message(_system_message())
    finally:
        await handle.close()

    assert _rows(store, f"SELECT id, {OWNER} FROM progressions") == [("m-sys", "principal:a")]


@pytest.mark.parametrize("name", _KEYWORD_NAMES)
async def test_a_trigger_the_keyword_it_is_named_after_writes_the_owner_from_new(tmp_path, name):
    store = await _managed_store(tmp_path / "managed.db")
    _execute(
        store,
        f'CREATE TRIGGER "{name}" AFTER INSERT ON messages BEGIN {_OTHER_OWNER_WRITE}; END',
    )
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")

    with pytest.raises(StateCompatibilityRefused, match=f"trigger {name}"):
        await handle.open()

    assert handle._engine is None


@pytest.mark.parametrize("name", _KEYWORD_NAMES)
async def test_a_trigger_named_after_a_keyword_is_admitted_when_it_copies_the_owner(tmp_path, name):
    store = await _managed_store(tmp_path / "managed.db")
    _execute(
        store,
        f'CREATE TRIGGER "{name}" AFTER INSERT ON messages BEGIN '
        f"UPDATE progressions SET collection = '[]' WHERE id = NEW.id "
        f"AND {OWNER} = NEW.{OWNER}; END",
    )
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")
    await handle.open()
    await handle.close()


async def test_two_opens_of_one_handle_at_once_build_one_engine(tmp_path, monkeypatch):
    """The store is read in a worker thread before the engine exists, so the second open waits for
    the first and finds its engine."""
    import asyncio

    from lionagi.state import db as db_mod

    built = []
    make_engine = db_mod.make_engine

    def counting(url, **kwargs):
        engine = make_engine(url, **kwargs)
        built.append(engine)
        return engine

    monkeypatch.setattr(db_mod, "make_engine", counting)
    handle = StateDB(tmp_path / "state.db")
    try:
        await asyncio.gather(handle.open(), handle.open())
        assert len(built) == 1
        assert handle._engine is built[0]
    finally:
        await handle.close()


def test_the_read_only_uri_ends_the_file_name_where_sqlite_does():
    """SQLite reads nothing after a `#` in a URI file name, so the read-only parameters go before
    it, on the file SQLAlchemy's connection opens."""
    from lionagi.state.db import _read_only_uri

    assert _read_only_uri("file:/stores/a.db#part?mode=rw", True, immutable=True) == (
        "file:/stores/a.db?immutable=1"
    )
    assert _read_only_uri("file:/stores/a.db?vfs=unix#part", True, immutable=False) == (
        "file:/stores/a.db?vfs=unix&mode=ro&readonly_shm=1"
    )


def _profile_that_admits_agent_setup(store: Path):
    """The test profile admitting the writes a fresh agent setup makes, branch provenance included."""
    from lionagi.state.compat import CompatibilityProfile, TableWrites

    base = _profile(store)
    writes = dict(base.writes)
    sessions = writes["sessions"]
    writes["sessions"] = TableWrites(
        insert=sessions.insert,
        update=sessions.update
        | {
            "started_at",
            "model",
            "provider",
            "effort",
            "agent_name",
            "agent_hash",
            "invocation_id",
        },
        unwritten=sessions.unwritten,
    )
    branches = writes["branches"]
    writes["branches"] = TableWrites(
        insert=branches.insert,
        update=branches.update | {"model", "provider", "agent_name"},
        unwritten=branches.unwritten,
    )
    return CompatibilityProfile(
        name=base.name,
        version=base.version,
        closure=base.closure,
        writes=writes,
        operations=base.operations,
        owner=base.owner,
    )


def _record_shared_store_lookups(monkeypatch) -> list:
    """Every lookup of the process-wide store, recorded rather than raised: the hook bus logs
    and drops most handler errors, so only a record shows the lookup happened."""
    from lionagi.state import db as db_mod

    lookups: list = []

    async def lookup(*args, **kwargs):
        lookups.append((args, kwargs))
        raise RuntimeError("agent setup reached the process-wide store")

    monkeypatch.setattr(db_mod, "get_shared_db", lookup)
    return lookups


@pytest.mark.parametrize("share_db", [True, False], ids=["shared", "not-shared"])
async def test_agent_setup_completes_on_a_compatibility_store(tmp_path, monkeypatch, share_db):
    """A fresh agent run's setup is served by the base profile: the session and branch rows are
    written through the run's own handle, and the setup handlers, handed that handle, reach no
    other store and write nothing more."""
    from lionagi.cli import _runs
    from lionagi.session.branch import Branch

    store = await _managed_store(tmp_path / "managed.db")
    handle = StateDB(store, compatibility=_profile(store), owner="principal:a")
    await handle.open()

    async def compatibility_store(*_args, **_kwargs):
        return handle

    monkeypatch.setattr(_runs, "_open_shared_db", compatibility_store)
    lookups = _record_shared_store_lookups(monkeypatch)
    monkeypatch.setattr(
        "lionagi.ln._proc.current_pid_markers",
        lambda: {
            "pid": 1,
            "pid_create_time": 0.0,
            "pid_host": "test",
            "pid_boot_time": 0.0,
            "process_identity_mode": "local",
        },
    )
    try:
        branch = Branch(name="main")
        ctx = await _runs.setup_agent_persist(
            branch,
            agent_name="fixture-agent",
            model="fixture-model",
            provider="fixture-provider",
            project="fixture-project",
            share_db=share_db,
        )

        assert ctx is not None
        assert ctx["worker_spawn_owner"] is None
        assert ctx["worker_spawns_unrecorded"] is True
        assert lookups == []
        assert _rows(store, "SELECT COUNT(*) FROM sessions") == [(1,)]
        assert _rows(store, f"SELECT name, model, provider, agent_name, {OWNER} FROM branches") == [
            ("main", "fixture-model", "fixture-provider", "fixture-agent", "principal:a")
        ]
        assert _rows(
            store, "SELECT status, status_reason_code, model, provider, agent_name FROM sessions"
        ) == [("running", "run.started.ok", "fixture-model", "fixture-provider", "fixture-agent")]
    finally:
        await handle.close()


@pytest.mark.parametrize("share_db", [True, False], ids=["shared", "not-shared"])
async def test_native_agent_setup_reaches_no_store_beside_its_own(tmp_path, monkeypatch, share_db):
    """Natively the setup handlers, handed the run's handle, open no process-wide store either,
    and the rows carry the same provenance and started reason as before."""
    from lionagi.cli import _runs
    from lionagi.session.branch import Branch

    handle = StateDB(tmp_path / "native.db")
    await handle.open()

    async def native_store(*_args, **_kwargs):
        return handle

    monkeypatch.setattr(_runs, "_open_shared_db", native_store)
    lookups = _record_shared_store_lookups(monkeypatch)
    monkeypatch.setattr(
        "lionagi.ln._proc.current_pid_markers",
        lambda: {
            "pid": 1,
            "pid_create_time": 0.0,
            "pid_host": "test",
            "pid_boot_time": 0.0,
            "process_identity_mode": "local",
        },
    )
    try:
        branch = Branch(name="main")
        ctx = await _runs.setup_agent_persist(
            branch,
            agent_name="fixture-agent",
            model="fixture-model",
            provider="fixture-provider",
            project="fixture-project",
            share_db=share_db,
        )

        assert ctx is not None
        assert ctx["worker_spawn_owner"] is not None
        assert ctx["worker_spawns_unrecorded"] is False
        assert lookups == []
        session = await handle.get_session(ctx["session_id"])
        assert (session["status"], session["status_reason_code"]) == ("running", "run.started.ok")
        assert (session["model"], session["provider"], session["agent_name"]) == (
            "fixture-model",
            "fixture-provider",
            "fixture-agent",
        )
        assert session["started_at"] is not None
        row = await handle.get_branch(str(branch.id))
        assert (row["model"], row["provider"], row["agent_name"]) == (
            "fixture-model",
            "fixture-provider",
            "fixture-agent",
        )
    finally:
        await handle.close()


async def test_a_run_cannot_update_a_branch_another_run_created(store):
    """Compatibility mode reaches only branch rows the writing run created: a second handle for the
    same owner refuses an update to the first run's branch instead of overwriting its columns."""
    base = _profile_that_admits_agent_setup(store)
    profile = CompatibilityProfile(
        name=base.name,
        version=base.version,
        closure=base.closure,
        writes=base.writes,
        operations=base.operations | {"update_branch"},
        owner=base.owner,
    )
    first = StateDB(store, compatibility=profile, owner="principal:a")
    await first.open()
    try:
        await _setup_run(first)
    finally:
        await first.close()

    second = StateDB(store, compatibility=profile, owner="principal:a")
    await second.open()
    try:
        with pytest.raises(StateCompatibilityRefused, match="was not created by this run"):
            await second.update_branch("b1", model="clobbered-by-another-run")
    finally:
        await second.close()

    assert _rows(store, "SELECT model FROM branches WHERE id = 'b1'") == [(None,)]


class _ExtendsAProgression(StateDB):
    """Drive the batched progression append the way its own operation would."""

    @_operation("insert_message")
    async def extend_progression(self, progression_id: str, message_ids: list[str]) -> None:
        async with self._tx() as conn:
            await self._extend_progression_in_tx(conn, progression_id, message_ids)


async def test_the_batched_progression_append_keeps_the_owner_predicate(store):
    """The batched rewrite (behind insert_messages) applies the same owner predicate on
    its read and its rewrite as the per-message append: this run's own progression is
    extended, and a progression another owner's run owns is read as absent, not rewritten.
    """
    from sqlalchemy import event

    _execute(
        store,
        f"INSERT INTO progressions (id, created_at, collection, {OWNER}) "
        "VALUES ('p-other', 0, '[]', 'principal:b')",
    )

    handle = _ExtendsAProgression(store, compatibility=_profile(store), owner="principal:a")
    await handle.open()
    try:
        await handle.create_progression("p-mine")
        await handle.extend_progression("p-mine", ["m1", "m2"])
        statements = []

        def spy(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(handle._engine.sync_engine, "before_cursor_execute", spy)
        try:
            await handle.extend_progression("p-other", ["m-other"])
        finally:
            event.remove(handle._engine.sync_engine, "before_cursor_execute", spy)
    finally:
        await handle.close()

    # Read as absent: the read carries the predicate, so no rewrite is even attempted.
    assert any(s.lstrip().startswith("SELECT collection FROM progressions") for s in statements)
    assert not [s for s in statements if s.lstrip().upper().startswith("UPDATE PROGRESSIONS")]

    assert _rows(store, "SELECT collection FROM progressions WHERE id = 'p-mine'") == [
        ('["m1","m2"]',)
    ]
    assert _rows(store, "SELECT collection FROM progressions WHERE id = 'p-other'") == [("[]",)]


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
def test_the_progression_read_puts_the_owner_conjunct_inside_the_where_clause(dialect):
    """The owner conjunct joins the WHERE clause; on a dialect that row-locks the read it
    comes before FOR UPDATE, never after it."""
    clause = ' AND "owner_principal_id" = :_compat_owner'
    sql = StateDB._progression_collection_for_update_sql(dialect, clause)
    head = "SELECT collection FROM progressions WHERE id = :id" + clause
    assert sql == (head if dialect == "sqlite" else head + " FOR UPDATE")
