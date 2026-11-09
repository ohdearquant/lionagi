from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable
from pathlib import Path
from typing import TypeVar

import lionagi.state.db as state_db_mod
from lionagi.state.db import StateDB

T = TypeVar("T")


def run_async(coro: Awaitable[T]) -> T:
    """Run a coroutine synchronously in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


_run = run_async


def _configure(monkeypatch, *, default: Path, url: str | None) -> None:
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", default)
    monkeypatch.setattr(
        state_db_mod,
        "settings",
        state_db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": url}),
    )


async def _seed(db_path: Path, name: str, count: int = 1) -> list[str]:
    ids = []
    async with StateDB(db_path) as db:
        for i in range(count):
            session_id = str(uuid.uuid4())
            pid = str(uuid.uuid4())
            await db.create_progression(pid)
            await db.create_session(
                {
                    "id": session_id,
                    "progression_id": pid,
                    "name": name if count == 1 else f"{name}-{i}",
                    "status": "completed",
                    "started_at": time.time(),
                }
            )
            ids.append(session_id)
    return ids


ENV = "LIONAGI_SQLITE_BUSY_TIMEOUT_MS"
