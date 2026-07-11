# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite

_log = logging.getLogger(__name__)

_ACTIVE_CONNECTIONS: int = 0


def get_active_connection_count() -> int:
    return _ACTIVE_CONNECTIONS


@asynccontextmanager
async def open_db(path: str) -> AsyncIterator[aiosqlite.Connection]:
    """Studio-local SQLite connection with WAL mode and busy_timeout.

    WAL journal mode + busy_timeout = 5000 ms prevents "database is locked"
    errors under modest concurrency from concurrent readers and the single writer.
    """
    global _ACTIVE_CONNECTIONS
    async with aiosqlite.connect(path) as db:
        _ACTIVE_CONNECTIONS += 1
        try:
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA busy_timeout = 5000")
            await db.execute("PRAGMA foreign_keys = ON")
            db.row_factory = aiosqlite.Row
            yield db
        finally:
            _ACTIVE_CONNECTIONS -= 1
