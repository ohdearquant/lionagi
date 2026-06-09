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
    eng = ResearchEngine()
    run = eng.new_run()
    teams_run: list[str] = []

    async def fake_team(_run, depth):
        return []

    async def fake_run_team(team, instruction, **kw):
        teams_run.append(instruction)
        return ""

    eng._team_for = fake_team
    run.run_team = fake_run_team
    await eng._explore(run, "Quantum Error Correction", 1)
    await eng._explore(run, "quantum error correction", 1)  # same after normalize
    assert len(teams_run) == 1


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
    emits — the weak-model failure the repair loop recovers (mirrors the
    ``_ProseBranch`` in test_engine_protection.py)."""

    name = "researcher-d0"

    def __init__(self, run, emit_on_call: int, event):
        self._run = run
        self._event = event
        self._emit_on = emit_on_call
        self.calls: list[str] = []

    async def operate(self, *, instruction):
        self.calls.append(instruction)
        if len(self.calls) == self._emit_on:
            await self._run.emit(self._event)
        return "prose"


@pytest.mark.asyncio
async def test_drive_node_repairs_when_team_emits_nothing():
    """A node whose whole team returned prose gets re-prompted; the repair turn
    lands the finding and an ``emission_repair`` notify fires."""
    eng = ResearchEngine(repair_retries=1)
    run = eng.new_run()
    events: list[dict] = []
    run.on_event = events.append
    # emit only on the 3rd call: run_team(1) → repair first-operate(2) → repair turn(3).
    member = _ProseTeamMember(run, 3, FindingEmitted(description="late finding", novelty=0.1))

    await eng._drive_node(run, [member], "Research topic (depth 0/1): X")
    await run.wait_quiescence()

    assert len(member.calls) == 3
    assert "produced no valid emission" in member.calls[2]
    assert "finding_emitted" in member.calls[2]  # repair names the expected key
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

    assert len(member.calls) == 1  # run_team only; no repair operate
    assert not any(e["type"] == "emission_repair" for e in events)
    assert len(run.by_type(FindingEmitted)) == 1


@pytest.mark.asyncio
async def test_drive_node_empty_team_is_noop():
    """An empty team (e.g. a budget-declined node) drives run_team once and never
    attempts repair — preserves the dedup/budget contracts."""
    eng = ResearchEngine(repair_retries=2)
    run = eng.new_run()
    teams_run: list[str] = []

    async def fake_run_team(team, instruction, **kw):
        teams_run.append(instruction)
        return ""

    run.run_team = fake_run_team
    await eng._drive_node(run, [], "Research topic (depth 0/1): X")
    assert teams_run == ["Research topic (depth 0/1): X"]
    assert len(run.by_type(FindingEmitted)) == 0
