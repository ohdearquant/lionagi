# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ResearchEngine reaction logic — depth spawning, dedup, synthesis. No LLM."""

from __future__ import annotations

import pytest

from lionagi.engines.research import (
    DepthRequested,
    FindingEmitted,
    ResearchEngine,
)


def _wire(eng, run):
    """Register the engine's reactions on a run, as ``_run`` does."""
    run.observe(FindingEmitted, lambda f, _c: eng._on_finding(run, f))
    run.observe(DepthRequested, lambda d, _c: eng._on_depth(run, d))


@pytest.mark.asyncio
async def test_high_novelty_finding_spawns_deeper_node():
    eng = ResearchEngine(max_depth=2, novelty_threshold=0.7)
    run = eng.new_run()
    explored: list[tuple[str, int]] = []

    async def rec(_run, topic, depth):
        explored.append((topic, depth))

    eng._explore = rec
    _wire(eng, run)
    await run.emit(FindingEmitted(description="deep", novelty=0.9, depth=0))
    await run.wait_quiescence()
    assert explored == [("deep", 1)]


@pytest.mark.asyncio
async def test_low_novelty_does_not_spawn():
    eng = ResearchEngine(novelty_threshold=0.7)
    run = eng.new_run()
    explored: list = []

    async def rec(_run, topic, depth):
        explored.append((topic, depth))

    eng._explore = rec
    _wire(eng, run)
    await run.emit(FindingEmitted(description="meh", novelty=0.3, depth=0))
    await run.wait_quiescence()
    assert explored == []


@pytest.mark.asyncio
async def test_depth_cap_blocks_spawn():
    eng = ResearchEngine(max_depth=2, novelty_threshold=0.7)
    run = eng.new_run()
    explored: list = []

    async def rec(_run, topic, depth):
        explored.append((topic, depth))

    eng._explore = rec
    _wire(eng, run)
    # depth == max_depth → not < max_depth → no spawn
    await run.emit(FindingEmitted(description="x", novelty=0.95, depth=2))
    await run.wait_quiescence()
    assert explored == []


@pytest.mark.asyncio
async def test_depth_requested_spawns():
    eng = ResearchEngine(max_depth=2)
    run = eng.new_run()
    explored: list[tuple[str, int]] = []

    async def rec(_run, topic, depth):
        explored.append((topic, depth))

    eng._explore = rec
    _wire(eng, run)
    await run.emit(DepthRequested(question="subquestion", parent_depth=0))
    await run.wait_quiescence()
    assert explored == [("subquestion", 1)]


@pytest.mark.asyncio
async def test_explore_dedups_normalized_topics():
    """_explore must deduplicate topics that are identical after normalisation.

    The dedup lives in _explore (seen() check), not in _drive_node.  Track via
    _drive_node rather than run_team because _drive_node now drives stages
    directly via operate_with_repair instead of delegating to run_team."""
    eng = ResearchEngine()
    run = eng.new_run()
    nodes_driven: list[str] = []

    async def fake_team(_run, depth):
        return []

    async def fake_drive_node(_run, team, instruction):
        nodes_driven.append(instruction)

    eng._team_for = fake_team
    eng._drive_node = fake_drive_node
    await eng._explore(run, "Quantum Error Correction", 1)
    await eng._explore(run, "quantum error correction", 1)  # same after normalize
    assert len(nodes_driven) == 1


@pytest.mark.asyncio
async def test_synthesis_reads_findings_from_store():
    eng = ResearchEngine()
    run = eng.new_run()
    # default novelty 0.5 < threshold → no exploration triggered
    await run.emit(FindingEmitted(description="A", evidence="e1", depth=0, source="r"))
    await run.emit(FindingEmitted(description="B", evidence="e2", depth=1, source="a"))

    captured: dict = {}

    class FakeSynth:
        name = "synthesizer"

        async def operate(self, *, instruction):
            captured["instruction"] = instruction
            return "SYNTHESIS"

    async def fake_make(role, **kw):
        return FakeSynth()

    run.make_agent = fake_make
    out = await eng._synthesize(run, "the topic")
    assert out == "SYNTHESIS"
    assert "A" in captured["instruction"]
    assert "B" in captured["instruction"]
    assert "the topic" in captured["instruction"]


# -- emission repair (ADR-0077 §3) -------------------------------------------


class _ProseTeamMember:
    """A node's team member that returns prose until ``emit_on_call``, then
    emits — the weak-model failure the per-stage repair loop recovers (mirrors
    the ``_ProseBranch`` in test_engine_protection.py).

    Faithful to the real arrival contract: the member carries the same
    emission ``Operable`` a real research agent gets, and an "emission" is a
    fenced-JSON capability block recorded in the member's own message log (the
    surface ``_branch_emitted`` inspects) — plus the bus event, matching what
    ``_observe`` produces for a real agent."""

    name = "researcher-d0"
    chat_model = None  # no is_cli → falls back to API repair template

    def __init__(self, run, emit_on_call: int, event):
        from lionagi.casts.emission import build_emission_operable

        self._run = run
        self._event = event
        self._emit_on = emit_on_call
        self.calls: list[str] = []
        self.messages: list = []
        self._capabilities = build_emission_operable((FindingEmitted, DepthRequested))

    def _record(self, text: str) -> None:
        from lionagi.protocols.messages import AssistantResponse

        self.messages.append(AssistantResponse(content={"assistant_response": text}))

    async def operate(self, *, instruction):
        self.calls.append(instruction)
        if len(self.calls) == self._emit_on:
            payload = self._event.model_dump_json(exclude_none=True)
            self._record(f'```json\n{{"finding_emitted": {payload}}}\n```')
            await self._run.emit(self._event)
        else:
            self._record("prose")
        return "prose"


@pytest.mark.asyncio
async def test_drive_node_repairs_when_team_emits_nothing():
    """A stage that returned prose on the first call gets a repair turn; the
    repair turn lands the finding and an ``emission_repair`` notify fires.

    Per-stage repair call sequence (repair_retries=1, 1-member team):
      call 1: per-stage first operate (original instruction) — no emit
      call 2: per-stage repair turn (repair instruction)    — emits finding
    Total: 2 calls; the repair instruction is in calls[1]."""
    eng = ResearchEngine(repair_retries=1)
    run = eng.new_run()
    events: list[dict] = []
    run.on_event = events.append
    # emit on call 2: per-stage first(1) → per-stage repair(2, emits).
    member = _ProseTeamMember(run, 2, FindingEmitted(description="late finding", novelty=0.1))

    await eng._drive_node(run, [member], "Research topic (depth 0/1): X")
    await run.wait_quiescence()

    assert len(member.calls) == 2
    assert "produced no valid emission" in member.calls[1]
    assert "finding_emitted" in member.calls[1]  # repair names the expected key
    assert any(e["type"] == "emission_repair" for e in events)
    assert len(run.by_type(FindingEmitted)) == 1


@pytest.mark.asyncio
async def test_drive_node_no_repair_when_team_emits():
    """A node that emits on the first team pass costs no extra agent call — the
    arrived() pre-check short-circuits before any repair operate."""
    eng = ResearchEngine(repair_retries=1)
    run = eng.new_run()
    events: list[dict] = []
    run.on_event = events.append
    member = _ProseTeamMember(run, 1, FindingEmitted(description="early", novelty=0.1))

    await eng._drive_node(run, [member], "Research topic (depth 0/1): X")
    await run.wait_quiescence()

    assert len(member.calls) == 1  # per-stage first call emits; no repair needed
    assert not any(e["type"] == "emission_repair" for e in events)
    assert len(run.by_type(FindingEmitted)) == 1


@pytest.mark.asyncio
async def test_drive_node_empty_team_is_noop():
    """An empty team (e.g. a budget-declined node) must be a no-op: no stages
    run, no findings emitted, no repair attempted — preserves the dedup/budget
    contracts."""
    eng = ResearchEngine(repair_retries=2)
    run = eng.new_run()
    events: list[dict] = []
    run.on_event = events.append
    await eng._drive_node(run, [], "Research topic (depth 0/1): X")
    assert len(run.by_type(FindingEmitted)) == 0
    assert not any(e["type"] == "emission_repair" for e in events)


class _SilentMemberWithChildNoise(_ProseTeamMember):
    """A silent member whose turn coincides with a CHILD node's emission
    landing on the run bus — the recursive-research interleaving where
    ``_on_finding`` spawned a deeper exploration mid-node.  The child's
    emission belongs to the run, NOT to this member's messages."""

    async def operate(self, *, instruction):
        self.calls.append(instruction)
        if len(self.calls) == self._emit_on:
            payload = self._event.model_dump_json(exclude_none=True)
            self._record(f'```json\n{{"finding_emitted": {payload}}}\n```')
            await self._run.emit(self._event)
        else:
            # Unrelated child-node finding arrives while this member is
            # mid-turn; the member itself produces only prose.
            await self._run.emit(
                FindingEmitted(description="child node finding", novelty=0.1, depth=1)
            )
            self._record("prose")
        return "prose"


@pytest.mark.asyncio
async def test_drive_node_child_emission_does_not_mask_silent_stage():
    """A concurrent child node's emission must not satisfy another stage's
    repair check.  The silent member still gets its repair turn even though
    the run-level FindingEmitted count rose during its first call — arrival is
    judged from the member's OWN messages, not global store deltas."""
    eng = ResearchEngine(repair_retries=1)
    run = eng.new_run()
    events: list[dict] = []
    run.on_event = events.append
    # First call: child noise + prose. Second call (repair): real emission.
    member = _SilentMemberWithChildNoise(
        run, 2, FindingEmitted(description="repaired finding", novelty=0.1)
    )

    await eng._drive_node(run, [member], "Research topic (depth 0/1): X")
    await run.wait_quiescence()

    assert len(member.calls) == 2, "silent stage must receive a repair turn"
    assert "produced no valid emission" in member.calls[1]
    assert any(e["type"] == "emission_repair" for e in events)
    # Both the child's and the repaired member's findings are on the bus.
    assert len(run.by_type(FindingEmitted)) == 2
