# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for worker tool enrichment (worker_extra_tools / worker_mcp_servers /
worker_extra_prompt) and mechanical-repair efficiency (auto_repair_cmds /
fast_test_cmd / _looks_mechanical / AutoRepairApplied)."""

from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from lionagi.engines.coding import (
    AutoRepairApplied,
    ChangeProposed,
    CodingEngine,
    VerifyResult,
    WorkPlanned,
    _looks_mechanical,
)
from lionagi.engines.engine import Engine, EngineRun

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


class _StubEngine(Engine):
    async def _run(self, run: EngineRun, *a: Any, **kw: Any) -> Any:  # pragma: no cover
        return ""


def _async(value):
    async def _coro():
        return value

    return _coro()


class _ScriptedBranch:
    def __init__(self, run, events: list, *, name: str = "agent"):
        self._run = run
        self._events = list(events)
        self.name = name
        self.calls: list[str] = []
        self.chat_model = None

    async def operate(self, *, instruction, **kw):
        self.calls.append(instruction)
        if self._events:
            await self._run.emit(self._events.pop(0))
        return "ok"


# ---------------------------------------------------------------------------
# _looks_mechanical unit tests
# ---------------------------------------------------------------------------


def test_looks_mechanical_returns_false_for_passing_tests():
    from lionagi.engines.coding import TestsRan

    t = TestsRan(cmd="cargo test", passed=True, returncode=0)
    assert not _looks_mechanical(t)


def test_looks_mechanical_returns_false_for_empty_output():
    from lionagi.engines.coding import TestsRan

    t = TestsRan(cmd="cargo test", passed=False, returncode=1, output_tail="")
    assert not _looks_mechanical(t)


def test_looks_mechanical_returns_true_for_rustfmt_output():
    from lionagi.engines.coding import TestsRan

    tail = "rustfmt would reformat src/lib.rs\nrustfmt would reformat src/main.rs\n"
    t = TestsRan(cmd="cargo fmt --check", passed=False, returncode=1, output_tail=tail)
    assert _looks_mechanical(t)


def test_looks_mechanical_returns_false_for_mixed_output():
    from lionagi.engines.coding import TestsRan

    tail = "rustfmt would reformat src/lib.rs\nerror[E0308]: mismatched types\n"
    t = TestsRan(cmd="cargo test", passed=False, returncode=1, output_tail=tail)
    assert not _looks_mechanical(t)


def test_looks_mechanical_ruff_format_output():
    from lionagi.engines.coding import TestsRan

    tail = "ruff format check would reformat 3 files\n"
    t = TestsRan(cmd="ruff format --check", passed=False, returncode=1, output_tail=tail)
    assert _looks_mechanical(t)


# ---------------------------------------------------------------------------
# worker_extra_tools: merged into make_agent call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_extra_tools_merged_with_coding_tools(tmp_path, monkeypatch):
    """worker_extra_tools must be combined with coding_tools when calling make_agent."""
    eng = CodingEngine(
        coding_tools=("coding",),
        worker_extra_tools=("search",),
        repair_retries=0,
        max_fix_rounds=0,
    )
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="do it")
    change_ev = ChangeProposed(summary="done", plan_ref="W-1")
    verdict_ev = VerifyResult(verdict="APPROVE", rationale="ok", meets_acceptance=True)

    captured_make_calls: list[dict] = []
    orig_make = run.make_agent

    async def spy_make(role, *, name=None, **kw):
        captured_make_calls.append({"role": role, "name": name, **kw})
        return _ScriptedBranch(
            run,
            {"plan": [plan_ev], "implement": [change_ev], "verify": [verdict_ev]}.get(name, []),
            name=name,
        )

    monkeypatch.setattr(run, "make_agent", spy_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    await eng._run(
        run,
        "Acceptance criteria: passes.",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )

    implement_call = next((c for c in captured_make_calls if c.get("name") == "implement"), None)
    assert implement_call is not None
    assert "coding" in implement_call["tools"]
    assert "search" in implement_call["tools"]


@pytest.mark.asyncio
async def test_plan_and_verify_do_not_get_worker_tools(tmp_path, monkeypatch):
    """plan/verify agents must not receive worker_extra_tools."""
    eng = CodingEngine(
        coding_tools=("coding",),
        worker_extra_tools=("search",),
        repair_retries=0,
        max_fix_rounds=0,
    )
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="do it")
    change_ev = ChangeProposed(summary="done", plan_ref="W-1")
    verdict_ev = VerifyResult(verdict="APPROVE", rationale="ok", meets_acceptance=True)

    captured_make_calls: list[dict] = []

    async def spy_make(role, *, name=None, **kw):
        captured_make_calls.append({"role": role, "name": name, **kw})
        return _ScriptedBranch(
            run,
            {"plan": [plan_ev], "implement": [change_ev], "verify": [verdict_ev]}.get(name, []),
            name=name,
        )

    monkeypatch.setattr(run, "make_agent", spy_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    await eng._run(
        run,
        "Acceptance criteria: passes.",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )

    for call in captured_make_calls:
        if call.get("name") in ("plan", "verify"):
            # plan and verify must not receive the worker extra tools
            assert "search" not in call.get("tools", ()), (
                f"{call['name']} agent must not receive worker_extra_tools"
            )


# ---------------------------------------------------------------------------
# worker_mcp_servers: passed to implement agent only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_mcp_servers_passed_to_implement_not_verify(tmp_path, monkeypatch):
    """worker_mcp_servers must be set on the implement agent; plan/verify must have None."""
    eng = CodingEngine(
        coding_tools=("coding",),
        worker_mcp_servers=["khive"],
        repair_retries=0,
        max_fix_rounds=0,
    )
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="do it")
    change_ev = ChangeProposed(summary="done", plan_ref="W-1")
    verdict_ev = VerifyResult(verdict="APPROVE", rationale="ok", meets_acceptance=True)

    captured_make_calls: list[dict] = []

    async def spy_make(role, *, name=None, **kw):
        captured_make_calls.append({"role": role, "name": name, **kw})
        return _ScriptedBranch(
            run,
            {"plan": [plan_ev], "implement": [change_ev], "verify": [verdict_ev]}.get(name, []),
            name=name,
        )

    monkeypatch.setattr(run, "make_agent", spy_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    await eng._run(
        run,
        "Acceptance criteria: passes.",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )

    implement_call = next((c for c in captured_make_calls if c.get("name") == "implement"), None)
    assert implement_call is not None
    assert implement_call.get("mcp_servers") == ["khive"]

    for call in captured_make_calls:
        if call.get("name") in ("plan", "verify"):
            assert call.get("mcp_servers") is None, f"{call['name']} must not receive mcp_servers"


# ---------------------------------------------------------------------------
# worker_extra_prompt: forwarded to implement agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_extra_prompt_forwarded(tmp_path, monkeypatch):
    """worker_extra_prompt must be forwarded as extra_prompt to the implement agent."""
    eng = CodingEngine(
        repair_retries=0,
        max_fix_rounds=0,
        worker_extra_prompt="Always prefer idiomatic Rust.",
    )
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="do it")
    change_ev = ChangeProposed(summary="done", plan_ref="W-1")
    verdict_ev = VerifyResult(verdict="APPROVE", rationale="ok", meets_acceptance=True)

    captured_make_calls: list[dict] = []

    async def spy_make(role, *, name=None, **kw):
        captured_make_calls.append({"role": role, "name": name, **kw})
        return _ScriptedBranch(
            run,
            {"plan": [plan_ev], "implement": [change_ev], "verify": [verdict_ev]}.get(name, []),
            name=name,
        )

    monkeypatch.setattr(run, "make_agent", spy_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    await eng._run(
        run,
        "Acceptance criteria: passes.",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )

    implement_call = next((c for c in captured_make_calls if c.get("name") == "implement"), None)
    assert implement_call is not None
    assert implement_call.get("extra_prompt") == "Always prefer idiomatic Rust."


# ---------------------------------------------------------------------------
# make_agent: mcp_servers + extra_prompt propagate through AgentSpec
# ---------------------------------------------------------------------------


def test_make_agent_mcp_servers_set_on_spec_directly():
    """AgentSpec.compose result must have mcp_servers set after make_agent wires it."""
    from lionagi.agent.spec import AgentSpec

    spec = AgentSpec.compose("implementer", tools=(), cwd=None)
    assert spec.mcp_servers is None
    # Simulate what make_agent does
    spec.mcp_servers = ["khive"]
    assert spec.mcp_servers == ["khive"]


def test_make_agent_extra_prompt_set_via_compose():
    """AgentSpec.compose must thread system_prompt into extra_prompt."""
    from lionagi.agent.spec import AgentSpec

    spec = AgentSpec.compose("implementer", system_prompt="prefer idiomatic Rust")
    assert spec.extra_prompt == "prefer idiomatic Rust"


# ---------------------------------------------------------------------------
# auto_repair_cmds: AutoRepairApplied events emitted before test gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_repair_cmds_fires_on_first_test_pass(tmp_path, monkeypatch):
    """auto_repair_cmds must run before the test gate and emit AutoRepairApplied."""
    # Use a real auto-repair command that always succeeds (echo/true).
    eng = CodingEngine(
        repair_retries=0,
        max_fix_rounds=0,
        auto_repair_cmds=["true"],  # no-op that always exits 0
    )
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="do it")
    change_ev = ChangeProposed(summary="done", plan_ref="W-1")
    verdict_ev = VerifyResult(verdict="APPROVE", rationale="ok", meets_acceptance=True)

    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _ScriptedBranch(run, [change_ev], name="implement"),
        "verify": _ScriptedBranch(run, [verdict_ev], name="verify"),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches.get(name, _ScriptedBranch(run, [], name=name))

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    events: list[dict] = []
    run.on_event = events.append

    result = await eng._run(
        run,
        "Acceptance criteria: passes.",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )

    assert result.passed is True
    auto_repair_events = [e for e in events if e["type"] == "AutoRepairApplied"]
    assert auto_repair_events, (
        "AutoRepairApplied event must be emitted when auto_repair_cmds is set"
    )
    assert auto_repair_events[0]["cmd"] == "true"


@pytest.mark.asyncio
async def test_auto_repair_failed_notifies_but_does_not_crash(tmp_path, monkeypatch):
    """A failing auto_repair_cmd must emit auto_repair_failed and continue."""
    eng = CodingEngine(
        repair_retries=0,
        max_fix_rounds=0,
        auto_repair_cmds=["false"],  # always exits 1
    )
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="do it")
    change_ev = ChangeProposed(summary="done", plan_ref="W-1")
    verdict_ev = VerifyResult(verdict="APPROVE", rationale="ok", meets_acceptance=True)

    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _ScriptedBranch(run, [change_ev], name="implement"),
        "verify": _ScriptedBranch(run, [verdict_ev], name="verify"),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches.get(name, _ScriptedBranch(run, [], name=name))

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    events: list[dict] = []
    run.on_event = events.append

    result = await eng._run(
        run,
        "Acceptance criteria: passes.",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )

    # Must not crash; test gate still determines the result
    assert result.passed is True
    failed_notifs = [e for e in events if e["type"] == "auto_repair_failed"]
    assert failed_notifs, (
        "auto_repair_failed must be emitted when the repair command exits non-zero"
    )


@pytest.mark.asyncio
async def test_auto_repair_recorded_in_measurements(tmp_path, monkeypatch):
    """auto_repair_rounds must appear in measurements when repairs are applied."""
    eng = CodingEngine(
        repair_retries=0,
        max_fix_rounds=0,
        auto_repair_cmds=["true"],
    )
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="do it")
    change_ev = ChangeProposed(summary="done", plan_ref="W-1")
    verdict_ev = VerifyResult(verdict="APPROVE", rationale="ok", meets_acceptance=True)

    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _ScriptedBranch(run, [change_ev], name="implement"),
        "verify": _ScriptedBranch(run, [verdict_ev], name="verify"),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches.get(name, _ScriptedBranch(run, [], name=name))

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    result = await eng._run(
        run,
        "Acceptance criteria: passes.",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )

    assert "auto_repair_rounds" in result.measurements


# ---------------------------------------------------------------------------
# mechanical round: skips judge, skips worker re-prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mechanical_fix_round_skips_judge(tmp_path, monkeypatch):
    """A mechanical fix round must skip the judge gate entirely."""
    eng = CodingEngine(
        repair_retries=0,
        max_fix_rounds=1,
        auto_repair_cmds=["true"],  # enables mechanical classification
    )
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="do it")
    change_ev = ChangeProposed(summary="done", plan_ref="W-1")
    verdict_ev = VerifyResult(verdict="APPROVE", rationale="ok", meets_acceptance=True)

    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": _ScriptedBranch(run, [change_ev], name="implement"),
        "verify": _ScriptedBranch(run, [verdict_ev], name="verify"),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches.get(name, _ScriptedBranch(run, [], name=name))

    judge_calls: list[str] = []

    async def spy_judge(r, eid, subject):
        judge_calls.append(eid)
        return True

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))
    monkeypatch.setattr(eng, "judge", spy_judge)

    # First test fails with rustfmt-looking output; second passes.
    call_count = 0

    async def fake_test(r, change, *, round_no):
        nonlocal call_count
        call_count += 1
        from lionagi.engines.coding import TestsRan

        if call_count == 1:
            t = TestsRan(
                change_ref=change.eid,
                cmd="cargo fmt --check",
                passed=False,
                returncode=1,
                round=round_no,
                output_tail="rustfmt would reformat src/lib.rs\n",
            )
        else:
            t = TestsRan(
                change_ref=change.eid,
                cmd="cargo test",
                passed=True,
                returncode=0,
                round=round_no,
            )
        await run.emit(t)
        return run.last(TestsRan)

    monkeypatch.setattr(eng, "_test", fake_test)

    events: list[dict] = []
    run.on_event = events.append

    result = await eng._run(
        run,
        "Acceptance criteria: passes.",
        test_cmd="cargo test",
        workspace=str(tmp_path),
    )

    assert result.passed is True
    # Judge must not have been called for the mechanical round
    assert not any("fix-" in eid for eid in judge_calls), (
        f"judge must not be called for mechanical rounds; calls={judge_calls}"
    )
    assert any(e["type"] == "fix_mechanical" for e in events)


# ---------------------------------------------------------------------------
# fast_test_cmd: used for intermediate rounds, full test_cmd as final gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fast_test_cmd_used_for_intermediate_rounds(tmp_path, monkeypatch):
    """fast_test_cmd must be invoked for intermediate fix rounds; full test_cmd is the final gate."""
    eng = CodingEngine(
        repair_retries=0,
        max_fix_rounds=2,
        fast_test_cmd="echo fast",
    )
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="do it")
    # Two changes: first fails full gate, second passes
    change_1 = ChangeProposed(summary="attempt 1", plan_ref="W-1")
    change_2 = ChangeProposed(summary="attempt 2", plan_ref="W-1")
    verdict_ev = VerifyResult(verdict="APPROVE", rationale="ok", meets_acceptance=True)

    class _TwoBranchImpl:
        name = "implement"
        call_count = 0

        async def operate(self, *, instruction, **kw):
            self.call_count += 1
            if self.call_count == 1:
                await run.emit(change_1)
            else:
                await run.emit(change_2)
            return "ok"

    impl = _TwoBranchImpl()
    branches = {
        "plan": _ScriptedBranch(run, [plan_ev], name="plan"),
        "implement": impl,
        "verify": _ScriptedBranch(run, [verdict_ev], name="verify"),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches.get(name, _ScriptedBranch(run, [], name=name))

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    fast_calls: list[int] = []
    full_calls: list[int] = []
    real_fast = eng._fast_test
    real_full = eng._test

    async def spy_fast(r, change, *, round_no):
        fast_calls.append(round_no)
        return await real_fast(r, change, round_no=round_no)

    test_round = 0

    async def spy_test(r, change, *, round_no):
        full_calls.append(round_no)
        return await real_full(r, change, round_no=round_no)

    monkeypatch.setattr(eng, "_fast_test", spy_fast)
    monkeypatch.setattr(eng, "_test", spy_test)

    # First full test fails; fast test passes; second full test passes.
    from lionagi.engines.coding import TestsRan

    full_test_call = 0

    async def controlled_test(r, change, *, round_no):
        nonlocal full_test_call
        full_test_call += 1
        full_calls.append(round_no)
        passed = full_test_call > 1
        t = TestsRan(
            change_ref=change.eid,
            cmd="real_test",
            passed=passed,
            returncode=0 if passed else 1,
            round=round_no,
        )
        await run.emit(t)
        return run.last(TestsRan)

    monkeypatch.setattr(eng, "_test", controlled_test)

    result = await eng._run(
        run,
        "Acceptance criteria: passes.",
        test_cmd="real_test",
        workspace=str(tmp_path),
    )

    assert result.passed is True
    # fast_test must have been called for the intermediate round (round_no=1, max=2)
    assert fast_calls, f"_fast_test must be called for intermediate rounds; got calls={fast_calls}"


# ---------------------------------------------------------------------------
# worker_grants recorded in measurements
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_worker_grants_recorded_in_measurements_when_configured(tmp_path, monkeypatch):
    """worker_grants must appear in measurements when worker_extra_tools or mcp_servers set."""
    eng = CodingEngine(
        repair_retries=0,
        max_fix_rounds=0,
        worker_extra_tools=("search",),
        worker_mcp_servers=["khive"],
    )
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="do it")
    change_ev = ChangeProposed(summary="done", plan_ref="W-1")
    verdict_ev = VerifyResult(verdict="APPROVE", rationale="ok", meets_acceptance=True)

    async def fake_make(role, *, name=None, **kw):
        return _ScriptedBranch(
            run,
            {"plan": [plan_ev], "implement": [change_ev], "verify": [verdict_ev]}.get(name, []),
            name=name,
        )

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    result = await eng._run(
        run,
        "Acceptance criteria: passes.",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )

    assert "worker_grants" in result.measurements
    grants = result.measurements["worker_grants"]
    assert "search" in grants["extra_tools"]
    assert "khive" in grants["mcp_servers"]


@pytest.mark.asyncio
async def test_worker_grants_absent_when_not_configured(tmp_path, monkeypatch):
    """worker_grants must not appear in measurements when no worker grants are set."""
    eng = CodingEngine(repair_retries=0, max_fix_rounds=0)
    run = eng.new_run()

    plan_ev = WorkPlanned(approach="do it")
    change_ev = ChangeProposed(summary="done", plan_ref="W-1")
    verdict_ev = VerifyResult(verdict="APPROVE", rationale="ok", meets_acceptance=True)

    async def fake_make(role, *, name=None, **kw):
        return _ScriptedBranch(
            run,
            {"plan": [plan_ev], "implement": [change_ev], "verify": [verdict_ev]}.get(name, []),
            name=name,
        )

    monkeypatch.setattr(run, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _async(""))

    result = await eng._run(
        run,
        "Acceptance criteria: passes.",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )

    assert "worker_grants" not in result.measurements


# ---------------------------------------------------------------------------
# AutoRepairApplied event shape
# ---------------------------------------------------------------------------


def test_auto_repair_applied_event_shape():
    ev = AutoRepairApplied(cmd="cargo fmt --all", files=["src/lib.rs"], round=1)
    assert ev.cmd == "cargo fmt --all"
    assert ev.files == ["src/lib.rs"]
    assert ev.round == 1


def test_auto_repair_applied_default_files():
    ev = AutoRepairApplied(cmd="cargo fmt --all")
    assert ev.files == []
    assert ev.round == 0
