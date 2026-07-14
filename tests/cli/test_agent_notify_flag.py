# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li agent --notify`: parses via the shared common args, defaults to None, and
threads through to _run_agent so a per-invocation terminal-notify override reaches
the run scope (mirrors the `li o flow` / `li o fanout` wiring)."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

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
