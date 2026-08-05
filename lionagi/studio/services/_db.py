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


class StoreNotAddressableError(RuntimeError):
    """The configured store has no file this layer can open.

    Raised by :func:`require_file_store` for a route that reads or writes rows
    straight through a SQLite connection. A server-backed store (or an
    in-memory one) has no file behind it, so ``store_path()`` would have to
    fall back to a path the daemon never writes -- reading it back would
    report a store nobody is serving, and connecting to write would create a
    file whose rows nothing else will ever see. The route has no honest answer
    against that connection, and this says so instead of giving one.
    """

    def __init__(self, backend: str) -> None:
        self.backend = backend
        super().__init__(
            f"this route reads from a local SQLite file, but the configured "
            f"store is {backend}-backed and has no such file"
        )


def require_file_store() -> None:
    """Raise :class:`StoreNotAddressableError` when the configured store is
    not a SQLite file this layer can open directly.

    Call this where a route or service function currently guards with
    ``if not store_exists(): return []`` (or opens the connection
    unconditionally): it slots in front of that check. A path that exists or a
    path that is merely absent both pass through unchanged -- the second case
    still means "no store yet", answered the same empty way as before. Only a
    resolution with no path at all (a server URL, or ``:memory:``) raises,
    because that is the one condition this layer cannot ever satisfy by
    waiting or by creating the file.
    """
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
