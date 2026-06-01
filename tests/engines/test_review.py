# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ReviewEngine logic — dimensional fan-out, adversarial verify, verdict. No LLM."""

from __future__ import annotations

import pytest

from lionagi.engines.review import IssueFound, ReviewEngine


class _FakeAgent:
    def __init__(self, name: str, recorder: list):
        self.name = name
        self._rec = recorder

    async def operate(self, *, instruction: str):
        self._rec.append(instruction)
        return None


@pytest.mark.asyncio
async def test_dimensions_fan_out_in_parallel():
    eng = ReviewEngine(dimensions=("correctness", "security"))
    run = eng.new_run()
    seen: list[str] = []

    async def fake_make(role, *, name=None, **kw):
        return _FakeAgent(name or role, seen)

    run.make_agent = fake_make
    await eng._run(run, "ARTIFACT-BODY")
    # one reviewer per dimension + one verdict author
    assert any("correctness" in s for s in seen)
    assert any("security" in s for s in seen)
    assert any("ARTIFACT-BODY" in s for s in seen)


@pytest.mark.asyncio
async def test_critical_issue_spawns_adversarial_verify():
    eng = ReviewEngine()
    run = eng.new_run()
    verified: list[str] = []

    async def rec(_run, issue):
        verified.append(issue.description)

    eng._verify = rec
    run.observe(IssueFound, lambda i, _c: eng._on_issue(run, i))

    await run.emit(IssueFound(dimension="security", description="sqli", severity="critical"))
    await run.emit(IssueFound(dimension="style", description="nit", severity="minor"))
    await run.wait_quiescence()
    assert verified == ["sqli"]  # only the critical one


@pytest.mark.asyncio
async def test_verify_dedups_same_issue():
    eng = ReviewEngine()
    run = eng.new_run()
    verified: list[str] = []

    async def rec(_run, issue):
        verified.append(issue.description)

    eng._verify = rec
    run.observe(IssueFound, lambda i, _c: eng._on_issue(run, i))

    await run.emit(IssueFound(dimension="security", description="dup", severity="critical"))
    await run.emit(IssueFound(dimension="correctness", description="dup", severity="major"))
    await run.wait_quiescence()
    assert verified == ["dup"]  # deduped by description


@pytest.mark.asyncio
async def test_verdict_reads_issues_from_store():
    eng = ReviewEngine()
    run = eng.new_run()
    await run.emit(IssueFound(dimension="security", description="X-issue", severity="major"))

    captured: dict = {}

    class FakeSynth:
        name = "verdict"

        async def operate(self, *, instruction):
            captured["instruction"] = instruction
            return "REQUEST-CHANGES"

    async def fake_make(role, **kw):
        return FakeSynth()

    run.make_agent = fake_make
    out = await eng._verdict(run, "ART", ("security",))
    assert out == "REQUEST-CHANGES"
    assert "X-issue" in captured["instruction"]
