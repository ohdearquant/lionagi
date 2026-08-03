# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite

from lionagi.state.engine import SQLITE_BUSY_TIMEOUT_MS

_log = logging.getLogger(__name__)

_ACTIVE_CONNECTIONS: int = 0


def get_active_connection_count() -> int:
    return _ACTIVE_CONNECTIONS


def store_path() -> str:
    """The store file these services open.

    Every service here used to name ``DEFAULT_DB_PATH`` directly, which is the
    right file exactly when nothing has moved it. With
    ``LIONAGI_STATE_DB_URL`` pointing at another file, a route would read a
    database the daemon never opens and report on rows nobody is serving, and
    ``aiosqlite`` would create that unrelated file on connect if it were not
    already there.

    This layer talks to SQLite directly, so the only store it can reach is one
    with a file behind it. When the configured store is a server, there is no
    file to name and this falls back to the default path, which is what these
    services did before and is equally wrong for that deployment: a
    server-backed store has rows no SQLite connection here can reach. What a
    route should answer in that case is a question about the route's contract,
    not about path resolution, and it is tracked separately.
    """
    from lionagi.state import db as db_mod

    path = db_mod.state_db_file()
    return str(path if path is not None else db_mod.DEFAULT_DB_PATH)


def store_exists() -> bool:
    """Whether the store file is there to read.

    The direct analogue of the ``DEFAULT_DB_PATH.exists()`` checks these
    services used to make, asked about the file they will actually open. It
    stays in step with :func:`store_path` by construction, so a guard and the
    connection it protects cannot disagree about which store is in play.
    """
    from pathlib import Path

    return Path(store_path()).exists()


@asynccontextmanager
async def open_db(path: str) -> AsyncIterator[aiosqlite.Connection]:
    """Studio-local SQLite connection with WAL mode and a busy timeout,
    preventing "database is locked" errors under modest concurrency.

    The timeout is the same value the StateDB engine applies, imported rather
    than restated: these are two connection layers onto one store file, and a
    lock wait that differs between them is a difference nobody chose.
    """
    global _ACTIVE_CONNECTIONS
    async with aiosqlite.connect(path) as db:
        _ACTIVE_CONNECTIONS += 1
        try:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
            await db.execute("PRAGMA foreign_keys = ON")
            db.row_factory = aiosqlite.Row
            yield db
        finally:
            _ACTIVE_CONNECTIONS -= 1
