# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""StructuredOutput signals: the observer keys off the *payload* type, and
``branch.operate`` emits run-lifecycle signals (RunStart/RunEnd/RunFailed)
onto the bus automatically.
"""

from __future__ import annotations

from pydantic import BaseModel

import lionagi.operations.operate.operate as op_mod
from lionagi.session.session import Session
from lionagi.session.signal import (
    RunEnd,
    RunFailed,
    RunStart,
    Signal,
    StructuredOutput,
)


class Plan(BaseModel):
    steps: int


class Finding(BaseModel):
    claim: str


async def test_observe_keys_off_payload_type():
    s = Session()
    seen = []

    @s.observe(Plan)
    def on_plan(payload, session):
        # handler receives the unwrapped payload, not the Signal envelope
        assert isinstance(payload, Plan)
        assert session is s
        seen.append(payload.steps)

    results = await s.default_branch.emit(StructuredOutput(data=Plan(steps=3)))
    assert seen == [3]
    assert results == [None]


async def test_payload_type_discriminates_handlers():
    s = Session()
    plans, findings = [], []

    s.observe(Plan, lambda p, _: plans.append(p.steps))
    s.observe(Finding, lambda f, _: findings.append(f.claim))

    await s.default_branch.emit(StructuredOutput(data=Plan(steps=1)))
    await s.default_branch.emit(StructuredOutput(data=Finding(claim="x")))

    assert plans == [1]
    assert findings == ["x"]


async def test_by_type_matches_payload():
    s = Session()
    await s.default_branch.emit(StructuredOutput(data=Plan(steps=9)))
    matched = s.observer.by_type(Plan)
    assert len(matched) == 1
    assert isinstance(matched[0], StructuredOutput)
    assert matched[0].data.steps == 9


async def test_route_condition_sees_payload():
    s = Session()
    s.route(lambda p: isinstance(p, Plan) and p.steps > 5, into="big")
    await s.default_branch.emit(StructuredOutput(data=Plan(steps=10)))
    await s.default_branch.emit(StructuredOutput(data=Plan(steps=2)))
    big = s.observer.stream("big")
    assert len(big) == 1
    assert big[0].data.steps == 10


async def test_gate_sees_payload():
    s = Session()
    fired = []
    s.observe(Plan, lambda p, _: fired.append(p.steps))
    s.gate(lambda p: getattr(p, "steps", 0) > 0)

    await s.default_branch.emit(StructuredOutput(data=Plan(steps=4)))
    await s.default_branch.emit(StructuredOutput(data=Plan(steps=0)))

    assert fired == [4]  # only the allowed one dispatched
    assert len(s.observer.by_type(Plan)) == 2  # both recorded


async def test_operate_emits_result_via_run_end(monkeypatch):
    # The final result rides on RunEnd; observe(ResultType) still fires because
    # RunEnd.data unwraps to the payload.
    s = Session()
    seen = []
    s.observe(Plan, lambda p, _: seen.append(p.steps))

    async def fake_operate(branch, **kw):
        return Plan(steps=7)

    monkeypatch.setattr(op_mod, "operate", fake_operate)
    monkeypatch.setattr(op_mod, "prepare_operate_kw", lambda branch, **kw: {})

    result = await s.default_branch.operate(instruction="plan it")
    assert isinstance(result, Plan)
    assert seen == [7]  # the final result was surfaced on the bus via RunEnd
    # exactly one RunEnd carrying the result; no duplicate StructuredOutput
    ends = s.observer.by_type(RunEnd)
    assert len(ends) == 1
    assert ends[0].data is result
    assert len(s.observer.by_type(RunStart)) == 1


async def test_operate_lifecycle_fires_for_text_result(monkeypatch):
    # A non-model result is still the run's output → emitted via RunEnd, and
    # observe(str) fires on it (the response is emitted, model or not).
    s = Session()
    seen = []
    s.observe(str, lambda p, _: seen.append(p))

    async def fake_operate(branch, **kw):
        return "just text"

    monkeypatch.setattr(op_mod, "operate", fake_operate)
    monkeypatch.setattr(op_mod, "prepare_operate_kw", lambda branch, **kw: {})

    result = await s.default_branch.operate(instruction="x")
    assert result == "just text"
    assert seen == ["just text"]
    assert len(s.observer.by_type(RunEnd)) == 1


async def test_operate_run_failed_on_exception(monkeypatch):
    s = Session()
    failures = []
    s.observe(RunFailed, lambda sig, _: failures.append(sig.data))

    async def fake_operate(branch, **kw):
        raise ValueError("boom")

    monkeypatch.setattr(op_mod, "operate", fake_operate)
    monkeypatch.setattr(op_mod, "prepare_operate_kw", lambda branch, **kw: {})

    import pytest

    with pytest.raises(ValueError, match="boom"):
        await s.default_branch.operate(instruction="x")
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert len(s.observer.by_type(RunStart)) == 1  # started before it failed
    assert len(s.observer.by_type(RunEnd)) == 0  # never completed


async def test_standalone_branch_operate_no_emit(monkeypatch):
    # A branch with no session/observer must not raise and must not emit.
    from lionagi.session.branch import Branch

    b = Branch()

    async def fake_operate(branch, **kw):
        return Plan(steps=1)

    monkeypatch.setattr(op_mod, "operate", fake_operate)
    monkeypatch.setattr(op_mod, "prepare_operate_kw", lambda branch, **kw: {})

    assert b._observer is None
    result = await b.operate(instruction="x")
    assert isinstance(result, Plan)  # no observer → emit skipped, no error


async def test_observe_lifecycle_by_envelope_type():
    # Lifecycle signals are observed by their own type; RunStart has no payload.
    s = Session()
    starts = []
    s.observe(RunStart, lambda sig, _: starts.append(sig))
    await s.default_branch.emit(RunStart())
    assert len(starts) == 1
    assert isinstance(starts[0], RunStart)


def test_signal_is_observable():
    sig = Signal(data={"k": 1})
    assert sig.id is not None
    assert sig.data == {"k": 1}
