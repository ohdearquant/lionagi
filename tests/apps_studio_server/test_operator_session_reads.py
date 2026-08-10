# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Focused tests for the session-facing Operator read tools: list_sessions,
session_detail, and session_signals."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

import pytest

import lionagi.state.db as state_db_mod

aiosqlite = pytest.importorskip("aiosqlite", reason="aiosqlite not installed")
fastapi = pytest.importorskip("fastapi", reason="studio extra not installed")

from lionagi.state.db import StateDB  # noqa: E402

pytestmark = pytest.mark.asyncio

# A github-token-shaped string: matches the shape-based secret pattern the
# redaction layer scrubs regardless of which dict key carries it, so it
# stands in for a real credential leaking into free text.
SECRET_VALUE = "ghp_zQ9xT7mK2vL8pR4wS6nB3cF5dH1"


async def seed_session(
    db_path: Path,
    *,
    session_id: str,
    status: str = "completed",
    name: str | None = None,
    project: str | None = None,
) -> None:
    prog_id = f"{session_id}-prog"
    async with StateDB(db_path) as db:
        await db.create_progression(prog_id)
        await db.create_session(
            {
                "id": session_id,
                "progression_id": prog_id,
                "name": name or f"run-{session_id}",
                "status": status,
                "project": project,
                "updated_at": time.time(),
                "invocation_kind": "agent",
                "source_kind": "live",
            }
        )


async def seed_branch(
    db_path: Path,
    *,
    branch_id: str,
    session_id: str,
    name: str = "worker",
) -> None:
    prog_id = f"{branch_id}-prog"
    async with StateDB(db_path) as db:
        await db.create_progression(prog_id)
        await db.create_branch(
            {
                "id": branch_id,
                "created_at": 200.0,
                "name": name,
                "session_id": session_id,
                "progression_id": prog_id,
                "model": "gpt-5",
                "provider": "openai",
                "agent_name": name,
            }
        )


async def seed_text_message(
    db_path: Path,
    *,
    branch_id: str,
    message_id: str,
    role: str,
    content: dict[str, Any],
    timestamp: float = 100.0,
) -> None:
    async with StateDB(db_path) as db:
        await db.insert_message(
            {
                "id": message_id,
                "created_at": timestamp,
                "role": role,
                "sender": "system",
                "content": content,
            }
        )
        branch = await db.get_branch(branch_id)
        prog_id = branch["progression_id"]
        existing = await db.get_progression(prog_id)
        await db.set_progression(prog_id, [*existing, message_id])


async def seed_signal(
    db_path: Path,
    *,
    session_id: str,
    kind: str,
    payload: dict[str, Any],
    ts: float = 100.0,
    op_id: str = "op-1",
) -> None:
    async with StateDB(db_path) as db:
        await db.insert_session_signal(
            session_id=session_id, kind=kind, op_id=op_id, ts=ts, payload=payload
        )


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: Any) -> Path:
    path = tmp_path / "state.db"
    monkeypatch.setattr(state_db_mod, "DEFAULT_DB_PATH", path)
    return path


# --------------------------------------------------------------------------
# list_sessions
# --------------------------------------------------------------------------


async def test_list_sessions_happy_path(db_path):
    from lionagi.studio.operator.application_mcp import list_sessions

    sid_a = str(uuid.uuid4())
    sid_b = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid_a, status="completed", project="alpha")
    await seed_session(db_path, session_id=sid_b, status="failed", project="beta")

    result = await list_sessions({"status": "completed"})

    assert result["source"] == "store"
    assert result["truncated"] is False
    ids = {row["id"] for row in result["sessions"]}
    assert ids == {sid_a}
    assert result["sessions"][0]["project"] == "alpha"


async def test_list_sessions_row_cap_reports_truncation(db_path):
    from lionagi.studio.operator.application_mcp import list_sessions

    session_ids = [str(uuid.uuid4()) for _ in range(3)]
    for sid in session_ids:
        await seed_session(db_path, session_id=sid, status="completed")

    result = await list_sessions({"limit": 1})

    assert result["total"] == 3
    assert len(result["sessions"]) == 1
    assert result["truncated"] is True


async def test_list_sessions_redacts_secret_shaped_name(db_path):
    from lionagi.studio.operator.application_mcp import list_sessions
    from lionagi.studio.services import runs as runs_service

    sid = str(uuid.uuid4())
    await seed_session(
        db_path,
        session_id=sid,
        status="completed",
        name=f"deploy using {SECRET_VALUE}",
    )

    # Positive control: the same value read straight from the carrier this
    # tool adapts is present verbatim, so its absence below is the
    # redaction layer's doing, not an accident of the fixture data.
    raw_rows = await runs_service.list_runs(status="completed")
    assert any(SECRET_VALUE in str(row) for row in raw_rows)

    result = await list_sessions({"status": "completed"})

    assert SECRET_VALUE not in str(result)


# --------------------------------------------------------------------------
# session_detail
# --------------------------------------------------------------------------


async def test_session_detail_happy_path(db_path):
    from lionagi.studio.operator.application_mcp import session_detail

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="completed", project="alpha")
    branch_id = f"{sid}-br1"
    await seed_branch(db_path, branch_id=branch_id, session_id=sid, name="worker")
    await seed_text_message(
        db_path,
        branch_id=branch_id,
        message_id=f"{sid}-m1",
        role="assistant",
        content={"assistant_response": "done"},
    )

    result = await session_detail({"session_id": sid})

    assert result["known"] is True
    assert result["source"] == "store"
    assert result["id"] == sid
    assert len(result["branches"]) == 1
    assert result["branches"][0]["messages"][0]["content"] == {"assistant_response": "done"}


async def test_session_detail_unknown_session(db_path):
    from lionagi.studio.operator.application_mcp import session_detail

    async with StateDB(db_path):
        pass

    result = await session_detail({"session_id": str(uuid.uuid4())})

    assert result["known"] is False


async def test_session_detail_message_limit_caps_and_reports_truncation(db_path):
    from lionagi.studio.operator.application_mcp import session_detail

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="completed")
    branch_id = f"{sid}-br1"
    await seed_branch(db_path, branch_id=branch_id, session_id=sid, name="worker")
    for i in range(3):
        await seed_text_message(
            db_path,
            branch_id=branch_id,
            message_id=f"{sid}-m{i}",
            role="assistant",
            content={"assistant_response": f"step {i}"},
            timestamp=100.0 + i,
        )

    result = await session_detail({"session_id": sid, "message_limit": 1})

    branch = result["branches"][0]
    assert len(branch["messages"]) == 1
    assert branch["messages_truncated"] is True


async def test_session_detail_redacts_secret_shaped_message_content(db_path):
    from lionagi.studio.operator.application_mcp import session_detail
    from lionagi.studio.services import sessions as sessions_service

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="completed")
    branch_id = f"{sid}-br1"
    await seed_branch(db_path, branch_id=branch_id, session_id=sid, name="deployer")
    await seed_text_message(
        db_path,
        branch_id=branch_id,
        message_id=f"{sid}-m1",
        role="assistant",
        content={"assistant_response": f"used token {SECRET_VALUE} to deploy"},
    )

    # Positive control: the unadapted carrier still carries the raw value.
    raw = await sessions_service.get_session(sid)
    assert SECRET_VALUE in str(raw)

    result = await session_detail({"session_id": sid})

    assert SECRET_VALUE not in str(result)


async def test_session_detail_falls_back_on_invalid_cursor(db_path):
    from lionagi.studio.operator.application_mcp import session_detail

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="completed")
    branch_id = f"{sid}-br1"
    await seed_branch(db_path, branch_id=branch_id, session_id=sid, name="worker")
    await seed_text_message(
        db_path,
        branch_id=branch_id,
        message_id=f"{sid}-m1",
        role="assistant",
        content={"assistant_response": "done"},
    )

    result = await session_detail({"session_id": sid, "message_cursor": "not-a-real-cursor"})

    assert result["known"] is True
    assert result["source"] == "fallback"


# --------------------------------------------------------------------------
# session_signals
# --------------------------------------------------------------------------


async def test_session_signals_happy_path(db_path):
    from lionagi.studio.operator.application_mcp import session_signals

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="running")
    await seed_signal(db_path, session_id=sid, kind="NodeStarted", payload={"name": "plan"}, ts=1.0)
    await seed_signal(
        db_path, session_id=sid, kind="NodeCompleted", payload={"name": "plan"}, ts=2.0
    )

    result = await session_signals({"session_id": sid})

    assert result["known"] is True
    assert result["source"] == "store"
    assert result["truncated"] is False
    assert [row["kind"] for row in result["signals"]] == ["NodeStarted", "NodeCompleted"]


async def test_session_signals_limit_caps_and_reports_truncation(db_path):
    from lionagi.studio.operator.application_mcp import session_signals

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="running")
    for i in range(3):
        await seed_signal(
            db_path, session_id=sid, kind="NodeStarted", payload={"name": f"step-{i}"}, ts=float(i)
        )

    result = await session_signals({"session_id": sid, "limit": 1})

    assert len(result["signals"]) == 1
    assert result["truncated"] is True


async def test_session_signals_redacts_secret_shaped_payload_value(db_path):
    from lionagi.studio.operator.application_mcp import session_signals
    from lionagi.studio.services import signals as signals_service

    sid = str(uuid.uuid4())
    await seed_session(db_path, session_id=sid, status="running")
    await seed_signal(
        db_path,
        session_id=sid,
        kind="NodeCompleted",
        payload={"note": f"authenticated with {SECRET_VALUE}"},
        ts=1.0,
    )

    # Positive control: the unadapted carrier still carries the raw value.
    raw_rows = await signals_service.get_signals_after(sid, 0)
    assert any(SECRET_VALUE in str(row) for row in raw_rows)

    result = await session_signals({"session_id": sid})

    assert SECRET_VALUE not in str(result)
