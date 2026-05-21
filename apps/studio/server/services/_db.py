# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiosqlite

_log = logging.getLogger(__name__)


@asynccontextmanager
async def open_db(path: str) -> AsyncIterator[aiosqlite.Connection]:
    """Studio-local SQLite connection with WAL mode and busy_timeout.

    Always enables WAL journal mode and sets busy_timeout = 5000 ms so
    concurrent readers and the single writer do not immediately receive
    "database is locked" under modest concurrency (#992).
    """
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA busy_timeout = 5000")
        db.row_factory = aiosqlite.Row
        yield db
