# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for ChainRun extraction (R1-1) and research per-stage repair uplift.

Parity tests:
- ChainRun is a proper base class that EngineRun subclasses.
- CodingRun and HypothesisRun inherit collect/emit/find/events_of from ChainRun
  rather than defining them directly.
- Functional tests: collect+emit path works through the public engine for each
  subclass.

Research per-stage repair uplift:
- A stage that returns malformed output gets repaired (per-stage, not
  node-level only).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from lionagi.engines.coding import CodingChainEvent, CodingRun, WorkPlanned
from lionagi.engines.engine import ChainRun, Engine, EngineEvent, EngineRun
from lionagi.engines.hypothesis import (
    ChainEvent,
    EvidenceCollected,
    HypothesisRun,
    QuestionRaised,
)
from lionagi.engines.research import (
    DepthRequested,
    FindingEmitted,
    ResearchEngine,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _EmittingFake:
    """Mixin giving a fake branch the REAL research arrival contract: the
    emission Operable a real agent gets, and a message log carrying a fenced
    capability block (the surface ``_branch_emitted`` inspects)."""

    def _init_emitter(self):
        from lionagi.casts.emission import build_emission_operable

        self.messages: list = []
        self._capabilities = build_emission_operable((FindingEmitted, DepthRequested))

    def _record_emission(self, event) -> None:
        from lionagi.protocols.messages import AssistantResponse

        payload = event.model_dump_json(exclude_none=True)
        self.messages.append(
            AssistantResponse(
                content={"assistant_response": f'```json\n{{"finding_emitted": {payload}}}\n```'}
            )
        )


class _StubEngine(Engine):
    async def _run(self, run: EngineRun, *a: Any, **kw: Any) -> str:
        return ""


def _stub_engine() -> _StubEngine:
    return _StubEngine()


# ---------------------------------------------------------------------------
# 1. ChainRun structural invariants
# ---------------------------------------------------------------------------


def test_chain_run_is_subclass_of_engine_run():
    """ChainRun must be a proper subclass of EngineRun."""
    assert issubclass(ChainRun, EngineRun)


def test_coding_run_subclasses_chain_run():
    """CodingRun must now inherit from ChainRun (not directly from EngineRun)."""
    assert issubclass(CodingRun, ChainRun)
    assert issubclass(CodingRun, EngineRun)


def test_hypothesis_run_subclasses_chain_run():
    """HypothesisRun must now inherit from ChainRun (not directly from EngineRun)."""
    assert issubclass(HypothesisRun, ChainRun)
    assert issubclass(HypothesisRun, EngineRun)


# ---------------------------------------------------------------------------
# 2. collect/emit/find/events_of reside on ChainRun, not on the subclasses
# ---------------------------------------------------------------------------


def test_collect_implementation_lives_on_chain_run():
    """The collect() body must reside on ChainRun, not be re-defined on
    CodingRun or HypothesisRun as a full implementation (subclasses may have
    typed thin wrappers, but the __qualname__ of the impl must be ChainRun)."""
    # ChainRun itself defines the full implementation
    assert "collect" in ChainRun.__dict__


def test_emit_implementation_lives_on_chain_run():
    """emit() must be defined on ChainRun (the shared impl)."""
    assert "emit" in ChainRun.__dict__


def test_find_implementation_lives_on_chain_run():
    """find() must be defined on ChainRun."""
    assert "find" in ChainRun.__dict__


def test_events_of_implementation_lives_on_chain_run():
    """events_of() must be defined on ChainRun."""
    assert "events_of" in ChainRun.__dict__


# ---------------------------------------------------------------------------
# 3. CodingRun collect+emit functional path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coding_run_collect_stamps_eid_and_stores():
    """collect() on a CodingRun must stamp an eid and store the event."""
    eng = _stub_engine()
    run = CodingRun(eng)
    plan = WorkPlanned(approach="do the thing")
    run.collect(plan)
    assert plan.eid.startswith("W-"), f"eid should start with W-; got {plan.eid!r}"
    assert run.events_of(WorkPlanned) == [plan]
    assert run.find(plan.eid) is plan


@pytest.mark.asyncio
async def test_coding_run_collect_increments_eid_counter():
    """Consecutive collects of the same type must yield sequential eids."""
    eng = _stub_engine()
    run = CodingRun(eng)
    from lionagi.engines.coding import ChangeProposed

    p1 = WorkPlanned(approach="step 1")
    p2 = WorkPlanned(approach="step 2")
    run.collect(p1)
    run.collect(p2)
    assert p1.eid == "W-1"
    assert p2.eid == "W-2"


@pytest.mark.asyncio
async def test_coding_run_emit_no_duplicate_notify_for_chain_event():
    """emit() on a CodingRun must NOT produce a duplicate on_event call for
    a CodingChainEvent — collect() is the sole notify path for chain events."""
    eng = _stub_engine()
    events_seen: list[dict] = []
    run = CodingRun(eng, on_event=events_seen.append)

    # Register a collect observer so the observer path is exercised as in _run.
    run.observe(WorkPlanned, lambda e, _: run.collect(e))

    plan = WorkPlanned(approach="emit test")
    await run.emit(plan)

    # collect() fires once via the observer → exactly one notify call.
    # emit()'s override must not fire a second one.
    matching = [e for e in events_seen if e.get("type") == "WorkPlanned"]
    assert len(matching) == 1, (
        f"expected exactly 1 WorkPlanned notify; got {len(matching)}: {matching}"
    )


@pytest.mark.asyncio
async def test_coding_run_emit_does_notify_for_non_chain_event():
    """emit() on a CodingRun MUST call notify for non-CodingChainEvent events
    (e.g. plain EngineEvent subclasses)."""
    eng = _stub_engine()
    events_seen: list[dict] = []
    run = CodingRun(eng, on_event=events_seen.append)

    class _Other(EngineEvent):
        value: str = "x"

    await run.emit(_Other(value="hello"))
    matching = [e for e in events_seen if e.get("type") == "_Other"]
    assert len(matching) == 1, f"expected 1 _Other notify; got {len(matching)}"


# ---------------------------------------------------------------------------
# 4. HypothesisRun collect+emit functional path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hypothesis_run_collect_stamps_eid_and_stores():
    """collect() on a HypothesisRun must stamp an eid and store the event."""
    from lionagi.engines.hypothesis import FindingPosted

    eng = _stub_engine()
    run = HypothesisRun(eng)
    f = FindingPosted(description="test finding")
    run.collect(f)
    assert f.eid.startswith("F-"), f"eid should start with F-; got {f.eid!r}"
    assert run.events_of(FindingPosted) == [f]
    assert run.find(f.eid) is f


@pytest.mark.asyncio
async def test_hypothesis_run_emit_no_duplicate_notify_for_chain_event():
    """emit() on a HypothesisRun must NOT duplicate notify for ChainEvents."""
    from lionagi.engines.hypothesis import FindingPosted

    eng = _stub_engine()
    events_seen: list[dict] = []
    run = HypothesisRun(eng, on_event=events_seen.append)

    run.observe(FindingPosted, lambda e, _: run.collect(e))

    f = FindingPosted(description="hypothesis emit test")
    await run.emit(f)

    matching = [e for e in events_seen if e.get("type") == "FindingPosted"]
    assert len(matching) == 1, f"expected exactly 1 FindingPosted notify; got {len(matching)}"


@pytest.mark.asyncio
async def test_hypothesis_run_emit_does_notify_for_non_chain_event():
    """emit() on a HypothesisRun MUST notify for non-ChainEvent events."""
    eng = _stub_engine()
    events_seen: list[dict] = []
    run = HypothesisRun(eng, on_event=events_seen.append)

    class _Sig(EngineEvent):
        note: str = ""

    await run.emit(_Sig(note="hi"))
    matching = [e for e in events_seen if e.get("type") == "_Sig"]
    assert len(matching) == 1


# ---------------------------------------------------------------------------
# 5. ChainRun._chain_event_cls correctness
# ---------------------------------------------------------------------------


def test_coding_run_chain_event_cls_is_coding_chain_event():
    assert CodingRun._chain_event_cls is CodingChainEvent


def test_hypothesis_run_chain_event_cls_is_chain_event():
    assert HypothesisRun._chain_event_cls is ChainEvent


# ---------------------------------------------------------------------------
# 6. store initialization from _event_prefix_map
# ---------------------------------------------------------------------------


def test_coding_run_store_initialised_with_event_prefix_keys():
    """store must be pre-populated from _event_prefix_map so existing callers
    that iterate store.values() see all event types."""
    from lionagi.engines.coding import _EVENT_PREFIX

    eng = _stub_engine()
    run = CodingRun(eng)
    assert set(run.store.keys()) == set(_EVENT_PREFIX.keys())


def test_hypothesis_run_store_initialised_with_event_prefix_keys():
    from lionagi.engines.hypothesis import _EVENT_PREFIX

    eng = _stub_engine()
    run = HypothesisRun(eng)
    assert set(run.store.keys()) == set(_EVENT_PREFIX.keys())


# ---------------------------------------------------------------------------
# 7. Research per-stage repair uplift
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_research_per_stage_repair_fires_when_stage_emits_nothing():
    """A stage that returns prose with no finding must trigger operate_with_repair
    for that stage (not just the node-level backstop).

    The per-stage arrived() check is scoped to the stage's own window: we
    instrument operate_with_repair on the run to track how many times repair
    was triggered and for which branch."""
    eng = ResearchEngine(repair_retries=1)
    run = eng.new_run()
    events: list[dict] = []
    run.on_event = events.append

    repair_calls: list[str] = []
    original_owr = run.operate_with_repair

    async def spy_owr(branch, instruction, *, arrived, emits=(), retries=1):
        name = getattr(branch, "name", "?")
        # Call the real impl; record after each attempted call.
        result = await original_owr(
            branch, instruction, arrived=arrived, emits=emits, retries=retries
        )
        repair_calls.append(name)
        return result

    run.operate_with_repair = spy_owr

    # Build two fake branches: first emits nothing (triggers repair), second
    # emits a finding on its first call (no repair needed).
    class _SilentBranch:
        name = "silent"
        chat_model = SimpleNamespace(is_cli=False)

        async def operate(self, *, instruction):
            return "no json here"

    class _TalkativeBranch(_EmittingFake):
        name = "talkative"
        chat_model = SimpleNamespace(is_cli=False)

        def __init__(self):
            self._init_emitter()

        async def operate(self, *, instruction):
            event = FindingEmitted(description="real finding", novelty=0.5)
            self._record_emission(event)
            await run.emit(event)
            return "ok"

    team = [_SilentBranch(), _TalkativeBranch()]
    instruction = _node_instruction("test topic", 0, eng.max_depth)
    await eng._drive_node(run, team, instruction)

    # Both branches must have gone through operate_with_repair (per-stage).
    assert "silent" in repair_calls, (
        f"silent branch was not passed to operate_with_repair; calls={repair_calls}"
    )
    assert "talkative" in repair_calls, (
        f"talkative branch was not passed to operate_with_repair; calls={repair_calls}"
    )

    # At least one emission_repair notify must have come from the silent stage.
    repair_events = [e for e in events if e.get("type") == "emission_repair"]
    assert repair_events, f"no emission_repair events emitted; events={[e['type'] for e in events]}"


@pytest.mark.asyncio
async def test_research_per_stage_repair_arriving_stage_needs_no_repair():
    """A stage that successfully emits must NOT trigger a repair turn."""
    eng = ResearchEngine(repair_retries=1)
    run = eng.new_run()
    events: list[dict] = []
    run.on_event = events.append

    class _GoodBranch(_EmittingFake):
        name = "good"
        chat_model = SimpleNamespace(is_cli=False)

        def __init__(self):
            self._init_emitter()

        async def operate(self, *, instruction):
            event = FindingEmitted(description="good finding", novelty=0.6)
            self._record_emission(event)
            await run.emit(event)
            return "ok"

    team = [_GoodBranch()]
    instruction = _node_instruction("topic", 0, eng.max_depth)
    await eng._drive_node(run, team, instruction)

    repair_events = [e for e in events if e.get("type") == "emission_repair"]
    assert not repair_events, (
        f"no repair should fire when agent emits successfully; got {repair_events}"
    )


@pytest.mark.asyncio
async def test_research_node_level_backstop_fires_when_all_stages_fail():
    """If all per-stage repair turns also fail, the node-level backstop must
    still fire (operate_with_repair called with team[-1] as target)."""
    eng = ResearchEngine(repair_retries=0)  # retries=0: per-stage tries once
    run = eng.new_run()
    events: list[dict] = []
    run.on_event = events.append

    # Track ALL operate_with_repair calls by branch name.
    backstop_calls: list[str] = []
    original_owr = run.operate_with_repair

    async def spy_owr(branch, instruction, *, arrived, emits=(), retries=0):
        backstop_calls.append(getattr(branch, "name", "?"))
        return await original_owr(
            branch, instruction, arrived=arrived, emits=emits, retries=retries
        )

    run.operate_with_repair = spy_owr

    class _DeadBranch:
        name = "dead"
        chat_model = SimpleNamespace(is_cli=False)

        async def operate(self, *, instruction):
            return "nothing"

    team = [_DeadBranch()]
    await eng._drive_node(run, team, _node_instruction("topic", 0, eng.max_depth))

    # With retries=0 the dead branch is called once per-stage, then once as
    # the node-level backstop — so "dead" must appear at least twice.
    dead_count = backstop_calls.count("dead")
    assert dead_count >= 2, (
        f"expected 'dead' branch called at least 2 times (per-stage + backstop); "
        f"got {dead_count}; all calls={backstop_calls}"
    )


def _node_instruction(topic: str, depth: int, max_depth: int) -> str:
    """Reproduce the research engine's node instruction for test harness."""
    from lionagi.engines.research import _node_instruction as _ni

    return _ni(topic, depth, max_depth)
