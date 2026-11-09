# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Shared database setup for lifecycle lookup and CLI status contracts."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from lionagi.state.db import StateDB


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test temp DB; patch DEFAULT_DB_PATH so StateDB() opens it.

    Also neutralizes LIONAGI_STATE_DB_URL, which StateDB prefers over
    DEFAULT_DB_PATH — a host env pointing at a real DB would otherwise
    bypass the patch entirely. AppSettings is frozen, so swap the module
    reference rather than mutating the instance.
    """
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(
        "lionagi.state.db.settings",
        SimpleNamespace(LIONAGI_STATE_DB_URL=None),
    )
    return db_path


async def _make_session(
    db: StateDB,
    *,
    status: str = "running",
    project: str | None = None,
    invocation_kind: str | None = "agent",
    model: str | None = "claude-3-5-sonnet",
    provider: str | None = "anthropic",
    invocation_id: str | None = None,
    source_kind: str | None = None,
) -> str:
    sid = uuid.uuid4().hex[:12]
    pid = uuid.uuid4().hex
    await db.create_progression(pid)
    await db.create_session(
        {
            "id": sid,
            "progression_id": pid,
            "status": status,
            "invocation_kind": invocation_kind,
            "project": project,
            "model": model,
            "provider": provider,
            "started_at": time.time(),
            "invocation_id": invocation_id,
            "source_kind": source_kind,
        }
    )
    return sid


async def _make_invocation(db: StateDB, *, status: str = "running", skill: str = "show") -> str:
    inv_id = uuid.uuid4().hex[:12]
    await db.create_invocation(
        {
            "id": inv_id,
            "skill": skill,
            "started_at": time.time(),
            "status": status,
        }
    )
    return inv_id


async def _make_show(db: StateDB, *, status: str = "active", topic: str = "test-topic") -> str:
    show_id = uuid.uuid4().hex[:12]
    await db.create_show(
        {
            "id": show_id,
            "topic": topic,
            "status": status,
            "show_dir": "/tmp/show",
        }
    )
    return show_id


async def _make_play(
    db: StateDB,
    show_id: str,
    *,
    status: str = "running",
    name: str = "play-1",
    session_id: str | None = None,
) -> str:
    play_id = uuid.uuid4().hex[:12]
    await db.create_play(
        {
            "id": play_id,
            "show_id": show_id,
            "name": name,
            "status": status,
            "started_at": time.time(),
            "session_id": session_id,
        }
    )
    return play_id


async def _make_branch(db: StateDB, session_id: str, *, branch_id: str | None = None) -> str:
    """Create a branch row tied to *session_id* — the resume token surfaced
    as `branch_id` in the status view and printed in `li agent -r` hints."""
    bid = branch_id or str(uuid.uuid4())
    pid = uuid.uuid4().hex
    await db.create_progression(pid)
    await db.create_branch(
        {
            "id": bid,
            "session_id": session_id,
            "progression_id": pid,
            "model": "claude-3-5-sonnet",
        }
    )
    return bid


async def _seed_session_with_id(db: StateDB, sid: str) -> str:
    prog_id = str(uuid.uuid4())
    await db.create_progression(prog_id)
    await db.create_session(
        {
            "id": sid,
            "progression_id": prog_id,
            "status": "running",
            "invocation_kind": "agent",
            "started_at": time.time(),
        }
    )
    return sid
