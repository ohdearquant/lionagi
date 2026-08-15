# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Detached MCP agent runs must know that nobody can wake a background wait."""

from __future__ import annotations

import pytest

from tests.cli.test_agent_resume_on_timeout import _wire_agent_stubs


@pytest.mark.asyncio
async def test_detached_agent_receives_undeliverable_wait_boundary(monkeypatch, tmp_path):
    monkeypatch.setenv("LIONAGI_MCP_JOB_RUN_ID", "20260811T120000-abcdef")
    _, instructions, _ = _wire_agent_stubs(
        monkeypatch,
        tmp_path,
        operate_side_effect=lambda _: "output",
    )

    from lionagi.cli.agent import _run_agent

    await _run_agent("codex/model", "run the long test gate", bypass=True)

    [prompt] = instructions
    assert prompt.endswith("run the long test gate")
    assert "DETACHED EXECUTION BOUNDARY" in prompt
    assert "cannot receive a background completion notification" in prompt
    assert "foreground" in prompt
    assert "poll" in prompt


@pytest.mark.asyncio
async def test_interactive_agent_prompt_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.delenv("LIONAGI_MCP_JOB_RUN_ID", raising=False)
    _, instructions, _ = _wire_agent_stubs(
        monkeypatch,
        tmp_path,
        operate_side_effect=lambda _: "output",
    )

    from lionagi.cli.agent import _run_agent

    await _run_agent("codex/model", "run the long test gate", bypass=True)

    assert instructions == ["run the long test gate"]
