# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A session read reports when its branches last had activity.

The message page is bounded, so the timestamps that describe the whole
history cannot be read off it: a branch whose newest message falls outside the
page would report no activity at all. Stall detection reads these fields, so
their absence has to mean "nothing was ever recorded", never "not in the
page".
"""

from __future__ import annotations

from pathlib import Path

import pytest

import lionagi.state.db as state_db_mod

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")


@pytest.fixture
def store(monkeypatch, tmp_path: Path) -> Path:
    import lionagi.studio.services.sessions as sessions_mod

    db_path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(sessions_mod, "store_path", lambda: str(db_path))
    return db_path


async def _seed(db_path: Path, *, messages: int) -> tuple[str, str]:
    from lionagi.state.db import StateDB

    session_id = "11111111-1111-1111-1111-111111111111"
    branch_id = f"{session_id}-br1"
    msg_ids = [f"m{i:03d}" for i in range(messages)]
    async with StateDB(db_path) as db:
        await db.create_progression(f"{branch_id}-prog", msg_ids)
        await db.create_progression(f"{session_id}-prog")
        await db.create_session(
            {
                "id": session_id,
                "progression_id": f"{session_id}-prog",
                "name": "run",
                "status": "running",
                "started_at": 1000.0,
                "invocation_kind": "flow",
                "source_kind": "live",
            }
        )
        await db.create_branch(
            {
                "id": branch_id,
                "created_at": 1000.0,
                "name": "write",
                "session_id": session_id,
                "progression_id": f"{branch_id}-prog",
                "agent_name": "write",
                "status": "running",
                "started_at": 1000.0,
            }
        )
        for index, msg_id in enumerate(msg_ids):
            await db.insert_message(
                {
                    "id": msg_id,
                    "created_at": float(index + 1),
                    "content": {"text": "working"},
                    "sender": "worker",
                    "recipient": "user",
                    "role": "assistant",
                    "node_metadata": {},
                }
            )
    return session_id, branch_id


async def test_branch_activity_spans_the_history_not_only_the_page(store):
    from lionagi.studio.services import sessions as sessions_mod

    session_id, branch_id = await _seed(store, messages=3)

    session = await sessions_mod.get_session(session_id, message_limit=1)

    branch = next(b for b in session["branches"] if b["id"] == branch_id)
    assert branch["first_message_at"] == 1.0
    assert branch["last_message_at"] == 3.0
    assert branch["message_total"] == 3


async def test_a_session_with_no_messages_reports_no_activity(store):
    from lionagi.studio.services import sessions as sessions_mod

    session_id, branch_id = await _seed(store, messages=0)

    session = await sessions_mod.get_session(session_id)

    branch = next(b for b in session["branches"] if b["id"] == branch_id)
    assert branch["first_message_at"] is None
    assert branch["last_message_at"] is None
