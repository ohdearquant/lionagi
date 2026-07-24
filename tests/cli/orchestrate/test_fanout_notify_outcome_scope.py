# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li o fanout` binds a settings-driven notify.on_terminal exec adapter's
outcome to the fan-out run directory, independently of `--notify` -- see
lionagi/state/lifecycle/notify_settings.py's register_run_notify_outcome_scope
contract. fanout.py imports register_/unregister_run_notify_outcome_scope
locally inside `_run_fanout`, so patches target the source module
(`notify_settings`), not `fanout_module`."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import lionagi.state.lifecycle.notify_settings as notify_settings_mod
from lionagi.cli.orchestrate import fanout as fanout_module

from .test_fanout_artifacts import _fanout_env


def _patch_fanout_scaffold(monkeypatch, env) -> None:
    monkeypatch.setattr(fanout_module, "setup_orchestration", AsyncMock(return_value=env))
    monkeypatch.setattr(fanout_module, "start_live_persist", AsyncMock())
    monkeypatch.setattr(
        fanout_module,
        "stop_live_persist",
        AsyncMock(side_effect=lambda env, status: status),
    )
    # Empty assignments short-circuits _run_fanout_inner before any
    # worker/DAG machinery runs -- the minimal path to "completed".
    monkeypatch.setattr(fanout_module, "plan", AsyncMock(return_value=[]))


async def test_settings_outcome_scope_registered_independently_of_notify_flag(
    tmp_path, monkeypatch
):
    """The missing binding: without `--notify`, `_run_fanout` must still
    scope the settings-driven notify.on_terminal adapter's outcome to this
    run, so a late-arriving adapter result lands in the run directory
    instead of nowhere."""
    env, run, _session = _fanout_env(tmp_path)
    _patch_fanout_scaffold(monkeypatch, env)

    captured: dict[str, Any] = {}

    def fake_register(run_arg, **kwargs: Any) -> str:
        captured["run"] = run_arg
        captured.update(kwargs)
        return "outcome-scope-name"

    fake_unregister = MagicMock()
    monkeypatch.setattr(notify_settings_mod, "register_run_notify_outcome_scope", fake_register)
    monkeypatch.setattr(notify_settings_mod, "unregister_run_notify_outcome_scope", fake_unregister)

    result, status = await fanout_module._run_fanout(
        "codex/model",
        "prompt",
        cwd="/some/project",
    )

    assert status == "completed"
    assert captured["run"] is run
    assert captured["entity_kind"] == "session"
    assert captured["entity_id"] == str(env.session.id)
    assert captured["project_dir"] == "/some/project"
    fake_unregister.assert_called_once_with("outcome-scope-name")


async def test_notify_flag_skips_settings_outcome_scope_to_avoid_double_fire(tmp_path, monkeypatch):
    """When `--notify` already owns this run's session entity as an
    exclusive override, a second override for the settings-driven outcome
    scope must not also be registered -- registering both would fire the
    adapter twice for the same terminal event (see register_flow_notify_scope
    vs. register_run_notify_outcome_scope: both register(..., override=True)
    for the same entity_kind/entity_id, and TerminalCallbackRegistry.emit()
    runs every override match concurrently)."""
    env, _run, _session = _fanout_env(tmp_path)
    _patch_fanout_scaffold(monkeypatch, env)
    monkeypatch.setattr(fanout_module, "register_flow_notify_scope", lambda **kw: "flow-scope")
    monkeypatch.setattr(fanout_module, "unregister_flow_notify_scope", lambda *a, **kw: None)

    called = False

    def fake_register(*args, **kwargs):
        nonlocal called
        called = True
        return "outcome-scope-name"

    monkeypatch.setattr(notify_settings_mod, "register_run_notify_outcome_scope", fake_register)
    monkeypatch.setattr(
        notify_settings_mod, "unregister_run_notify_outcome_scope", lambda *a, **kw: None
    )

    result, status = await fanout_module._run_fanout(
        "codex/model",
        "prompt",
        notify="some-hook {status}",
    )

    assert status == "completed"
    assert called is False
