# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li o fanout --notify` remains session-scoped with `--invocation`."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from lionagi.cli.orchestrate import fanout as fanout_module
from lionagi.state.db import StateDB

from .test_fanout_artifacts import _fanout_env


async def test_notify_scope_is_session_even_with_invocation_id(tmp_path, monkeypatch):
    # `--invocation` is validated against the recorded invocations before setup,
    # so the id has to name a real row for the run to be reached at all.
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr(
        "lionagi.state.db.settings", type("S", (), {"LIONAGI_STATE_DB_URL": None})()
    )
    async with StateDB(db_path) as db:
        await db.create_invocation({"id": "parent-inv-456", "skill": "fanout", "started_at": 1.0})
    env, _run, _session = _fanout_env(tmp_path)

    monkeypatch.setattr(fanout_module, "setup_orchestration", AsyncMock(return_value=env))
    monkeypatch.setattr(fanout_module, "start_live_persist", AsyncMock())
    monkeypatch.setattr(
        fanout_module,
        "stop_live_persist",
        AsyncMock(side_effect=lambda env, status: status),
    )
    # Empty assignments short-circuits _run_fanout_inner before any worker/DAG
    # machinery runs — the minimal path to a clean "completed" terminal status.
    monkeypatch.setattr(fanout_module, "plan", AsyncMock(return_value=[]))

    captured: dict[str, Any] = {}

    def fake_register(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "scope-name"

    monkeypatch.setattr(fanout_module, "register_flow_notify_scope", fake_register)
    monkeypatch.setattr(fanout_module, "unregister_flow_notify_scope", lambda *a, **kw: None)

    result, status = await fanout_module._run_fanout(
        "codex/model",
        "prompt",
        invocation_id="parent-inv-456",
        notify="fan-hook {status}",
    )

    assert status == "completed"
    assert captured["entity_kind"] == "session"
    assert captured["entity_id"] == str(env.session.id)
    assert captured["invocation_id"] == "parent-inv-456"
