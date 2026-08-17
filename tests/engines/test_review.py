# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ReviewEngine logic — dimensional fan-out, adversarial verify, verdict. No LLM."""

from __future__ import annotations

import pytest

from lionagi.engines.review import (
    DimensionClean,
    IssueFound,
    ReviewEngine,
    ReviewVerdict,
    VerifyResult,
    _clean_ref,
    _verdict_instruction,
    _verify_clean_instruction,
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


# -- clean-verdict audit ------------------------------------------------------
# A clean or minor-only review spawns no issue verifiers, so it used to reach
# the verdict with zero VerifyResult and ship an APPROVE backed by nothing
# executed. Whether a consumer refuses that as evidence-empty is the consumer's
# own code and is not observable from here, so it is not what these tests pin.
# The engine now audits its own clean verdict: one adversarial verifier that
# must execute a real check and emit a VerifyResult carrying the run's clean
# ref, so a clean verdict ships positive evidence instead of absence.


class _RoutedEmitter:
    """operate() emits the event registered for this agent's name, once."""

    def __init__(self, run, name: str, event):
        self.name = name
        self._run = run
        self._event = event
        self.calls: list[str] = []

    async def operate(self, *, instruction: str):
        self.calls.append(instruction)
        if self._event is not None and len(self.calls) == 1:
            await self._run.emit(self._event)
        return None


def _routing_make(run, events_by_name: dict, made: list, verdict_capture: dict | None = None):
    async def fake_make(role, *, name=None, **kw):
        made.append(name or role)
        if name == "verdict":

            class _Synth:
                name = "verdict"

                async def operate(self, *, instruction: str):
                    if verdict_capture is not None:
                        verdict_capture["instruction"] = instruction
                    return "APPROVE"

            return _Synth()
        return _RoutedEmitter(run, name or role, events_by_name.get(name))

    return fake_make


@pytest.mark.asyncio
async def test_clean_review_emits_a_clean_audit_verify_result():
    dims = ("correctness", "security")
    eng = ReviewEngine(dimensions=dims)
    run = eng.new_run()
    ref = _clean_ref(dims)
    made: list = []
    run.make_agent = _routing_make(
        run,
        {
            "review-correctness": DimensionClean(dimension="correctness", rationale="checked"),
            "review-security": DimensionClean(dimension="security", rationale="checked"),
            "verify-clean": VerifyResult(
                issue="CLEAN: review affirmed no blocking issues",
                ref=ref,
                holds=True,
                rationale="ran the suite named in the artifact; 12 passed",
            ),
        },
        made,
    )
    await eng._run(run, "ARTIFACT")
    assert "verify-clean" in made
    results = run.by_type(VerifyResult)
    assert len(results) == 1 and results[0].ref == ref


@pytest.mark.asyncio
async def test_minor_only_review_also_gets_the_clean_audit():
    # Minor issues spawn no issue verifier, so without the audit this run
    # would carry zero VerifyResult exactly like a clean one.
    dims = ("correctness",)
    eng = ReviewEngine(dimensions=dims)
    run = eng.new_run()
    ref = _clean_ref(dims)
    made: list = []
    run.make_agent = _routing_make(
        run,
        {
            "review-correctness": IssueFound(
                dimension="correctness", description="nit", severity="minor"
            ),
            "verify-clean": VerifyResult(issue="CLEAN: minor-only", ref=ref, holds=True),
        },
        made,
    )
    await eng._run(run, "ARTIFACT")
    assert "verify-clean" in made
    assert any(v.ref == ref for v in run.by_type(VerifyResult))


@pytest.mark.asyncio
async def test_issue_path_spawns_no_clean_audit():
    # A critical issue produces a real issue verification — the clean audit
    # must not fire on top of it.
    dims = ("security",)
    eng = ReviewEngine(dimensions=dims)
    run = eng.new_run()
    issue = IssueFound(dimension="security", description="sqli", severity="critical")
    made: list = []
    run.make_agent = _routing_make(
        run,
        {
            "review-security": issue,
            "verify-security": VerifyResult(issue="sqli", ref=_verify_ref(issue), holds=True),
        },
        made,
    )
    await eng._run(run, "ARTIFACT")
    assert "verify-security" in made
    assert "verify-clean" not in made


@pytest.mark.asyncio
async def test_verify_clean_off_switch_restores_old_behavior():
    dims = ("correctness",)
    eng = ReviewEngine(dimensions=dims, verify_clean=False)
    run = eng.new_run()
    made: list = []
    run.make_agent = _routing_make(
        run,
        {"review-correctness": DimensionClean(dimension="correctness")},
        made,
    )
    await eng._run(run, "ARTIFACT")
    assert "verify-clean" not in made
    assert run.by_type(VerifyResult) == []


def test_verify_clean_instruction_mandates_an_executed_check():
    dims = ("correctness", "security")
    ref = _clean_ref(dims)
    clean = [DimensionClean(dimension="correctness", rationale="looked sound")]
    text = _verify_clean_instruction("ARTIFACT-BODY", clean, ref)
    assert f"ref='{ref}'" in text
    assert "MUST execute" in text
    assert "naming no executed check is invalid" in text
    assert "ARTIFACT-BODY" in text  # the verifier gets the artifact to check against
    assert "correctness: looked sound" in text
    # Deterministic per dimension set, distinct across sets.
    assert _clean_ref(dims) == ref
    assert _clean_ref(("other",)) != ref


def test_verdict_instruction_inverts_polarity_for_refuted_clean_audit():
    # The generic guidance says "weigh refuted issues down" — read onto a
    # clean audit, that polarity is backwards: holds=false there REFUTES the
    # clean verdict. The clean-audit section must carry its own AGAINST-
    # approval guidance, and it must appear only when a clean audit exists.
    dims = ("correctness",)
    refuted = VerifyResult(
        issue="CLEAN: review affirmed no blocking issues",
        ref=_clean_ref(dims),
        holds=False,
        rationale="ran the suite; 2 tests fail",
    )
    with_audit = _verdict_instruction("ART", dims, [], [refuted], [])
    assert "Clean-verdict audit" in with_audit
    assert "weigh that AGAINST approval" in with_audit
    assert "ran the suite; 2 tests fail" in with_audit

    # An ordinary issue verification must NOT be routed into the audit
    # section or given the inverted guidance.
    issue = IssueFound(dimension="security", description="sqli", severity="critical")
    ordinary = VerifyResult(issue="sqli", ref=_verify_ref(issue), holds=False)
    without_audit = _verdict_instruction("ART", dims, [issue], [ordinary], [])
    assert "Clean-verdict audit" not in without_audit
    assert "weigh that AGAINST approval" not in without_audit


# -- withholding a pass that rests on nothing executed ------------------------


class _ApprovingSynth:
    """Synthesis that emits an APPROVE, the way a real one does on a clean run."""

    name = "verdict"

    def __init__(self, run):
        self._run = run

    async def operate(self, *, instruction):
        await self._run.emit(ReviewVerdict(verdict="APPROVE", rationale="nothing found"))
        return "APPROVE: nothing found"


def _synth_factory(run):
    async def fake_make(role, **kw):
        return _ApprovingSynth(run)

    return fake_make


@pytest.mark.asyncio
async def test_an_approval_with_no_executed_evidence_is_withheld():
    """Isolating a dead verifier records it and carries on, so a run whose
    verifiers all died reaches synthesis with no VerifyResult. Synthesis is
    handed counts and has no branch that can refuse, so the absence of evidence
    arrives looking like an absence of problems."""
    eng = ReviewEngine(verify_clean=True)
    run = eng.new_run()
    run.make_agent = _synth_factory(run)

    out = await eng._verdict(run, "ART", ("security",))

    assert out.startswith("INCONCLUSIVE"), out
    emitted = run.by_type(ReviewVerdict)[0]
    assert emitted.verdict == "INCONCLUSIVE"
    # The withheld decision is kept rather than erased: a reader has to be able
    # to see what was refused, or the refusal is unauditable.
    assert "APPROVE" in emitted.rationale


@pytest.mark.asyncio
async def test_an_approval_backed_by_a_verification_is_left_alone():
    """The control that keeps the guard from reading as 'never approve'."""
    eng = ReviewEngine(verify_clean=True)
    run = eng.new_run()
    await run.emit(VerifyResult(issue="sqli", holds=False, rationale="boundary test refutes"))
    run.make_agent = _synth_factory(run)

    out = await eng._verdict(run, "ART", ("security",))

    assert out == "APPROVE: nothing found"
    assert run.by_type(ReviewVerdict)[0].verdict == "APPROVE"


@pytest.mark.asyncio
async def test_a_review_that_was_never_asked_for_evidence_still_approves():
    """With verify_clean off no verifier is spawned at all, so having no
    VerifyResult is the ordinary case rather than the failed one. Withholding
    here would refuse every clean review the engine was configured to do."""
    eng = ReviewEngine(verify_clean=False)
    run = eng.new_run()
    run.make_agent = _synth_factory(run)

    out = await eng._verdict(run, "ART", ("security",))

    assert out == "APPROVE: nothing found"
    assert run.by_type(ReviewVerdict)[0].verdict == "APPROVE"
