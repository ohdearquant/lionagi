# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ReviewEngine logic — dimensional fan-out, adversarial verify, verdict. No LLM."""

from __future__ import annotations

import pytest

from lionagi.engines.review import (
    DimensionClean,
    IssueFound,
    ReviewEngine,
    VerifyResult,
    _verify_instruction,
    _verify_ref,
)


class _FakeAgent:
    def __init__(self, name: str, recorder: list):
        self.name = name
        self._rec = recorder

    async def operate(self, *, instruction: str):
        self._rec.append(instruction)
        return None


class _ProseAgent:
    """Returns prose until ``emit_on_call``, then emits — simulates weak-model failure; 0=never emits."""

    def __init__(self, run, name: str, emit_on_call: int, event):
        self.name = name
        self._run = run
        self._event = event
        self._emit_on = emit_on_call
        self.calls: list[str] = []

    async def operate(self, *, instruction: str):
        self.calls.append(instruction)
        if self._emit_on and len(self.calls) == self._emit_on:
            await self._run.emit(self._event)
        return "prose"


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


# -- emission repair (ADR-0034 §3) -------------------------------------------


@pytest.mark.asyncio
async def test_review_dimension_repairs_prose_reviewer():
    """A reviewer that returns prose first gets re-prompted; the repair turn
    lands the issue and an ``emission_repair`` notify fires."""
    eng = ReviewEngine(repair_retries=1)
    run = eng.new_run()
    events: list[dict] = []
    run.on_event = events.append
    issue = IssueFound(dimension="security", description="sqli", severity="critical")
    agent = _ProseAgent(run, "review-security", emit_on_call=2, event=issue)

    async def fake_make(role, **kw):
        return agent

    run.make_agent = fake_make
    await eng._review_dimension(run, "ARTIFACT", "security")

    assert len(agent.calls) == 2  # initial operate (prose) + repair turn (emits)
    assert "produced no valid emission" in agent.calls[1]
    assert "issue_found" in agent.calls[1]
    assert any(e["type"] == "emission_repair" for e in events)
    assert len(run.by_type(IssueFound)) == 1


@pytest.mark.asyncio
async def test_review_dimension_clean_emission_satisfies_arrival():
    """A clean dimension is an affirmative dimension_clean emission on the
    first turn — no repair round, no emission_missing, nothing fabricated."""
    eng = ReviewEngine(repair_retries=1)
    run = eng.new_run()
    events: list[dict] = []
    run.on_event = events.append
    clean = DimensionClean(dimension="style", rationale="naming and layout are consistent")
    agent = _ProseAgent(run, "review-style", emit_on_call=1, event=clean)

    async def fake_make(role, **kw):
        return agent

    run.make_agent = fake_make
    await eng._review_dimension(run, "ARTIFACT", "style")

    assert len(agent.calls) == 1  # arrival satisfied, no repair burn
    assert not any(e["type"] in ("emission_repair", "emission_missing") for e in events)
    assert len(run.by_type(IssueFound)) == 0
    assert run.by_type(DimensionClean)[0].dimension == "style"
    assert "dimension_clean" in agent.calls[0]  # the clean path is instructed, not hoped for


@pytest.mark.asyncio
async def test_review_dimension_silent_reviewer_is_transport_failure():
    """A reviewer that emits neither an issue nor a dimension_clean is a
    transport failure: nudged once, then emission_missing — the repair never
    invents an issue."""
    eng = ReviewEngine(repair_retries=1)
    run = eng.new_run()
    events: list[dict] = []
    run.on_event = events.append
    agent = _ProseAgent(run, "review-style", emit_on_call=0, event=None)

    async def fake_make(role, **kw):
        return agent

    run.make_agent = fake_make
    await eng._review_dimension(run, "ARTIFACT", "style")

    assert len(agent.calls) == 2  # one repair nudge attempted
    assert any(e["type"] == "emission_missing" for e in events)
    assert len(run.by_type(IssueFound)) == 0  # nothing fabricated
    assert len(run.by_type(DimensionClean)) == 0


@pytest.mark.asyncio
async def test_verify_repairs_prose_verifier():
    """The adversarial verifier always owes a verdict, so a prose first response
    is repaired into a VerifyResult."""
    eng = ReviewEngine(repair_retries=1)
    run = eng.new_run()
    events: list[dict] = []
    run.on_event = events.append
    issue = IssueFound(dimension="security", description="sqli", severity="critical")
    result = VerifyResult(issue="sqli", holds=True, rationale="boundary test confirms")
    agent = _ProseAgent(run, "verify-security", emit_on_call=2, event=result)

    async def fake_make(role, **kw):
        return agent

    run.make_agent = fake_make
    await eng._verify(run, issue)

    assert len(agent.calls) == 2
    assert "produced no valid emission" in agent.calls[1]
    assert "verify_result" in agent.calls[1]
    assert any(e["type"] == "emission_repair" for e in events)
    assert run.by_type(VerifyResult)[0].holds is True


@pytest.mark.asyncio
async def test_verify_arrival_accepts_paraphrased_issue_via_ref():
    """A verifier that paraphrases the issue text but echoes the engine-assigned
    ref has arrived — no repair round burned on an emission that landed."""
    eng = ReviewEngine(repair_retries=1)
    run = eng.new_run()
    events: list[dict] = []
    run.on_event = events.append
    issue = IssueFound(
        dimension="security", description="sqli via unescaped id", severity="critical"
    )
    ref = _verify_ref(issue)
    result = VerifyResult(issue="the SQL injection through the id parameter", ref=ref, holds=True)
    agent = _ProseAgent(run, "verify-security", emit_on_call=1, event=result)

    async def fake_make(role, **kw):
        return agent

    run.make_agent = fake_make
    await eng._verify(run, issue)

    assert len(agent.calls) == 1  # paraphrase + correct ref = arrived
    assert not any(e["type"] in ("emission_repair", "emission_missing") for e in events)
    assert ref in agent.calls[0]  # the instruction names the token to echo


def test_verify_instruction_names_the_ref_field():
    issue = IssueFound(dimension="security", description="sqli", severity="critical")
    ref = _verify_ref(issue)
    text = _verify_instruction(issue, ref)
    assert f"ref='{ref}'" in text
    assert "claim: sqli" in text
    # Deterministic: the same issue always gets the same token.
    assert _verify_ref(issue) == ref
