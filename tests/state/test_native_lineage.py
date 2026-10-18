"""A native store names lionagi as its lineage, and a store without the row is adopted only when
its shape is one lionagi produced (ADR-0128 D7)."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from lionagi.state import SchemaTooNewError, StateDB, UnrecognizedStoreError
from lionagi.state.db import _LIONAGI_TABLES, _OPERATOR_TABLES, SCHEMA_VERSION

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
    db = StateDB(path)
    with pytest.raises(UnrecognizedStoreError):
        await db.open()
    assert db._engine is None
    assert _rows(path, "SELECT count(*) FROM schema_meta WHERE key = 'lineage'") == [(0,)]
    assert _rows(path, "SELECT count(*) FROM sqlite_master WHERE name = 'branches'") == [(0,)]


async def test_a_store_without_the_lineage_row_and_a_higher_version_is_too_new(tmp_path):
    path = tmp_path / "later.db"
    _execute(
        path,
        "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        f"INSERT INTO schema_meta VALUES ('version', '{int(SCHEMA_VERSION) + 10}');",
    )
    with pytest.raises(SchemaTooNewError):
        await _open_and_close(path)


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
