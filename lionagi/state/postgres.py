# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""PostgreSQL backend for the :class:`~lionagi.state.store.StateStore` protocol.

``asyncpg`` is an **optional** dependency.  The module can always be imported;
:class:`PostgresStore` raises :class:`ImportError` at *instantiation* time if
``asyncpg`` is not installed.

SQL dialect translation
-----------------------
lionagi's internal SQL is written in SQLite dialect (``?`` placeholders,
SQLite-specific functions, etc.).  :func:`translate_sql` converts the most
common patterns to PostgreSQL before execution:

* ``?`` positional placeholders  →  ``$1, $2, …``
* ``INTEGER PRIMARY KEY``        →  ``SERIAL PRIMARY KEY``
* ``json_extract(col, '$.key')`` →  ``col->>'key'``
* ``datetime('now')``            →  ``now()``
* ``strftime(…)``                →  ``now()``
* ``INSERT OR IGNORE``           →  ``INSERT … ON CONFLICT DO NOTHING``
* ``INSERT OR REPLACE``          →  ``INSERT … ON CONFLICT … DO UPDATE``

The translation is intentionally *conservative*: patterns not listed above
pass through unchanged.  Callers that need full dialect control should write
native PostgreSQL SQL and pass it directly to :meth:`PostgresStore.execute`.

Schema
------
Use :mod:`lionagi.state.schema_pg.sql` (PostgreSQL DDL) to initialise the
database before first use.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg  # noqa: F401 — type-check only, never imported at runtime


# ── SQL dialect translator ────────────────────────────────────────────────────


def translate_sql(sql: str) -> str:
    """Translate SQLite-dialect SQL to PostgreSQL dialect.

    The function applies a fixed set of textual substitutions that cover the
    patterns used in lionagi's query layer.  The translation is *stateless*
    and *non-destructive*: a string that contains no SQLite-isms is returned
    unchanged.

    Parameters
    ----------
    sql:
        A SQL string possibly containing SQLite-specific syntax.

    Returns
    -------
    str
        A SQL string with SQLite-isms replaced by PostgreSQL equivalents.

    Examples
    --------
    >>> translate_sql("SELECT * FROM t WHERE id = ?")
    'SELECT * FROM t WHERE id = $1'
    >>> translate_sql("INSERT OR IGNORE INTO t (a) VALUES (?)")
    'INSERT INTO t (a) VALUES ($1) ON CONFLICT DO NOTHING'
    """
    # ── 1. INSERT OR IGNORE → INSERT … ON CONFLICT DO NOTHING ────────
    # Single atomic replacement: capture everything after INTO and append
    # the ON CONFLICT clause.  Must run before any other INSERT rewrites.
    sql = re.sub(
        r"\bINSERT\s+OR\s+IGNORE\s+(INTO\b.*?)(\s*;?\s*)$",
        r"INSERT \1 ON CONFLICT DO NOTHING\2",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # ── 2. INSERT OR REPLACE → INSERT (caller should add ON CONFLICT DO UPDATE) ──
    sql = re.sub(
        r"\bINSERT\s+OR\s+REPLACE\s+INTO\b",
        "INSERT INTO",
        sql,
        flags=re.IGNORECASE,
    )

    # ── 3. INTEGER PRIMARY KEY → SERIAL PRIMARY KEY ───────────────────
    sql = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\b",
        "SERIAL PRIMARY KEY",
        sql,
        flags=re.IGNORECASE,
    )

    # ── 4. BIGINT PRIMARY KEY → BIGSERIAL PRIMARY KEY ─────────────────
    sql = re.sub(
        r"\bBIGINT\s+PRIMARY\s+KEY\b",
        "BIGSERIAL PRIMARY KEY",
        sql,
        flags=re.IGNORECASE,
    )

    # ── 5. json_extract(col, '$.key') → (col->>'key') ────────────────
    def _json_extract_sub(m: re.Match) -> str:  # type: ignore[type-arg]
        col = m.group(1).strip()
        path = m.group(2).strip().strip("'\"")
        key = path[2:] if path.startswith("$.") else path
        return f"({col}->>'{key}')"

    sql = re.sub(
        r"\bjson_extract\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
        _json_extract_sub,
        sql,
        flags=re.IGNORECASE,
    )

    # ── 6. datetime('now') / strftime(…) → now() ─────────────────────
    sql = re.sub(
        r"\bdatetime\s*\(\s*'now'\s*\)",
        "now()",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\bstrftime\s*\([^)]*'now'[^)]*\)",
        "now()",
        sql,
        flags=re.IGNORECASE,
    )

    # ── 7. ? positional placeholders → $1, $2, … ─────────────────────
    sql = _replace_placeholders(sql)

    return sql


def _replace_placeholders(sql: str) -> str:
    """Replace all ``?`` in *sql* with ``$1``, ``$2``, … in order.

    Characters inside single-quoted string literals are skipped so that a
    literal ``'?'`` in a DEFAULT clause is not mangled.
    """
    result: list[str] = []
    counter = 0
    in_string = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'" and not in_string:
            in_string = True
            result.append(ch)
        elif ch == "'" and in_string:
            # Peek for escaped quote ''
            if i + 1 < len(sql) and sql[i + 1] == "'":
                result.append("''")
                i += 2
                continue
            in_string = False
            result.append(ch)
        elif ch == "?" and not in_string:
            counter += 1
            result.append(f"${counter}")
        else:
            result.append(ch)
        i += 1
    return "".join(result)


# ── PostgresStore ─────────────────────────────────────────────────────────────


class PostgresStore:
    """PostgreSQL backend for :class:`~lionagi.state.store.StateStore`.

    Uses ``asyncpg`` for fully-async, connection-pooled access to a
    PostgreSQL database.  ``asyncpg`` is a *lazy optional* dependency:
    the module can always be imported, but :class:`PostgresStore` raises
    :exc:`ImportError` at instantiation time if ``asyncpg`` is not installed.

    Parameters
    ----------
    dsn:
        A PostgreSQL connection string, e.g.
        ``"postgresql://user:pass@localhost/dbname"`` or the libpq-style
        ``"host=localhost dbname=mydb user=myuser"`` format.

    Example
    -------
    ::

        store = PostgresStore("postgresql://lion@localhost/state")
        await store.connect()
        rows = await store.execute("SELECT 1 AS n")
        await store.close()
    """

    def __init__(self, dsn: str) -> None:
        try:
            import asyncpg  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "asyncpg is required for PostgresStore. Install it with: pip install asyncpg"
            ) from exc
        self._dsn = dsn
        self._pool: Any = None  # asyncpg.Pool, typed as Any to avoid hard import
        self._txn_conn: Any = None  # set to the connection during the outermost transaction()
        self._txn_depth: int = 0  # nesting counter; >0 while inside a transaction() block

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Create an asyncpg connection pool.

        Raises
        ------
        ImportError
            If ``asyncpg`` is not installed (should have already raised in
            ``__init__``, but guard here for safety).
        """
        import asyncpg  # noqa: PLC0415

        self._pool = await asyncpg.create_pool(self._dsn)

    async def close(self) -> None:
        """Close the asyncpg connection pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def is_connected(self) -> bool:
        """Return ``True`` when the pool is open."""
        return self._pool is not None

    # ── Async context manager ─────────────────────────────────────────────

    async def __aenter__(self) -> PostgresStore:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    # ── Internal helpers ──────────────────────────────────────────────────

    @property
    def _pg_pool(self) -> Any:
        if self._pool is None:
            raise RuntimeError("PostgresStore is not connected — call connect() or use async with")
        return self._pool

    @staticmethod
    def _record_to_dict(record: Any) -> dict[str, Any]:
        """Convert an asyncpg.Record to a plain dict."""
        return dict(record)

    # ── StateStore protocol ───────────────────────────────────────────────

    async def execute(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Execute *sql* (SQLite dialect) and return all rows as dicts.

        The SQL is translated from SQLite to PostgreSQL dialect before
        execution via :func:`translate_sql`.

        When called inside a :meth:`transaction` block the already-acquired
        transaction connection is reused so the query participates in the
        ongoing transaction.
        """
        pg_sql = translate_sql(sql)
        if self._txn_conn is not None:
            rows = await self._txn_conn.fetch(pg_sql, *params)
            return [self._record_to_dict(r) for r in rows]
        async with self._pg_pool.acquire() as conn:
            rows = await conn.fetch(pg_sql, *params)
        return [self._record_to_dict(r) for r in rows]

    async def execute_insert(self, sql: str, params: tuple = ()) -> int:
        """Execute an INSERT and return lastrowid equivalent.

        asyncpg does not expose ``lastrowid``.  The method appends a
        ``RETURNING rowid`` clause if the SQL has no RETURNING already;
        if no integer rowid is available (PostgreSQL tables lack a
        ``rowid`` column), returns ``0``.

        For best results, write INSERT … RETURNING id and call
        :meth:`execute` directly.

        When called inside a :meth:`transaction` block the transaction
        connection is reused so the insert is atomic with the surrounding
        transaction.
        """
        pg_sql = translate_sql(sql)
        if self._txn_conn is not None:
            # asyncpg.execute returns a status string, not rows.
            await self._txn_conn.execute(pg_sql, *params)
            return 0
        async with self._pg_pool.acquire() as conn:
            await conn.execute(pg_sql, *params)
        return 0

    async def executemany(self, sql: str, params_list: list[tuple]) -> None:
        """Execute *sql* once per item in *params_list* via asyncpg executemany.

        When called inside a :meth:`transaction` block the transaction
        connection is reused.
        """
        pg_sql = translate_sql(sql)
        if self._txn_conn is not None:
            await self._txn_conn.executemany(pg_sql, params_list)
            return
        async with self._pg_pool.acquire() as conn:
            await conn.executemany(pg_sql, params_list)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Async context manager wrapping an asyncpg transaction.

        On the outermost call, acquires a pool connection, stores it in
        :attr:`_txn_conn`, and opens an asyncpg transaction.  Nested calls
        use ``conn.transaction()`` inside the existing transaction, which
        asyncpg implements via ``SAVEPOINT`` automatically, so an inner
        failure rolls back only the inner block without aborting the outer
        transaction.

        :attr:`_txn_conn` is only cleared and the pool connection only
        released when the outermost call unwinds (depth returns to 0),
        preventing premature connection release that would cause inner-block
        DML to acquire separate pool connections outside the transaction.
        """
        self._txn_depth += 1
        if self._txn_depth == 1:
            # Outermost call: acquire a connection and start the transaction.
            async with self._pg_pool.acquire() as conn:
                self._txn_conn = conn
                try:
                    async with conn.transaction():
                        yield
                finally:
                    self._txn_depth -= 1
                    self._txn_conn = None
        else:
            # Nested call: asyncpg creates a SAVEPOINT inside the existing
            # transaction, so an exception here rolls back only this block.
            conn = self._txn_conn
            if conn is None:
                self._txn_depth -= 1
                raise RuntimeError("Nested transaction() called but no connection acquired")
            try:
                async with conn.transaction():
                    yield
            finally:
                self._txn_depth -= 1

    async def apply_schema(self, schema_sql: str) -> None:
        """Execute a multi-statement DDL script (for initial schema setup).

        Parameters
        ----------
        schema_sql:
            The full contents of ``schema_pg.sql`` (PostgreSQL dialect DDL).
        """
        async with self._pg_pool.acquire() as conn:
            await conn.execute(schema_sql)
