# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""StateStore — backend-agnostic storage interface for the lionagi state layer.

The existing :class:`~lionagi.state.db.StateDB` continues to work unchanged;
this module provides a *parallel path* that new code can use when it needs to
be backend-agnostic (SQLite or PostgreSQL).

Protocol
--------
:class:`StateStore` is a :pep:`544` structural-typing protocol.  Any class
that implements the six async methods below satisfies it without explicit
inheritance.

Concrete implementations
------------------------
* :class:`SQLiteStore` — wraps ``aiosqlite`` for drop-in SQLite compatibility.
* ``PostgresStore`` lives in :mod:`lionagi.state.postgres` (optional asyncpg
  dependency).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, runtime_checkable

import aiosqlite

if TYPE_CHECKING:
    from typing import Protocol
else:
    from typing import Protocol


@runtime_checkable
class StateStore(Protocol):
    """Abstract storage interface used by the lionagi state layer.

    All methods are async so that both I/O-bound (SQLite via aiosqlite) and
    network-bound (PostgreSQL via asyncpg) backends share the same call
    surface without blocking the event loop.

    SQL flavour
    -----------
    Callers should use SQLite-style ``?`` positional placeholders.
    Each backend is responsible for translating to its native style (e.g.
    ``$1, $2, …`` for PostgreSQL) before execution.

    Thread / coroutine safety
    -------------------------
    Implementations are expected to be safe for concurrent ``await`` calls
    from a single event-loop thread; additional inter-thread safety is not
    guaranteed.
    """

    async def execute(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Execute *sql* and return all rows as plain dicts.

        Parameters
        ----------
        sql:
            A SELECT (or DML with a RETURNING clause) statement using ``?``
            positional placeholders.
        params:
            Positional parameter values matching the ``?`` placeholders.

        Returns
        -------
        list[dict[str, Any]]
            Zero or more rows.  Column names are the keys; values are Python
            native types (str, int, float, bytes, None).
        """
        ...

    async def execute_insert(self, sql: str, params: tuple = ()) -> int:
        """Execute an INSERT statement and return the *lastrowid*.

        Parameters
        ----------
        sql:
            An INSERT statement using ``?`` positional placeholders.
        params:
            Positional parameter values.

        Returns
        -------
        int
            The rowid of the newly inserted row (``cursor.lastrowid``).
        """
        ...

    async def executemany(self, sql: str, params_list: list[tuple]) -> None:
        """Execute *sql* once per item in *params_list* in a single batch.

        Equivalent to ``cursor.executemany``; intended for bulk inserts or
        bulk updates where the statement shape is identical for every row.

        Parameters
        ----------
        sql:
            A DML statement using ``?`` positional placeholders.
        params_list:
            Each element is a tuple of positional values for one execution.
        """
        ...

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:  # type: ignore[override]
        """Async context manager that wraps a database transaction.

        On normal exit the transaction is committed; on exception it is
        rolled back.

        Usage::

            async with store.transaction():
                await store.execute_insert(...)
                await store.execute(...)
        """
        yield  # pragma: no cover — protocol stub, never called directly

    async def close(self) -> None:
        """Close the underlying connection / pool and release resources."""
        ...

    def is_connected(self) -> bool:
        """Return ``True`` if the store has an open connection."""
        ...


# ── SQLiteStore ───────────────────────────────────────────────────────────────


class SQLiteStore:
    """SQLite backend for :class:`StateStore` built on *aiosqlite*.

    This is a compatibility shim that exposes the :class:`StateStore`
    protocol surface over an ``aiosqlite`` connection.  The existing
    :class:`~lionagi.state.db.StateDB` is **not** modified — this class
    provides an alternative entry point for new code that wants a simpler,
    backend-agnostic interface.

    Parameters
    ----------
    db_path:
        Filesystem path to the SQLite database file, or ``":memory:"`` for
        an in-memory database (useful in tests).

    Example
    -------
    ::

        store = SQLiteStore(":memory:")
        await store.connect()
        async with store.transaction():
            await store.execute_insert(
                "INSERT INTO t (col) VALUES (?)", ("val",)
            )
        rows = await store.execute("SELECT * FROM t")
        await store.close()
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self._conn: aiosqlite.Connection | None = None
        self._txn_depth: int = 0  # nesting counter; >0 while inside a transaction() block

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open the aiosqlite connection and configure row_factory."""
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row

    async def close(self) -> None:
        """Close the aiosqlite connection."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def is_connected(self) -> bool:
        """Return ``True`` when the connection is open."""
        return self._conn is not None

    # ── Async context manager ─────────────────────────────────────────────

    async def __aenter__(self) -> SQLiteStore:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # ── Internal helper ───────────────────────────────────────────────────

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("SQLiteStore is not connected — call connect() or use async with")
        return self._conn

    @staticmethod
    def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
        """Convert an aiosqlite.Row to a plain dict.

        JSON columns that are stored as strings are left as-is; callers that
        need deserialization should do so themselves (unlike StateDB which
        eagerly parses a fixed set of JSON keys).
        """
        return dict(row)

    # ── StateStore protocol ───────────────────────────────────────────────

    async def execute(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Execute *sql* and return all rows as dicts."""
        cur = await self._db.execute(sql, params)
        rows = await cur.fetchall()
        return [self._row_to_dict(r) for r in rows]

    async def execute_insert(self, sql: str, params: tuple = ()) -> int:
        """Execute an INSERT and return lastrowid.

        When called inside a :meth:`transaction` block the commit is deferred
        to the transaction boundary.  Outside a transaction an implicit commit
        is issued immediately so the row is visible to subsequent reads.
        """
        cur = await self._db.execute(sql, params)
        if not self._txn_depth > 0:
            await self._db.commit()
        return cur.lastrowid or 0

    async def executemany(self, sql: str, params_list: list[tuple]) -> None:
        """Execute *sql* once per item in *params_list*.

        Commit behaviour mirrors :meth:`execute_insert`: deferred inside a
        :meth:`transaction` block, immediate otherwise.
        """
        await self._db.executemany(sql, params_list)
        if not self._txn_depth > 0:
            await self._db.commit()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Commit on success, rollback on exception.

        Increments :attr:`_txn_depth` on entry and decrements it in the
        ``finally`` block.  Actual commit/rollback only occurs when the
        outermost call unwinds (depth returns to 0), so nested
        ``transaction()`` calls are safe and do not prematurely reset the
        transaction state.
        """
        self._txn_depth += 1
        try:
            yield
            if self._txn_depth == 1:
                await self._db.commit()
        except Exception:
            if self._txn_depth == 1:
                await self._db.rollback()
            raise
        finally:
            self._txn_depth -= 1

    async def execute_script(self, sql: str) -> None:
        """Execute a multi-statement SQL script (DDL setup helper)."""
        await self._db.executescript(sql)


# ── JSON serialisation helpers (shared by both backends) ─────────────────────


def to_json_column(value: Any) -> Any:
    """Serialize *value* for storage in a JSON/TEXT column.

    ``None`` and ``bytes``-like values pass through unchanged.  Everything
    else is serialised with :func:`json.dumps` so that a Python string that
    happens to contain valid JSON round-trips correctly (deserialisation with
    :func:`json.loads` is the exact inverse).
    """
    if value is None or isinstance(value, bytes | bytearray | memoryview):
        return value
    return json.dumps(value)


def from_json_column(value: Any) -> Any:
    """Deserialise a value returned from a JSON/TEXT column.

    If *value* is a ``str``, attempt ``json.loads``; return the original
    string on failure.  Non-string values pass through unchanged.
    """
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return value
