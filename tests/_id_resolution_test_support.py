# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Shared database setup for entity-resolution contracts."""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from lionagi.state.db import StateDB

SHARED = "abc123"
FIRST = f"{SHARED}00-0000-4000-8000-000000000001"
SECOND = f"{SHARED}00-0000-4000-8000-000000000002"


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", path)
    return path


async def _seed_session(db: StateDB, session_id: str) -> None:
    prog_id = str(uuid.uuid4())
    await db.create_progression(prog_id)
    await db.create_session(
        {
            "id": session_id,
            "progression_id": prog_id,
            "status": "running",
            "started_at": time.time(),
        }
    )


async def _seed_invocation(db: StateDB, invocation_id: str) -> None:
    await db.create_invocation(
        {
            "id": invocation_id,
            "skill": "test",
            "started_at": time.time(),
            "status": "running",
        }
    )
