# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for total sub-agent failure diagnostics: EngineRun._agent_errors
accumulation and Engine._total_agent_failure — the signal that lets a run
where every agent terminally errored (e.g. missing API key) be surfaced as
failed instead of silently reporting 'completed'."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lionagi.engines.engine import Engine, EngineRun

# ---------------------------------------------------------------------------
# EngineRun._agent_errors accumulation via notify()
# ---------------------------------------------------------------------------


def _make_minimal_engine_run() -> EngineRun:
    engine = MagicMock()
    engine.max_concurrent = 1
    engine.max_agents = 100
    engine.deadline_s = None
    engine.model = None
    return EngineRun(engine, on_event=None)


def test_engine_run_agent_errors_starts_empty():
    er = _make_minimal_engine_run()
    assert er._agent_errors == []


def test_notify_agent_error_accumulates():
    er = _make_minimal_engine_run()
    er.notify("agent_error", agent="worker-1", error="API key is required")
    er.notify("agent_error", agent="worker-2", error="rate limited")
    assert er._agent_errors == [
        "worker-1: API key is required",
        "worker-2: rate limited",
    ]


def test_notify_other_kinds_do_not_affect_agent_errors():
    er = _make_minimal_engine_run()
    er.notify("gated", eid="x", reason="reject")
    er.notify("budget_exhausted", reason="agents", agents_made=5, elapsed=1.0)
    assert er._agent_errors == []


def test_notify_still_forwards_to_on_event():
    calls = []
    engine = MagicMock()
    engine.max_concurrent = 1
    engine.max_agents = 100
    engine.deadline_s = None
    engine.model = None
    er = EngineRun(engine, on_event=lambda e: calls.append(e))

    er.notify("agent_error", agent="worker-1", error="boom")

    assert calls == [{"type": "agent_error", "agent": "worker-1", "error": "boom"}]
    assert er._agent_errors == ["worker-1: boom"]


# ---------------------------------------------------------------------------
# Engine._total_agent_failure — flagged only when every agent made errored
# ---------------------------------------------------------------------------


async def test_total_agent_failure_true_when_all_agents_errored():
    class AllAgentsFailEngine(Engine):
        async def _run(self, run: EngineRun, spec: str, **kwargs) -> str:  # type: ignore[override]
            run.agents_made = 2
            run.notify("agent_error", agent="a1", error="API key is required")
            run.notify("agent_error", agent="a2", error="API key is required")
            return ""

    engine = AllAgentsFailEngine(max_agents=5)
    await engine.run("spec")

    assert engine._total_agent_failure is True
    assert engine._agent_errors == [
        "a1: API key is required",
        "a2: API key is required",
    ]


async def test_total_agent_failure_false_when_some_agents_succeed():
    class PartialAgentFailEngine(Engine):
        async def _run(self, run: EngineRun, spec: str, **kwargs) -> str:  # type: ignore[override]
            run.agents_made = 2
            run.notify("agent_error", agent="a1", error="API key is required")
            return "ok"

    engine = PartialAgentFailEngine(max_agents=5)
    result = await engine.run("spec")

    assert engine._total_agent_failure is False
    assert "ok" in str(result)


async def test_total_agent_failure_false_when_no_agents_made():
    class NoAgentsEngine(Engine):
        async def _run(self, run: EngineRun, spec: str, **kwargs) -> str:  # type: ignore[override]
            return "no agents needed"

    engine = NoAgentsEngine(max_agents=5)
    await engine.run("spec")

    assert engine._total_agent_failure is False
    assert engine._agent_errors == []


async def test_total_agent_failure_false_on_clean_run():
    class CleanEngine(Engine):
        async def _run(self, run: EngineRun, spec: str, **kwargs) -> str:  # type: ignore[override]
            run.agents_made = 3
            return "clean"

    engine = CleanEngine(max_agents=5)
    await engine.run("spec")

    assert engine._total_agent_failure is False
    assert engine._agent_errors == []


async def test_engine_reuse_second_run_resets_total_agent_failure():
    """A reused engine must not carry a total-failure flag from a previous run
    into the next one, mirroring the existing _emission_failures reset guarantee."""

    call_count = [0]

    class TwoRunEngine(Engine):
        async def _run(self, run: EngineRun, spec: str, **kwargs) -> str:  # type: ignore[override]
            call_count[0] += 1
            if call_count[0] == 1:
                run.agents_made = 2
                run.notify("agent_error", agent="a1", error="boom")
                run.notify("agent_error", agent="a2", error="boom")
            else:
                run.agents_made = 2
            return "result"

    engine = TwoRunEngine(max_agents=5)

    await engine.run("run-1")
    assert engine._total_agent_failure is True

    await engine.run("run-2")
    assert engine._total_agent_failure is False
    assert engine._agent_errors == []
