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
