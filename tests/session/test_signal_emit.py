# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""StructuredOutput signals: the observer keys off the *payload* type, and
``branch.operate`` raises its structured result onto the bus automatically.
"""

from __future__ import annotations

from pydantic import BaseModel

import lionagi.operations.operate.operate as op_mod
from lionagi.session.session import Session
from lionagi.session.signal import Signal, StructuredOutput


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


async def test_operate_emits_structured_output(monkeypatch):
    s = Session()
    seen = []
    s.observe(Plan, lambda p, _: seen.append(p.steps))

    async def fake_operate(branch, **kw):
        return Plan(steps=7)

    monkeypatch.setattr(op_mod, "operate", fake_operate)
    monkeypatch.setattr(op_mod, "prepare_operate_kw", lambda branch, **kw: {})

    result = await s.default_branch.operate(instruction="plan it")
    assert isinstance(result, Plan)
    assert seen == [7]  # the structured result was emitted onto the bus


async def test_operate_non_model_result_not_emitted(monkeypatch):
    s = Session()
    seen = []
    s.observe(str, lambda p, _: seen.append(p))

    async def fake_operate(branch, **kw):
        return "just text"

    monkeypatch.setattr(op_mod, "operate", fake_operate)
    monkeypatch.setattr(op_mod, "prepare_operate_kw", lambda branch, **kw: {})

    result = await s.default_branch.operate(instruction="x")
    assert result == "just text"
    assert seen == []  # plain text is not a structured-output emission


async def test_standalone_branch_operate_no_emit(monkeypatch):
    # A branch with no session/observer must not raise when operate returns a model.
    from lionagi.session.branch import Branch

    b = Branch()

    async def fake_operate(branch, **kw):
        return Plan(steps=1)

    monkeypatch.setattr(op_mod, "operate", fake_operate)
    monkeypatch.setattr(op_mod, "prepare_operate_kw", lambda branch, **kw: {})

    assert b._observer is None
    result = await b.operate(instruction="x")
    assert isinstance(result, Plan)  # no observer → emit skipped, no error


def test_signal_is_observable():
    sig = Signal(data={"k": 1})
    assert sig.id is not None
    assert sig.data == {"k": 1}
