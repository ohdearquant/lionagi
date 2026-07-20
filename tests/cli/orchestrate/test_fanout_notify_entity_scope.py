# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li o fanout --notify` remains session-scoped with `--invocation`."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from lionagi.cli.orchestrate import fanout as fanout_module

from .test_fanout_artifacts import _fanout_env


async def test_notify_scope_is_session_even_with_invocation_id(tmp_path, monkeypatch):
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
