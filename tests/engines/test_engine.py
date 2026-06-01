# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Engine base machinery — event store, reactions, quiescence, team loop. No LLM."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from lionagi.engines import Engine, EngineEvent


class Finding(EngineEvent):
    claim: str
    novelty: float = 0.5


@pytest.mark.asyncio
async def test_emit_records_and_queries():
    eng = Engine()
    await eng.emit(Finding(claim="x", novelty=0.9))
    await eng.emit(Finding(claim="y", novelty=0.2))
    assert len(eng.by_type(Finding)) == 2
    # the emission store is queryable via pile[type] (Phase A)
    assert len(eng.events[Finding]) == 2


@pytest.mark.asyncio
async def test_observe_reacts_to_type():
    eng = Engine()
    seen: list[str] = []

    @eng.observe(Finding)
    def _on(f, _ctx):
        seen.append(f.claim)

    await eng.emit(Finding(claim="hit"))
    assert seen == ["hit"]


@pytest.mark.asyncio
async def test_observe_with_field_filter():
    from lionagi.ln.types import Spec

    eng = Engine()
    high: list[Finding] = []

    @eng.observe(Spec(float, name="novelty").q > 0.7)
    def _on(f, _ctx):
        high.append(f)

    await eng.emit(Finding(claim="lo", novelty=0.1))
    await eng.emit(Finding(claim="hi", novelty=0.9))
    assert [f.claim for f in high] == ["hi"]


@pytest.mark.asyncio
async def test_spawn_and_quiescence():
    eng = Engine()
    done: list[int] = []

    async def work(n: int) -> None:
        await asyncio.sleep(0.01)
        done.append(n)

    eng.spawn(work(1))
    eng.spawn(work(2))
    await eng.wait_quiescence()
    assert sorted(done) == [1, 2]


@pytest.mark.asyncio
async def test_observer_spawns_depth_node():
    """The canonical engine loop: an emission triggers a spawned task."""
    eng = Engine(max_depth=2)
    expanded: list[str] = []

    async def deeper(claim: str) -> None:
        await asyncio.sleep(0)
        expanded.append(claim)

    @eng.observe(Finding)
    def _on(f, _ctx):
        if f.novelty > 0.7:
            eng.spawn(deeper(f.claim))

    await eng.emit(Finding(claim="deep", novelty=0.9))
    await eng.emit(Finding(claim="shallow", novelty=0.3))
    await eng.wait_quiescence()
    assert expanded == ["deep"]


@pytest.mark.asyncio
async def test_run_team_sequences_and_carries_output():
    eng = Engine()
    calls: list[tuple[str, str]] = []

    def fake(name: str, reply: str):
        async def operate(*, instruction: str):
            calls.append((name, instruction))
            return reply

        return SimpleNamespace(name=name, operate=operate)

    team = [fake("a", "AOUT"), fake("b", "BOUT")]
    last = await eng.run_team(team, "do the task")
    assert last == "BOUT"
    assert calls[0] == ("a", "do the task")
    assert "AOUT" in calls[1][1]  # b builds on a's output


@pytest.mark.asyncio
async def test_run_team_survives_agent_failure():
    eng = Engine()

    def boom(name: str):
        async def operate(*, instruction: str):
            raise RuntimeError("kaboom")

        return SimpleNamespace(name=name, operate=operate)

    def ok(name: str):
        async def operate(*, instruction: str):
            return "recovered"

        return SimpleNamespace(name=name, operate=operate)

    last = await eng.run_team([boom("x"), ok("y")], "go")
    assert last == "recovered"  # team continued past the failure


@pytest.mark.asyncio
async def test_make_agent_builds_casts_branch_with_emissions():
    eng = Engine()
    b = await eng.make_agent("researcher", name="r1", emits=(Finding,))
    assert b.name == "r1"
    assert b in eng.session.branches
    # emissions granted as capabilities
    assert b.capabilities is not None
    # casts role body composed into the system message
    assert b.system is not None
