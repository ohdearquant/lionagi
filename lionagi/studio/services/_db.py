# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite

# Module import, not a from-import of the value: the timeout is a module
# attribute that deployments set via env and tests retune at runtime, and a
# from-import would freeze a copy here at import time — two layers onto one
# store file would then wait different lengths, a difference nobody chose.
from lionagi.state import engine as _state_engine

_log = logging.getLogger(__name__)

_ACTIVE_CONNECTIONS: int = 0


def get_active_connection_count() -> int:
    return _ACTIVE_CONNECTIONS


def store_path() -> str:
    """The store file these services open. Resolves ``LIONAGI_STATE_DB_URL``
    rather than naming ``DEFAULT_DB_PATH`` directly, so a route reads the
    same file the daemon actually opens. Falls back to the default path when
    the configured store is server-backed (no file to name) -- equally wrong
    for that deployment, but tracked as a separate, route-level concern
    rather than a path-resolution one."""
    from lionagi.state import db as db_mod

    path = db_mod.state_db_file()
    return str(path if path is not None else db_mod.DEFAULT_DB_PATH)


def store_exists() -> bool:
    """Whether the store file is there to read -- stays in step with
    :func:`store_path` by construction, so a guard and the connection it
    protects can't disagree about which store is in play."""
    from pathlib import Path

    return Path(store_path()).exists()


class StoreNotAddressableError(RuntimeError):
    """The configured store has no file this layer can open. Raised by
    :func:`require_file_store` for a route reading/writing straight through
    a SQLite connection -- a server-backed or in-memory store has no file
    behind it, so ``store_path()``'s fallback would read/write a file the
    daemon never serves."""

    def __init__(self, backend: str) -> None:
        self.backend = backend
        super().__init__(
            f"this route reads from a local SQLite file, but the configured "
            f"store is {backend}-backed and has no such file"
        )


def require_file_store() -> None:
    """Raise :class:`StoreNotAddressableError` when the configured store is
    not a SQLite file this layer can open directly. Slots in front of a
    route's existing ``if not store_exists(): return []`` guard: a path that
    exists or is merely absent both pass through unchanged (absent still
    means "no store yet"); only a resolution with no path at all (server
    URL, or ``:memory:``) raises."""
    from lionagi.state import db as db_mod

    if db_mod.state_db_file() is not None:
        return

    from lionagi.state.engine import dialect_of, normalize_state_db_url

    raw = db_mod.settings.LIONAGI_STATE_DB_URL
    if raw is None:
        raw = db_mod.DEFAULT_DB_PATH
    url = normalize_state_db_url(raw)
    dialect = dialect_of(url)
    from sqlalchemy.engine import make_url

    database = make_url(url).database
    backend = "in-memory sqlite" if (not database or database == ":memory:") else dialect
    raise StoreNotAddressableError(backend)


@asynccontextmanager
async def open_db(path: str) -> AsyncIterator[aiosqlite.Connection]:
    """Studio-local SQLite connection with WAL mode and a busy timeout,
    preventing "database is locked" errors under modest concurrency.

    The timeout is the same value the StateDB engine applies, imported rather
    than restated: these are two connection layers onto one store file, and a
    lock wait that differs between them is a difference nobody chose.
    """
    global _ACTIVE_CONNECTIONS
    # Announced here as well as in make_engine because this is a second,
    # independent way into the store: a process that only ever opens
    # connections this way would otherwise never say which timeout it uses.
    # The announcement is once per process, so two call sites is one line.
    _state_engine.announce_busy_timeout()
    async with aiosqlite.connect(path) as db:
        _ACTIVE_CONNECTIONS += 1
        try:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute(f"PRAGMA busy_timeout = {_state_engine.SQLITE_BUSY_TIMEOUT_MS}")
            await db.execute("PRAGMA foreign_keys = ON")
            db.row_factory = aiosqlite.Row
            yield db
        finally:
            _ACTIVE_CONNECTIONS -= 1


async def table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    """Columns the store actually has on a table, as it is on disk right now.

    These connections read the store; they never migrate it. A store last
    written by an older version therefore keeps that version's shape for as
    long as nothing opens it for writing, which is the state of every store
    immediately after an upgrade and the permanent state of one this process
    can only read. A read that names a column added by a later version has to
    ask whether it is there, because SQLite rejects the statement outright
    rather than returning NULL for the column that is missing.
    """
    cur = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}
