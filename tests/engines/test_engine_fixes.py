# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for three engine bug fixes.

- #1367: CodingEngine deadline does not bound in-flight worker calls.
- #1366: CodingEngine normalize-before-gate (ValueError before run state exists).
- #1363: HypothesisEngine emission repair weak for agentic CLI workers.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from lionagi.engines.coding import (
    CodingEngine,
    WorkPlanned,
)
from lionagi.engines.engine import Engine, EngineEvent, _cli_repair_instruction, emission_keys
from lionagi.engines.hypothesis import (
    FindingPosted,
    HypothesisEngine,
    QuestionRaised,
)

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


class _StubEngine(Engine):
    async def _run(self, run, *a, **kw):  # pragma: no cover
        return ""


class _SlowEvent(EngineEvent):
    value: str = ""


# ---------------------------------------------------------------------------
# #1367 — deadline watchdog cancels in-flight spawned tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deadline_watchdog_cancels_slow_spawned_tasks():
    """A spawned task that would run longer than ``deadline_s`` must be
    cancelled by the watchdog.  The run must complete (not hang) and the
    budget_exhausted event must be emitted."""
    # Very short deadline so the test is fast.
    eng = _StubEngine(deadline_s=0.05)
    cancelled = asyncio.Event()
    ran_to_completion = asyncio.Event()

    async def _override_run(run, *a, **kw):
        async def slow_worker():
            try:
                await asyncio.sleep(10)  # would run far past the deadline
                ran_to_completion.set()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        run.spawn(slow_worker())
        await run.wait_quiescence()
        return "done"

    eng._run = _override_run

    events: list[dict] = []
    result = await eng.run(on_event=events.append)

    assert result == "done"
    assert cancelled.is_set(), "slow worker was not cancelled by the watchdog"
    assert not ran_to_completion.is_set(), "slow worker must not have finished"
    assert any(e["type"] == "budget_exhausted" for e in events), (
        f"budget_exhausted event not emitted; got {[e['type'] for e in events]}"
    )


@pytest.mark.asyncio
async def test_deadline_watchdog_does_not_fire_when_no_deadline():
    """Without a deadline, the watchdog task is never created and the run
    completes normally."""
    eng = _StubEngine()  # no deadline_s
    completed: list[str] = []

    async def _override_run(run, *a, **kw):
        async def quick():
            await asyncio.sleep(0.01)
            completed.append("done")

        run.spawn(quick())
        await run.wait_quiescence()
        return "ok"

    eng._run = _override_run
    result = await eng.run()
    assert result == "ok"
    assert completed == ["done"]


@pytest.mark.asyncio
async def test_deadline_watchdog_cleans_up_after_fast_run():
    """The watchdog task must be cancelled (not left dangling) when the run
    finishes before the deadline expires."""
    eng = _StubEngine(deadline_s=60.0)  # long deadline — run finishes first

    async def _override_run(run, *a, **kw):
        return "fast"

    eng._run = _override_run
    result = await eng.run()
    assert result == "fast"
    # If the watchdog leaked, it would still be sleeping — the test itself
    # finishing promptly is the observable signal that cleanup happened.


# ---------------------------------------------------------------------------
# #1366 — CodingEngine normalize-before-gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coding_engine_rejects_empty_spec_before_run_state():
    """An empty string spec must raise ValueError before any session/run state
    is created — the gate must fire before ``new_run()``."""
    eng = CodingEngine()
    make_agent_called = []

    # Patch new_run to detect if it was called before the error.
    original_new_run = eng.new_run

    def spy_new_run(**kw):
        make_agent_called.append(True)
        return original_new_run(**kw)

    eng.new_run = spy_new_run

    with pytest.raises(ValueError, match="empty"):
        await eng.run(
            "   ",  # empty after strip
            test_cmd=[sys.executable, "-c", "exit(0)"],
        )

    assert not make_agent_called, "new_run() must not be called before spec validation raises"


@pytest.mark.asyncio
async def test_coding_engine_rejects_empty_dict_spec_before_run_state():
    """An empty dict spec must raise ValueError before any run state is
    created."""
    eng = CodingEngine()
    make_agent_called = []

    original_new_run = eng.new_run

    def spy_new_run(**kw):
        make_agent_called.append(True)
        return original_new_run(**kw)

    eng.new_run = spy_new_run

    with pytest.raises(ValueError, match="no procedure"):
        await eng.run(
            {},  # dict with no valid keys
            test_cmd=[sys.executable, "-c", "exit(0)"],
        )

    assert not make_agent_called, "new_run() must not be called before spec validation raises"


@pytest.mark.asyncio
async def test_coding_engine_rejects_wrong_type_spec_before_run_state():
    """A non-str/non-dict spec must raise TypeError before run state exists."""
    eng = CodingEngine()
    make_agent_called = []

    original_new_run = eng.new_run

    def spy_new_run(**kw):
        make_agent_called.append(True)
        return original_new_run(**kw)

    eng.new_run = spy_new_run

    with pytest.raises(TypeError):
        await eng.run(
            42,  # type: ignore[arg-type]
            test_cmd=[sys.executable, "-c", "exit(0)"],
        )

    assert not make_agent_called, "new_run() must not be called before spec validation raises"


@pytest.mark.asyncio
async def test_coding_engine_valid_spec_proceeds_to_run(tmp_path, monkeypatch):
    """A valid spec must not be rejected — the gate is pass-through for well-
    formed input."""
    eng = CodingEngine(repair_retries=0)
    run_obj = eng.new_run()

    plan_ev = WorkPlanned(approach="trivial")
    from lionagi.engines.coding import (
        ChangeProposed,
        VerifyResult,
    )

    branches: dict = {
        "plan": SimpleNamespace(
            name="plan",
            operate=lambda *, instruction: _emit_and_return(run_obj, plan_ev),
        ),
        "implement": SimpleNamespace(
            name="implement",
            operate=lambda *, instruction: _emit_and_return(
                run_obj, ChangeProposed(summary="done", plan_ref="W-1")
            ),
        ),
        "verify": SimpleNamespace(
            name="verify",
            operate=lambda *, instruction: _emit_and_return(
                run_obj,
                VerifyResult(verdict="APPROVE", rationale="ok", meets_acceptance=True),
            ),
        ),
    }

    async def fake_make(role, *, name=None, **kw):
        return branches[name]

    monkeypatch.setattr(run_obj, "make_agent", fake_make)
    monkeypatch.setattr(eng, "_capture_diff", lambda r: _coro(""))

    # Patch new_run to return our pre-built run so we can inject fake_make.
    monkeypatch.setattr(eng, "new_run", lambda **kw: run_obj)

    result = await eng.run(
        "write a trivial function",
        test_cmd=[sys.executable, "-c", "exit(0)"],
        workspace=str(tmp_path),
    )
    assert result.passed is True


async def _emit_and_return(run, event):
    await run.emit(event)
    return "ok"


def _coro(value):
    async def _inner():
        return value

    return _inner()


# ---------------------------------------------------------------------------
# #1363 — CLI-aware emission repair instruction
# ---------------------------------------------------------------------------


def test_cli_repair_instruction_contains_fenced_example():
    """The CLI repair instruction must include a fenced JSON example block —
    the full-structure example that CLI workers need, not just key name hints."""
    from lionagi.engines.research import FindingEmitted

    hint = emission_keys((FindingEmitted,))
    msg = _cli_repair_instruction(hint, (FindingEmitted,))

    assert "```json" in msg, "CLI repair must include a fenced JSON block"
    assert "finding_emitted" in msg, "emission key must appear in the example"
    assert "no fenced JSON block" in msg, "message must explain the failure mode"


def test_cli_repair_instruction_differs_from_api_repair():
    """The CLI repair template must be distinct from the API repair template —
    they address different failure modes."""
    from lionagi.engines.engine import _repair_instruction
    from lionagi.engines.research import FindingEmitted

    hint = emission_keys((FindingEmitted,))
    api_msg = _repair_instruction(hint)
    cli_msg = _cli_repair_instruction(hint, (FindingEmitted,))
    assert api_msg != cli_msg, "CLI and API repair instructions must differ"


@pytest.mark.asyncio
async def test_operate_with_repair_uses_cli_template_for_cli_branch():
    """When the branch's chat_model.is_cli is True, operate_with_repair must
    issue the CLI repair instruction (fenced JSON example), not the API one."""
    eng = _StubEngine()
    run = eng.new_run()
    events: list[dict] = []
    run.on_event = events.append

    from lionagi.engines.research import FindingEmitted

    repair_instructions: list[str] = []
    call_count = 0

    class _CLIBranch:
        name = "cli-worker"
        chat_model = SimpleNamespace(is_cli=True)

        async def operate(self, *, instruction):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                repair_instructions.append(instruction)
                # Emit on the repair turn so arrived() becomes true.
                await run.emit(FindingEmitted(description="found", novelty=0.5))
            return "prose with no JSON"

    branch = _CLIBranch()
    await run.operate_with_repair(
        branch,
        "find things",
        arrived=lambda: bool(run.by_type(FindingEmitted)),
        emits=(FindingEmitted,),
        retries=1,
    )

    assert call_count == 2, "repair must issue exactly one extra turn"
    assert repair_instructions, "a repair instruction must have been sent"
    repair_text = repair_instructions[0]
    assert "```json" in repair_text, (
        f"CLI repair must include a fenced JSON example; got: {repair_text!r}"
    )
    assert any(e["type"] == "emission_repair" and e.get("cli_worker") is True for e in events), (
        "emission_repair event must carry cli_worker=True flag"
    )


@pytest.mark.asyncio
async def test_operate_with_repair_uses_api_template_for_api_branch():
    """When the branch's chat_model.is_cli is False, the API repair template
    (key-name hints, not full example) must be used."""
    eng = _StubEngine()
    run = eng.new_run()
    events: list[dict] = []
    run.on_event = events.append

    from lionagi.engines.research import FindingEmitted

    repair_instructions: list[str] = []
    call_count = 0

    class _APIBranch:
        name = "api-worker"
        chat_model = SimpleNamespace(is_cli=False)

        async def operate(self, *, instruction):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                repair_instructions.append(instruction)
                await run.emit(FindingEmitted(description="api-found", novelty=0.3))
            return "prose"

    branch = _APIBranch()
    await run.operate_with_repair(
        branch,
        "find things",
        arrived=lambda: bool(run.by_type(FindingEmitted)),
        emits=(FindingEmitted,),
        retries=1,
    )

    assert repair_instructions, "a repair instruction must have been sent"
    repair_text = repair_instructions[0]
    # API repair: key-name hint present, NOT the CLI "fenced JSON block" prose.
    assert "finding_emitted" in repair_text, "API repair must name the emission key"
    assert "no fenced JSON block" not in repair_text, (
        "API repair must NOT use the CLI failure-mode description"
    )
    assert any(e["type"] == "emission_repair" and e.get("cli_worker") is False for e in events), (
        "emission_repair event must carry cli_worker=False for API branch"
    )


@pytest.mark.asyncio
async def test_operate_with_repair_no_chat_model_falls_back_to_api_template():
    """When the branch has no chat_model attribute, the repair must fall back to
    the API template without raising — graceful degradation."""
    eng = _StubEngine()
    run = eng.new_run()

    from lionagi.engines.research import FindingEmitted

    repair_instructions: list[str] = []
    call_count = 0

    class _NakedBranch:
        """No chat_model attribute — emulates a test stub or unusual branch."""

        name = "naked"

        async def operate(self, *, instruction):
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                repair_instructions.append(instruction)
                await run.emit(FindingEmitted(description="late", novelty=0.2))
            return "prose"

    branch = _NakedBranch()
    await run.operate_with_repair(
        branch,
        "find things",
        arrived=lambda: bool(run.by_type(FindingEmitted)),
        emits=(FindingEmitted,),
        retries=1,
    )
    assert repair_instructions, "repair must have been issued"
    # Should use the API template (key hints), not crash.
    assert "finding_emitted" in repair_instructions[0]
