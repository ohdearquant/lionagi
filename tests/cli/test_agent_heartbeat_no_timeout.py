# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A leg spawned without --timeout must still surface the steer receipt ack.

Before this fix, the heartbeat loop (and the "steer queued — lands at end of
current turn" ack it carries) only started when --timeout was set, because it
lived inside `if timeout is not None:`. An operator steering an untimed leg
had no way to tell "received, will apply at turn end" from "lost" until the
turn actually ended.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lionagi.state.db import StateDB


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


async def _make_running_agent_session(db: StateDB) -> str:
    sid = uuid.uuid4().hex[:12]
    pid = uuid.uuid4().hex
    await db.create_progression(pid)
    await db.create_session(
        {
            "id": sid,
            "progression_id": pid,
            "status": "running",
            "invocation_kind": "agent",
            "run_id": "20260801T060606-hbrun",
        }
    )
    return sid


def _wire_agent_stubs(
    monkeypatch, tmp_path: Path, *, sid: str, live_db: StateDB, operate_delay: float
):
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.manager import iModelManager

    async def fake_operate(self, instruction=None, **kw):
        await asyncio.sleep(operate_delay)
        return "done"

    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)
    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "claude_code/sonnet")

    async def fake_setup(*a, **kw):
        return {"session_id": sid, "db": live_db}

    async def fake_teardown(
        ctx,
        *,
        status="completed",
        exception=None,
        cwd=None,
        engine_session_uid=None,
        defer_terminal=False,
    ):
        return status

    monkeypatch.setattr(agent_mod, "setup_agent_persist", fake_setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", fake_teardown)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}",
            agent_definition_hash=lambda n: "abc",
        ),
    )
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)
    monkeypatch.setattr(
        agent_mod,
        "allocate_run",
        lambda: SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "artifacts",
            stream_dir=tmp_path / "stream",
            branches_dir=tmp_path / "branches",
        ),
    )
    # Shrink the tick so the test doesn't wait out a real 60s heartbeat.
    # raising=False: the constant does not exist pre-fix, and this helper is
    # also used to demonstrate the pre-fix failure (an AttributeError here
    # would mask the actual defect being tested).
    monkeypatch.setattr(agent_mod, "_HEARTBEAT_INTERVAL_S", 0.01, raising=False)


@pytest.mark.asyncio
async def test_steer_receipt_fires_without_timeout(temp_db_path, monkeypatch, tmp_path):
    async with StateDB() as db:
        sid = await _make_running_agent_session(db)
        await db.insert_session_control(
            session_id=sid, verb="message", payload={"text": "redirect"}
        )

    hints: list[str] = []

    async with StateDB() as live_db:
        _wire_agent_stubs(monkeypatch, tmp_path, sid=sid, live_db=live_db, operate_delay=0.08)

        import lionagi.cli.agent as agent_mod

        monkeypatch.setattr(agent_mod, "hint", lambda line: hints.append(line))

        from lionagi.cli.agent import _run_agent

        await _run_agent("claude_code/sonnet", "hello", timeout=None)

    assert any("steer queued x1" in h and "lands at end of current turn" in h for h in hints), (
        f"no steer receipt ack observed in heartbeat lines: {hints!r}"
    )


@pytest.mark.asyncio
async def test_progress_heartbeat_fires_without_timeout_even_with_no_steer(
    temp_db_path, monkeypatch, tmp_path
):
    """Control: the plain progress line (no queued steer) must also fire
    without --timeout — this is the general "receipt path armed" claim, not
    just the steer-specific ack."""
    async with StateDB() as db:
        sid = await _make_running_agent_session(db)

    hints: list[str] = []

    async with StateDB() as live_db:
        _wire_agent_stubs(monkeypatch, tmp_path, sid=sid, live_db=live_db, operate_delay=0.08)

        import lionagi.cli.agent as agent_mod

        monkeypatch.setattr(agent_mod, "hint", lambda line: hints.append(line))

        from lionagi.cli.agent import _run_agent

        await _run_agent("claude_code/sonnet", "hello", timeout=None)

    assert any(h.startswith("[progress]") for h in hints), (
        f"no progress heartbeat observed without --timeout: {hints!r}"
    )
