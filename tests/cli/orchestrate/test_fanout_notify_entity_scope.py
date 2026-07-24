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


async def test_settings_notify_outcome_scope_registered_without_notify_flag(tmp_path, monkeypatch):
    """Without `--notify`, `_run_fanout` must still bind this run into the
    settings-driven notify.on_terminal outcome scope, so a configured exec
    adapter's result lands in this run's own notify_outcome.json instead of
    being handled by the process-wide no-op registration."""
    env, run, _session = _fanout_env(tmp_path)

    monkeypatch.setattr(fanout_module, "setup_orchestration", AsyncMock(return_value=env))
    monkeypatch.setattr(fanout_module, "start_live_persist", AsyncMock())
    monkeypatch.setattr(
        fanout_module,
        "stop_live_persist",
        AsyncMock(side_effect=lambda env, status: status),
    )
    monkeypatch.setattr(fanout_module, "plan", AsyncMock(return_value=[]))

    captured: dict[str, Any] = {}
    unregistered: list[Any] = []

    def fake_register_outcome(run_arg, **kwargs: Any) -> str:
        captured["run"] = run_arg
        captured.update(kwargs)
        return "outcome-scope-name"

    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.register_run_notify_outcome_scope",
        fake_register_outcome,
    )
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.unregister_run_notify_outcome_scope",
        lambda name, **kw: unregistered.append(name),
    )

    result, status = await fanout_module._run_fanout(
        "codex/model",
        "prompt",
    )

    assert status == "completed"
    assert captured["run"] is run
    assert captured["entity_kind"] == "session"
    assert captured["entity_id"] == str(env.session.id)
    assert unregistered == ["outcome-scope-name"]


async def test_notify_flag_skips_settings_outcome_scope(tmp_path, monkeypatch):
    """`--notify` already owns this entity as an exclusive override, so the
    settings-driven outcome scope must not also be registered (it would
    otherwise fire the configured adapter twice for one terminal transition)."""
    env, _run, _session = _fanout_env(tmp_path)

    monkeypatch.setattr(fanout_module, "setup_orchestration", AsyncMock(return_value=env))
    monkeypatch.setattr(fanout_module, "start_live_persist", AsyncMock())
    monkeypatch.setattr(
        fanout_module,
        "stop_live_persist",
        AsyncMock(side_effect=lambda env, status: status),
    )
    monkeypatch.setattr(fanout_module, "plan", AsyncMock(return_value=[]))
    monkeypatch.setattr(fanout_module, "register_flow_notify_scope", lambda **kw: "scope-name")
    monkeypatch.setattr(fanout_module, "unregister_flow_notify_scope", lambda *a, **kw: None)

    calls: list[Any] = []
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.register_run_notify_outcome_scope",
        lambda *a, **kw: calls.append((a, kw)) or "outcome-scope-name",
    )

    result, status = await fanout_module._run_fanout(
        "codex/model",
        "prompt",
        notify="fan-hook {status}",
    )

    assert status == "completed"
    assert calls == []
