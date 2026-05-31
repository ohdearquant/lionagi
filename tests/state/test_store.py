# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.state.store (StateStore protocol, SQLiteStore, translation)
and lionagi.state.postgres (PostgresStore + translate_sql).

SQLiteStore tests use real in-memory SQLite — no mocking required.
PostgresStore tests mock asyncpg so no running PostgreSQL instance is needed.
asyncio_mode = "auto" (pyproject.toml) — no @pytest.mark.asyncio needed.
"""

from __future__ import annotations

import unittest.mock as mock
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from lionagi.state.postgres import _replace_placeholders, translate_sql
from lionagi.state.store import SQLiteStore, StateStore, from_json_column, to_json_column

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def store():
    """Fresh in-memory SQLiteStore with a simple test table."""
    s = SQLiteStore(":memory:")
    await s.connect()
    await s.execute_script(
        """
        CREATE TABLE items (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            name    TEXT NOT NULL,
            meta    TEXT
        );
        """
    )
    yield s
    await s.close()


# ─────────────────────────────────────────────────────────────────────────────
# SQLiteStore — connection lifecycle
# ─────────────────────────────────────────────────────────────────────────────


async def test_sqlite_store_connect_disconnect():
    s = SQLiteStore(":memory:")
    assert not s.is_connected()
    await s.connect()
    assert s.is_connected()
    await s.close()
    assert not s.is_connected()


async def test_sqlite_store_async_context_manager():
    async with SQLiteStore(":memory:") as s:
        assert s.is_connected()
    assert not s.is_connected()


async def test_sqlite_store_not_connected_raises():
    s = SQLiteStore(":memory:")
    with pytest.raises(RuntimeError, match="not connected"):
        await s.execute("SELECT 1")


async def test_sqlite_store_double_close_is_safe():
    s = SQLiteStore(":memory:")
    await s.connect()
    await s.close()
    await s.close()  # second close must not raise


# ─────────────────────────────────────────────────────────────────────────────
# SQLiteStore — basic CRUD
# ─────────────────────────────────────────────────────────────────────────────


async def test_sqlite_execute_empty_table(store: SQLiteStore):
    rows = await store.execute("SELECT * FROM items")
    assert rows == []


async def test_sqlite_execute_insert_returns_lastrowid(store: SQLiteStore):
    rowid = await store.execute_insert(
        "INSERT INTO items (name) VALUES (?)",
        ("alpha",),
    )
    assert isinstance(rowid, int)
    assert rowid > 0


async def test_sqlite_execute_insert_and_select(store: SQLiteStore):
    await store.execute_insert("INSERT INTO items (name) VALUES (?)", ("beta",))
    rows = await store.execute("SELECT name FROM items WHERE name = ?", ("beta",))
    assert len(rows) == 1
    assert rows[0]["name"] == "beta"


async def test_sqlite_execute_returns_multiple_rows(store: SQLiteStore):
    await store.execute_insert("INSERT INTO items (name) VALUES (?)", ("x",))
    await store.execute_insert("INSERT INTO items (name) VALUES (?)", ("y",))
    rows = await store.execute("SELECT name FROM items ORDER BY name")
    names = [r["name"] for r in rows]
    assert names == ["x", "y"]


async def test_sqlite_executemany(store: SQLiteStore):
    params = [("a",), ("b",), ("c",)]
    await store.executemany("INSERT INTO items (name) VALUES (?)", params)
    rows = await store.execute("SELECT COUNT(*) AS cnt FROM items")
    assert rows[0]["cnt"] == 3


async def test_sqlite_execute_with_no_params(store: SQLiteStore):
    await store.execute_insert("INSERT INTO items (name) VALUES (?)", ("z",))
    rows = await store.execute("SELECT * FROM items")
    assert len(rows) == 1


# ─────────────────────────────────────────────────────────────────────────────
# SQLiteStore — transactions
# ─────────────────────────────────────────────────────────────────────────────


async def test_sqlite_transaction_commit(store: SQLiteStore):
    async with store.transaction():
        await store.execute_insert("INSERT INTO items (name) VALUES (?)", ("committed",))
        # Override autocommit: in transaction context, commit happens on exit
        # Use execute directly (bypasses inner commit) to verify atomicity
        await store._db.execute("INSERT INTO items (name) VALUES (?)", ("also_committed",))
    rows = await store.execute("SELECT name FROM items ORDER BY name")
    names = [r["name"] for r in rows]
    assert "committed" in names or "also_committed" in names  # at least one row present


async def test_sqlite_transaction_rollback_on_exception(store: SQLiteStore):
    with pytest.raises(ValueError, match="oops"):
        async with store.transaction():
            await store._db.execute("INSERT INTO items (name) VALUES (?)", ("never_saved",))
            raise ValueError("oops")
    rows = await store.execute("SELECT * FROM items")
    assert rows == []


async def test_sqlite_transaction_rollback_leaves_prior_data_intact(store: SQLiteStore):
    await store.execute_insert("INSERT INTO items (name) VALUES (?)", ("existing",))
    with pytest.raises(RuntimeError):
        async with store.transaction():
            await store._db.execute("INSERT INTO items (name) VALUES (?)", ("transient",))
            raise RuntimeError("abort")
    rows = await store.execute("SELECT name FROM items")
    names = [r["name"] for r in rows]
    assert "existing" in names
    assert "transient" not in names


async def test_sqlite_execute_insert_inside_transaction_is_atomic(store: SQLiteStore):
    """execute_insert() inside transaction() must NOT auto-commit.

    Inserts two rows via execute_insert() then raises before the transaction
    closes.  Neither row should be visible after rollback — verifying that
    the _in_txn flag correctly suppresses the per-call commit.
    """
    with pytest.raises(ValueError, match="rollback_me"):
        async with store.transaction():
            await store.execute_insert("INSERT INTO items (name) VALUES (?)", ("row_a",))
            await store.execute_insert("INSERT INTO items (name) VALUES (?)", ("row_b",))
            raise ValueError("rollback_me")

    rows = await store.execute("SELECT name FROM items")
    names = [r["name"] for r in rows]
    assert "row_a" not in names, "row_a should not be committed after rollback"
    assert "row_b" not in names, "row_b should not be committed after rollback"


async def test_sqlite_executemany_inside_transaction_is_atomic(store: SQLiteStore):
    """executemany() inside transaction() must NOT auto-commit.

    Inserts three rows via executemany() then raises before the transaction
    closes.  No rows should be visible after rollback.
    """
    with pytest.raises(RuntimeError, match="abort_many"):
        async with store.transaction():
            await store.executemany(
                "INSERT INTO items (name) VALUES (?)",
                [("m1",), ("m2",), ("m3",)],
            )
            raise RuntimeError("abort_many")

    rows = await store.execute("SELECT name FROM items")
    assert rows == [], "No rows should survive a rolled-back executemany"


# ─────────────────────────────────────────────────────────────────────────────
# SQLiteStore — satisfies StateStore protocol
# ─────────────────────────────────────────────────────────────────────────────


def test_sqlite_store_satisfies_protocol():
    """isinstance() check works because StateStore is @runtime_checkable."""
    s = SQLiteStore(":memory:")
    assert isinstance(s, StateStore)


# ─────────────────────────────────────────────────────────────────────────────
# JSON column helpers
# ─────────────────────────────────────────────────────────────────────────────


def test_to_json_column_none_passthrough():
    assert to_json_column(None) is None


def test_to_json_column_bytes_passthrough():
    b = b"\x00\x01\x02"
    assert to_json_column(b) is b


def test_to_json_column_dict():
    result = to_json_column({"k": "v"})
    import json

    assert json.loads(result) == {"k": "v"}


def test_to_json_column_string_that_looks_like_json():
    # A plain string should be serialised so it round-trips correctly.
    import json

    s = '{"text": "x"}'
    result = to_json_column(s)
    # Stored as a JSON string-of-a-string; loads gives back the original.
    assert json.loads(result) == s


def test_from_json_column_non_string_passthrough():
    assert from_json_column(42) == 42
    assert from_json_column(None) is None
    assert from_json_column({"a": 1}) == {"a": 1}


def test_from_json_column_valid_json():
    assert from_json_column('{"k": "v"}') == {"k": "v"}


def test_from_json_column_invalid_json_returns_string():
    assert from_json_column("not-json-at-all") == "not-json-at-all"


# ─────────────────────────────────────────────────────────────────────────────
# translate_sql — placeholder conversion
# ─────────────────────────────────────────────────────────────────────────────


def test_translate_single_placeholder():
    assert translate_sql("SELECT * FROM t WHERE id = ?") == "SELECT * FROM t WHERE id = $1"


def test_translate_multiple_placeholders():
    result = translate_sql("INSERT INTO t (a, b, c) VALUES (?, ?, ?)")
    assert "$1" in result
    assert "$2" in result
    assert "$3" in result
    assert "?" not in result


def test_translate_placeholder_skips_string_literals():
    # The '?' inside a quoted string must not be replaced.
    sql = "SELECT * FROM t WHERE col = '?'"
    result = translate_sql(sql)
    assert result == sql  # no ? outside quotes, none replaced


def test_translate_mixed_literal_and_placeholder():
    sql = "SELECT * FROM t WHERE a = ? AND b = '?'"
    result = translate_sql(sql)
    assert "$1" in result
    assert "'?'" in result  # literal preserved
    assert result.count("?") == 1  # only the literal one remains (inside quotes)


def test_replace_placeholders_empty_string():
    assert _replace_placeholders("") == ""


def test_replace_placeholders_no_placeholders():
    sql = "SELECT 1"
    assert _replace_placeholders(sql) == sql


def test_replace_placeholders_escaped_quote():
    # '' inside a string literal (SQL escaped quote) should not be split.
    sql = "SELECT * FROM t WHERE name = 'it''s'"
    result = _replace_placeholders(sql)
    assert result == sql  # no ? to replace, string intact


# ─────────────────────────────────────────────────────────────────────────────
# translate_sql — dialect translations
# ─────────────────────────────────────────────────────────────────────────────


def test_translate_integer_primary_key():
    sql = "CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)"
    result = translate_sql(sql)
    assert "SERIAL PRIMARY KEY" in result
    assert "INTEGER PRIMARY KEY" not in result


def test_translate_bigint_primary_key():
    sql = "CREATE TABLE t (id BIGINT PRIMARY KEY, val TEXT)"
    result = translate_sql(sql)
    assert "BIGSERIAL PRIMARY KEY" in result


def test_translate_datetime_now():
    sql = "INSERT INTO t (created_at) VALUES (datetime('now'))"
    result = translate_sql(sql)
    assert "now()" in result
    assert "datetime(" not in result


def test_translate_strftime():
    sql = "SELECT strftime('%s', 'now') AS ts"
    result = translate_sql(sql)
    assert "now()" in result
    assert "strftime(" not in result


def test_translate_insert_or_ignore():
    sql = "INSERT OR IGNORE INTO t (a) VALUES (?)"
    result = translate_sql(sql)
    assert "OR IGNORE" not in result
    assert "ON CONFLICT DO NOTHING" in result
    assert "$1" in result  # placeholder also translated


def test_translate_json_extract():
    sql = "SELECT json_extract(col, '$.key') FROM t WHERE id = ?"
    result = translate_sql(sql)
    assert "json_extract" not in result.lower()
    assert "->>" in result
    assert "$1" in result


def test_translate_no_op_on_plain_sql():
    # A simple query with no SQLite-isms should pass through (after $N sub)
    sql = "SELECT id, name FROM sessions WHERE status = $1"
    result = translate_sql(sql)
    # No double-substitution: $1 should remain $1, not become $$1 etc.
    assert result.count("$1") == 1


# ─────────────────────────────────────────────────────────────────────────────
# PostgresStore — import error without asyncpg
# ─────────────────────────────────────────────────────────────────────────────


def test_postgres_store_raises_import_error_without_asyncpg():
    """PostgresStore must raise ImportError at __init__ if asyncpg is absent."""
    import builtins
    import sys

    original = sys.modules.pop("asyncpg", None)
    real_import = builtins.__import__

    def _block_asyncpg(name, *args, **kwargs):
        if name == "asyncpg" or name.startswith("asyncpg."):
            raise ImportError("No module named 'asyncpg'")
        return real_import(name, *args, **kwargs)

    try:
        with mock.patch.object(builtins, "__import__", side_effect=_block_asyncpg):
            from lionagi.state.postgres import PostgresStore

            with pytest.raises(ImportError, match="asyncpg"):
                PostgresStore("postgresql://localhost/test")
    finally:
        if original is not None:
            sys.modules["asyncpg"] = original


# ─────────────────────────────────────────────────────────────────────────────
# PostgresStore — mocked asyncpg (no real DB required)
# ─────────────────────────────────────────────────────────────────────────────


def _make_mock_asyncpg_pool():
    """Build a mock asyncpg pool + connection that records calls."""

    mock_record = {"id": 1, "name": "test"}

    # asyncpg.Record-like mock (supports dict())
    mock_row = mock.MagicMock()
    mock_row.__iter__ = mock.MagicMock(return_value=iter(mock_record.items()))
    mock_row.keys = mock.MagicMock(return_value=mock_record.keys())
    # dict(record) in asyncpg uses __iter__ over (key, value) pairs — use MappingProxy
    # Simpler: use a real dict as the "record" since _record_to_dict calls dict()
    mock_row = dict(mock_record)  # real dict; dict(dict) works fine

    mock_conn = mock.AsyncMock()
    mock_conn.fetch = mock.AsyncMock(return_value=[mock_row])
    mock_conn.execute = mock.AsyncMock(return_value="INSERT 0 1")
    mock_conn.executemany = mock.AsyncMock(return_value=None)

    # transaction() context manager
    mock_txn = mock.AsyncMock()
    mock_txn.__aenter__ = mock.AsyncMock(return_value=None)
    mock_txn.__aexit__ = mock.AsyncMock(return_value=False)
    mock_conn.transaction = mock.MagicMock(return_value=mock_txn)

    # pool.acquire() context manager
    @asynccontextmanager
    async def _acquire() -> AsyncIterator[Any]:
        yield mock_conn

    mock_pool = mock.MagicMock()
    mock_pool.acquire = _acquire
    mock_pool.close = mock.AsyncMock(return_value=None)

    return mock_pool, mock_conn


def _make_mock_asyncpg_module(pool: Any) -> mock.MagicMock:
    """Return a mock asyncpg module whose create_pool returns pool."""
    m = mock.MagicMock()
    m.create_pool = mock.AsyncMock(return_value=pool)
    return m


async def test_postgres_store_connect_and_close():
    """connect() calls asyncpg.create_pool; close() calls pool.close()."""
    mock_pool, _ = _make_mock_asyncpg_pool()
    mock_asyncpg = _make_mock_asyncpg_module(mock_pool)

    import sys

    sys.modules["asyncpg"] = mock_asyncpg
    try:
        from importlib import reload

        import lionagi.state.postgres as pg_mod

        reload(pg_mod)
        store = pg_mod.PostgresStore("postgresql://localhost/test")
        assert not store.is_connected()
        await store.connect()
        assert store.is_connected()
        await store.close()
        assert not store.is_connected()
    finally:
        del sys.modules["asyncpg"]


async def test_postgres_store_execute_returns_rows():
    mock_pool, mock_conn = _make_mock_asyncpg_pool()
    mock_asyncpg = _make_mock_asyncpg_module(mock_pool)

    import sys

    sys.modules["asyncpg"] = mock_asyncpg
    try:
        from importlib import reload

        import lionagi.state.postgres as pg_mod

        reload(pg_mod)
        store = pg_mod.PostgresStore("postgresql://localhost/test")
        await store.connect()
        rows = await store.execute("SELECT id, name FROM items WHERE id = ?", (1,))
        assert len(rows) == 1
        assert rows[0]["id"] == 1
        assert rows[0]["name"] == "test"
        # Verify placeholder translation was applied to the call
        called_sql = mock_conn.fetch.call_args[0][0]
        assert "?" not in called_sql
        assert "$1" in called_sql
        await store.close()
    finally:
        del sys.modules["asyncpg"]


async def test_postgres_store_executemany():
    mock_pool, mock_conn = _make_mock_asyncpg_pool()
    mock_asyncpg = _make_mock_asyncpg_module(mock_pool)

    import sys

    sys.modules["asyncpg"] = mock_asyncpg
    try:
        from importlib import reload

        import lionagi.state.postgres as pg_mod

        reload(pg_mod)
        store = pg_mod.PostgresStore("postgresql://localhost/test")
        await store.connect()
        params = [("a",), ("b",)]
        await store.executemany("INSERT INTO t (name) VALUES (?)", params)
        mock_conn.executemany.assert_called_once()
        called_sql = mock_conn.executemany.call_args[0][0]
        assert "?" not in called_sql
        assert "$1" in called_sql
        await store.close()
    finally:
        del sys.modules["asyncpg"]


async def test_postgres_store_transaction_context_manager():
    mock_pool, mock_conn = _make_mock_asyncpg_pool()
    mock_asyncpg = _make_mock_asyncpg_module(mock_pool)

    import sys

    sys.modules["asyncpg"] = mock_asyncpg
    try:
        from importlib import reload

        import lionagi.state.postgres as pg_mod

        reload(pg_mod)
        store = pg_mod.PostgresStore("postgresql://localhost/test")
        await store.connect()
        async with store.transaction():
            pass  # just verify the context manager completes without error
        await store.close()
    finally:
        del sys.modules["asyncpg"]


async def test_postgres_store_not_connected_raises():
    mock_pool, _ = _make_mock_asyncpg_pool()
    mock_asyncpg = _make_mock_asyncpg_module(mock_pool)

    import sys

    sys.modules["asyncpg"] = mock_asyncpg
    try:
        from importlib import reload

        import lionagi.state.postgres as pg_mod

        reload(pg_mod)
        store = pg_mod.PostgresStore("postgresql://localhost/test")
        # Do NOT call connect()
        with pytest.raises(RuntimeError, match="not connected"):
            await store.execute("SELECT 1")
    finally:
        del sys.modules["asyncpg"]


async def test_postgres_store_satisfies_statestore_protocol():
    """PostgresStore satisfies the StateStore protocol (runtime_checkable)."""
    mock_pool, _ = _make_mock_asyncpg_pool()
    mock_asyncpg = _make_mock_asyncpg_module(mock_pool)

    import sys

    sys.modules["asyncpg"] = mock_asyncpg
    try:
        from importlib import reload

        import lionagi.state.postgres as pg_mod

        reload(pg_mod)
        store = pg_mod.PostgresStore("postgresql://localhost/test")
        assert isinstance(store, StateStore)
    finally:
        del sys.modules["asyncpg"]
