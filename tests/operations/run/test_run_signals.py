# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Per-message stream emission: the run loop raises each streamed message onto
the session bus as a ``MessageAdded`` signal — every message, observable by
payload type (``observe(ActionRequest)``) — plus a StructuredOutput bundle when
an assistant message carries capability emissions.

A capability is a named typed ``Spec``; the agent's grant is an ``Operable``.
The bundle preserves field names, so observers can react by type (``Finding``)
or by named field+value (``flower.q == "rose"``). A response may carry two or
more capabilities → one bundle satisfying several observers.
"""

from __future__ import annotations

from pydantic import BaseModel

from lionagi.ln.types import Operable, Spec
from lionagi.operations._observe import attempt_extract as _attempt_extract
from lionagi.operations._observe import emit_message as _emit_message_signal
from lionagi.protocols.messages import (
    ActionRequest,
    ActionResponse,
    AssistantResponse,
)
from lionagi.protocols.messages.assistant_response import AssistantResponseContent
from lionagi.session.session import Session
from lionagi.session.signal import StructuredOutput


class Finding(BaseModel):
    claim: str
    confidence: float = 0.5


class Question(BaseModel):
    text: str


def _grant() -> Operable:
    return Operable(
        (Spec(Finding, name="finding"), Spec(Question, name="question")),
        name="AgentCapabilities",
    )


def _assistant(text: str) -> AssistantResponse:
    return AssistantResponse(
        content=AssistantResponseContent(assistant_response=text),
        sender=None,
        recipient="user",
    )


# -- _attempt_extract → bundle ----------------------------------------------


def test_extract_single_capability():
    bundles, violations, _rejects = _attempt_extract(
        '{"finding": {"claim": "x", "confidence": 0.9}}', _grant()
    )
    assert len(bundles) == 1 and not violations
    assert isinstance(bundles[0].finding, Finding)
    assert bundles[0].finding.claim == "x" and bundles[0].finding.confidence == 0.9


def test_extract_multiple_keys_one_block():
    text = '```json\n{"finding": {"claim": "y"}, "question": {"text": "why?"}}\n```'
    bundles, _, _ = _attempt_extract(text, _grant())
    assert len(bundles) == 1
    assert bundles[0].finding.claim == "y"
    assert bundles[0].question.text == "why?"


def test_extract_multiple_blocks_one_message():
    # the case the live demo exposed: several ```json blocks in one response
    text = (
        "Reading along...\n"
        '```json\n{"finding": {"claim": "first"}}\n```\n'
        "and more...\n"
        '```json\n{"finding": {"claim": "second", "confidence": 0.9}}\n```\n'
        "done."
    )
    bundles, _, _ = _attempt_extract(text, _grant())
    assert len(bundles) == 2
    assert [b.finding.claim for b in bundles] == ["first", "second"]


def test_extract_prose_returns_empty():
    assert _attempt_extract("just thinking out loud", _grant()) == ([], [], [])


def test_extract_non_capability_json_ignored():
    # valid JSON, keys disjoint from the grant → ordinary output, not a capability
    assert _attempt_extract('{"unrelated": 1}', _grant()) == ([], [], [])


def test_extract_illegal_emission_becomes_violation(caplog):
    from lionagi.session.capabilities import CapabilityViolation

    # 'secret' is outside the grant → not honored, recorded as a violation
    bundles, violations, _rejects = _attempt_extract(
        '{"finding": {"claim": "z"}, "secret": {"x": 1}}', _grant()
    )
    assert bundles == []  # the mixed block is not honored
    assert len(violations) == 1
    v = violations[0]
    assert isinstance(v, CapabilityViolation)
    assert v.offending == ["secret"]
    assert "finding" in v.allowed and "question" in v.allowed
    assert any("Illegal capability emission" in r.message for r in caplog.records)


async def test_violation_emitted_as_bus_signal():
    from lionagi.session.capabilities import CapabilityViolation

    s = Session()
    caught = []
    s.observe(CapabilityViolation, lambda v, _: caught.append(v.offending))

    branch = s.default_branch
    branch.capabilities = _grant()
    # a mixed block reaches past the grant (finding granted, secret not)
    await _emit_message_signal(
        branch, _assistant('{"finding": {"claim": "z"}, "secret": {"x": 1}}')
    )

    assert caught == [["secret"]]  # governance observer fired on the over-grant


def test_extract_invalid_in_grant_block_becomes_reject():
    from lionagi.session.capabilities import EmissionRejected

    # 'finding' is granted but the block fails schema validation (bad type)
    bundles, violations, rejects = _attempt_extract(
        '{"finding": {"claim": "z", "confidence": "very high"}}', _grant()
    )
    assert bundles == [] and violations == []
    assert len(rejects) == 1
    r = rejects[0]
    assert isinstance(r, EmissionRejected)
    assert "confidence" in r.error
    assert r.block == {"finding": {"claim": "z", "confidence": "very high"}}


async def test_reject_emitted_as_bus_signal_with_branch_name():
    from lionagi.session.capabilities import EmissionRejected

    s = Session()
    caught = []
    s.observe(EmissionRejected, lambda r, _: caught.append(r.branch_name))

    branch = s.default_branch
    branch.name = "weak-extractor"
    branch.capabilities = _grant()
    await _emit_message_signal(
        branch, _assistant('{"finding": {"claim": "z", "confidence": "very high"}}')
    )

    assert caught == ["weak-extractor"]  # repair observers can attribute it


# -- assistant capability bundle → bus, observed by TYPE --------------------


async def test_bundle_observed_by_type():
    s = Session()
    findings, questions = [], []
    s.observe(Finding, lambda f, _: findings.append(f.claim))
    s.observe(Question, lambda q, _: questions.append(q.text))

    branch = s.default_branch
    branch.capabilities = _grant()

    await _emit_message_signal(
        branch,
        _assistant('{"finding": {"claim": "found"}, "question": {"text": "q?"}}'),
    )
    # one bundle emit, two observers fire — each handed its typed field
    assert findings == ["found"]
    assert questions == ["q?"]
    # exactly one signal recorded, matched by both type filters
    assert len(s.observer.by_type(Finding)) == 1
    assert len(s.observer.by_type(Question)) == 1


async def test_assistant_no_grant_no_extraction():
    s = Session()
    seen = []
    s.observe(Finding, lambda f, _: seen.append(f.claim))
    # capabilities unset (default None) → extraction dormant
    await _emit_message_signal(s.default_branch, _assistant('{"finding": {"claim": "x"}}'))
    assert seen == []


# -- observed by named FIELD + VALUE (SpecFilter) ---------------------------


async def test_bundle_observed_by_spec_filter():
    flower = Spec(str, name="flower_name")
    grant = Operable((flower,), name="FlowerCaps")

    s = Session()
    roses, others = [], []
    # react only when the agent names a rose
    s.observe(flower.q == "rose", lambda bundle, _: roses.append(bundle.flower_name))
    s.observe(flower.q != "rose", lambda bundle, _: others.append(bundle.flower_name))

    branch = s.default_branch
    branch.capabilities = grant

    await _emit_message_signal(branch, _assistant('{"flower_name": "rose"}'))
    await _emit_message_signal(branch, _assistant('{"flower_name": "tulip"}'))

    assert roses == ["rose"]
    assert others == ["tulip"]


# -- tool-use / tool-result signals -----------------------------------------


async def test_action_request_signal():
    s = Session()
    calls = []
    s.observe(ActionRequest, lambda req, _: calls.append(req.function))

    req = s.default_branch.msgs.create_action_request(
        function="search", arguments={"q": "lion"}, sender=None, recipient="user"
    )
    await _emit_message_signal(s.default_branch, req)

    # observe(ActionRequest) fires exactly once — off the MessageAdded envelope's
    # unwrapped payload. There is no separate ActionRequestSignal carrying the
    # same message, which would double-fire this data-type observer.
    assert calls == ["search"]
    matched = s.observer.by_type(ActionRequest)
    assert len(matched) == 1 and matched[0].data.arguments == {"q": "lion"}


async def test_action_response_signal():
    s = Session()
    outputs = []
    s.observe(ActionResponse, lambda res, _: outputs.append(res.output))

    branch = s.default_branch
    req = branch.msgs.create_action_request(
        function="search", arguments={"q": "x"}, sender=None, recipient="user"
    )
    res = branch.msgs.create_action_response(
        action_request=req, action_output={"hits": 3}, sender="user", recipient=None
    )
    await _emit_message_signal(branch, res)

    # Fires once via the MessageAdded envelope (no separate ActionResponseSignal).
    assert outputs == [{"hits": 3}]
    matched = s.observer.by_type(ActionResponse)
    assert len(matched) == 1 and matched[0].data.output == {"hits": 3}


async def test_tool_stats_aggregation():
    # the motivating use case: count tool calls by function from the bus
    s = Session()
    branch = s.default_branch
    for fn in ("search", "search", "read"):
        req = branch.msgs.create_action_request(
            function=fn, arguments={}, sender=None, recipient="user"
        )
        await _emit_message_signal(branch, req)

    reqs = s.observer.by_type(ActionRequest)
    counts: dict[str, int] = {}
    for ev in reqs:
        counts[ev.data.function] = counts.get(ev.data.function, 0) + 1
    assert counts == {"search": 2, "read": 1}


async def test_standalone_branch_emit_noop():
    from lionagi.session.branch import Branch

    b = Branch()
    b.capabilities = _grant()
    await _emit_message_signal(b, _assistant('{"finding": {"claim": "x"}}'))
    req = b.msgs.create_action_request(function="f", arguments={}, sender=None, recipient="user")
    await _emit_message_signal(b, req)
    assert b._observer is None  # no observer was lazily created
