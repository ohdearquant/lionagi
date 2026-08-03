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
