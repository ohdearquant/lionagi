# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li agent --notify`: parses via the shared common args, defaults to None, and
threads through to _run_agent so a per-invocation terminal-notify override reaches
the run scope (mirrors the `li o flow` / `li o fanout` wiring)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tests.cli.test_agent_resume_on_timeout import _wire_agent_stubs

_CAPTURED: dict[str, Any] = {}


async def _fake_run_agent(
    model_str: str | None,
    prompt: str,
    **kwargs: Any,
) -> tuple[str, str, str, str, str | None]:
    _CAPTURED["agent"] = {"model_str": model_str, "prompt": prompt, **kwargs}
    return "output", "provider", "branch-id", "completed", "sess-001"


def _run(argv: list[str]) -> int:
    import lionagi.cli.agent as agent_mod
    from lionagi.cli.main import main

    _CAPTURED.clear()
    with patch.object(agent_mod, "_run_agent", _fake_run_agent):
        return main(argv)


def test_agent_notify_flag_threads_into_run_agent():
    rc = _run(["agent", "claude", "do the thing", "--notify", "my-hook {payload}"])
    assert rc == 0
    assert _CAPTURED["agent"]["notify"] == "my-hook {payload}"


def test_agent_notify_flag_defaults_to_none_when_absent():
    rc = _run(["agent", "claude", "do the thing"])
    assert rc == 0
    assert _CAPTURED["agent"]["notify"] is None


@pytest.mark.asyncio
async def test_notify_scope_is_session_even_with_invocation_id(monkeypatch, tmp_path):
    """Notification remains session-scoped when `--invocation` is set."""
    _wire_agent_stubs(
        monkeypatch,
        tmp_path,
        operate_side_effect=lambda i: "done",
        session_ids=["sess-notify-1"],
    )

    from lionagi.cli.orchestrate import _notify as notify_mod

    captured: dict[str, Any] = {}

    def fake_register(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "scope-name"

    monkeypatch.setattr(notify_mod, "register_flow_notify_scope", fake_register)
    monkeypatch.setattr(notify_mod, "unregister_flow_notify_scope", lambda *a, **kw: None)

    from lionagi.cli.agent import _run_agent

    await _run_agent(
        "claude_code/sonnet",
        "hello",
        invocation_id="parent-inv-123",
        notify="my-hook {status}",
    )

    assert captured["entity_kind"] == "session"
    assert captured["entity_id"] == "sess-notify-1"
    assert captured["invocation_id"] == "parent-inv-123"
