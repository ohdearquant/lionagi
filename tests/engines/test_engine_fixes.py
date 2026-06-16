# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Regression tests: deadline cancellation, normalize-before-gate, CLI emission repair."""

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
# Deadline watchdog cancels in-flight spawned tasks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deadline_watchdog_cancels_slow_spawned_tasks():
    """A spawned task that would run longer than ``deadline_s`` must be
    cancelled by the watchdog.  After cancellation, Engine.run() must return
    normally (the partial export hook runs, even if it returns None for a
    _StubEngine), budget_exhausted must be emitted, and the slow worker must
    not have run to completion.

    Internal deadline/budget cancellation is a normal terminal state — it does
    NOT propagate as CancelledError to the caller (that is reserved for
    external cancellation).  See also test_external_cancellation_propagates
    and test_deadline_cancels_in_flight_operate_with_repair.
    """
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
    # Internal deadline cancellation returns normally (partial export = None for stub).
    result = await eng.run(on_event=events.append)
    assert result is None, f"Expected None from stub partial export, got {result!r}"

    # Give the event loop one tick for the spawned task to finish cancelling.
    await asyncio.sleep(0)

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
# CodingEngine normalize-before-gate
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
# CLI-aware emission repair instruction
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


# ---------------------------------------------------------------------------
# Deadline cancels in-flight operate_with_repair
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deadline_cancels_in_flight_operate_with_repair():
    """branch.operate() blocked past deadline_s must be cancelled by the watchdog."""
    import time

    from lionagi.engines.engine import EngineRun

    operate_completed = asyncio.Event()
    operate_started = asyncio.Event()

    class _SlowBranch:
        name = "slow"
        chat_model = SimpleNamespace(is_cli=False)

        async def operate(self, *, instruction):
            operate_started.set()
            await asyncio.sleep(10)  # far past the 0.02 s deadline
            operate_completed.set()
            return "done"

    class _SlowEngine(Engine):
        async def _run(self, run: EngineRun, *a, **kw):
            branch = _SlowBranch()
            await run.operate_with_repair(
                branch,
                "do work",
                arrived=lambda: False,  # never arrives — only deadline stops it
                emits=(),
                retries=0,
            )
            return "completed"

    eng = _SlowEngine(deadline_s=0.02)
    t0 = time.monotonic()

    events: list[dict] = []
    # Internal deadline cancellation returns normally; partial export = None for stub.
    result = await eng.run(on_event=events.append)
    assert result is None, f"Expected None from stub partial export, got {result!r}"

    elapsed = time.monotonic() - t0

    # Must cancel within a reasonable bound (<<10 s — the sleep duration).
    assert elapsed < 2.0, f"deadline overrun: ran for {elapsed:.2f}s, expected <2s"
    assert not operate_completed.is_set(), "branch.operate must not have completed"
    assert any(e["type"] == "budget_exhausted" for e in events), (
        f"budget_exhausted not emitted; events: {[e['type'] for e in events]}"
    )


# ---------------------------------------------------------------------------
# CLI repair example is syntactically valid JSON
# ---------------------------------------------------------------------------


def test_cli_repair_instruction_example_is_valid_json():
    """The fenced block in the CLI repair instruction must be parseable by
    json.loads() without modification — a worker who copies it literally
    gets a valid emission, not a JSON parse error."""
    import json
    import re

    from lionagi.engines.coding import WorkPlanned

    hint = emission_keys((WorkPlanned,))
    msg = _cli_repair_instruction(hint, (WorkPlanned,))

    # Extract the fenced code block.  The pattern requires a newline after
    # the opening fence to avoid matching inline ``` references in prose.
    m = re.search(r"```json\n(.*?)\n```", msg, re.DOTALL)
    assert m is not None, "CLI repair instruction must contain a fenced JSON block"
    fenced_content = m.group(1)

    # Must parse without error.
    parsed = json.loads(fenced_content)
    assert isinstance(parsed, dict), "fenced block must be a JSON object"
    assert "work_planned" in parsed, (
        f"emission key 'work_planned' missing from fenced block; got keys: {list(parsed)}"
    )


def test_cli_repair_instruction_fenced_block_validates_against_emission():
    """The fenced block must not only parse as JSON but also validate against
    the actual emission model — running it through the pipeline's extraction
    path must produce a valid typed bundle, not a rejection."""
    import json
    import re

    from lionagi.casts.emission import build_emission_operable
    from lionagi.engines.coding import WorkPlanned
    from lionagi.operations._observe import attempt_extract

    hint = emission_keys((WorkPlanned,))
    msg = _cli_repair_instruction(hint, (WorkPlanned,))

    m = re.search(r"```json\n(.*?)\n```", msg, re.DOTALL)
    assert m is not None, "CLI repair instruction must contain a fenced JSON block"
    fenced_content = m.group(1)

    # Simulate what the pipeline does: wrap in a fenced code block (as it
    # would appear in a model response) and run attempt_extract().
    response_text = f"```json\n{fenced_content}\n```"
    capabilities = build_emission_operable((WorkPlanned,))
    bundles, violations, rejects = attempt_extract(response_text, capabilities)

    assert not violations, f"emission had violations: {violations}"
    assert not rejects, f"emission was rejected: {rejects}"
    assert bundles, (
        "attempt_extract returned no bundles — the example JSON did not parse into "
        f"a WorkPlanned instance; fenced_content={fenced_content!r}"
    )


# ---------------------------------------------------------------------------
# _active tasks are drained even when _run() raises immediately
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_tasks_drained_when_run_raises():
    """_run() raising immediately must cancel spawned tasks and leave _active empty."""
    spawned_cancelled = asyncio.Event()

    class _LeakyEngine(Engine):
        async def _run(self, run, *a, **kw):
            async def long_background():
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    spawned_cancelled.set()
                    raise

            run.spawn(long_background())
            raise RuntimeError("_run failed immediately")

    eng = _LeakyEngine()

    with pytest.raises(RuntimeError, match="_run failed immediately"):
        await eng.run()

    # Give the event loop one tick to process the cancellation.
    await asyncio.sleep(0)

    assert spawned_cancelled.is_set(), "spawned task was not cancelled in the finalizer"


async def test_external_cancel_during_drain_propagates():
    """Cancelling Engine.run() mid-drain must propagate CancelledError only after
    all run-owned tasks complete — no task outlives the caller's cancellation."""
    in_drain = asyncio.Event()
    child_cleanup_done = asyncio.Event()
    captured_runs = []

    class _SlowDrainEngine(Engine):
        def new_run(self, **kw):
            run = super().new_run(**kw)
            captured_runs.append(run)
            return run

        async def _run(self, run, *a, **kw):
            async def stubborn_child():
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    in_drain.set()
                    # Delay our own cancellation so run() sits in the drain
                    # await long enough for the caller to cancel it.
                    await asyncio.sleep(0.3)
                    child_cleanup_done.set()
                    raise

            run.spawn(stubborn_child())
            return "ok"

    eng = _SlowDrainEngine()
    outer = asyncio.ensure_future(eng.run())

    await asyncio.wait_for(in_drain.wait(), timeout=5)
    outer.cancel()

    with pytest.raises(asyncio.CancelledError):
        await outer

    # The caller must observe cancellation only after the drain finished:
    # the child's cleanup ran to completion and nothing is left in _active.
    assert child_cleanup_done.is_set(), "caller observed cancellation before child cleanup finished"
    assert captured_runs and not captured_runs[0]._active, (
        "run._active not drained at caller-visible exit"
    )


# ---------------------------------------------------------------------------
# _normalize_spec called exactly once via run()
# ---------------------------------------------------------------------------


def test_coding_engine_normalizes_spec_exactly_once(monkeypatch):
    """_normalize_spec must be called exactly once when CodingEngine.run() is invoked."""
    import lionagi.engines.coding as _coding_mod

    call_count = 0
    _original = _coding_mod._normalize_spec

    def _counting_normalize(spec):
        nonlocal call_count
        call_count += 1
        return _original(spec)

    monkeypatch.setattr(_coding_mod, "_normalize_spec", _counting_normalize)

    # We only want to test the normalization count; stop before agents are made.
    eng = CodingEngine()

    # Patch new_run so the run never actually executes agent stages.
    class _EarlyExit(Exception):
        pass

    original_new_run = eng.new_run

    def _spy_new_run(**kw):
        run = original_new_run(**kw)
        # Patch _run on the engine instance to raise immediately — we only
        # care that _normalize_spec was called the right number of times by
        # the run() → _run() dispatch.
        return run

    monkeypatch.setattr(eng, "new_run", _spy_new_run)

    import asyncio as _asyncio

    async def _check():
        # Override _run to exit immediately after normalization path.
        original_run_inner = eng._run

        async def _fake_run(
            run, spec, *, test_cmd, workspace=None, export_dir=None, _normalized=None
        ):
            # Just count how many times _normalize_spec was called up to here.
            # _normalized kwarg should carry the pre-normalized pair.
            assert _normalized is not None, (
                "_run() must receive the pre-normalized pair from run(); "
                "got _normalized=None which means run() did not forward it"
            )
            raise _EarlyExit()

        eng._run = _fake_run
        with pytest.raises(_EarlyExit):
            await eng.run("do something", test_cmd=["true"])

    _asyncio.run(_check())

    assert call_count == 1, (
        f"_normalize_spec was called {call_count} times; expected exactly 1 "
        f"(run() normalizes once and passes the result to _run())"
    )
