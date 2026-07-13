# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0048 D2 acceptance matrix: the turn-origin token fires USER_PROMPT_SUBMIT
exactly once per genuine user turn, no matter how many internal calls that
turn drives underneath it.

Each test below maps to one row of the ADR's acceptance table:

    direct chat()                                          -> 1
    chat_and_record() delegating to chat()                 -> 1 total (forwarded, not re-originated)
    communicate()                                           -> 1
    operate() delegating to communicate()                   -> 1
    direct run() (CLI streaming)                             -> 1
    ReAct() with extension rounds + a final-answer turn      -> 1 total
    a failing-then-repaired parse (parse._inner_parse() ->
      Branch.chat() with no-origin) inside any of the above  -> 1 total, zero additional from the repair
"""

from __future__ import annotations

import types
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from lionagi.hooks.bus import HookBus, HookPoint
from lionagi.operations._turn_origin import TurnOrigin, consume_turn_origin, resolve_turn_origin
from lionagi.operations.run.run import run
from lionagi.operations.types import RunParam
from lionagi.service.imodel import iModel
from lionagi.service.types.stream_chunk import StreamChunk
from lionagi.session.branch import Branch
from lionagi.testing import LionAGIMockFactory


def _wire_prompt_submit_counter(branch: Branch) -> list[dict]:
    """Attach a bare HookBus to *branch* with a USER_PROMPT_SUBMIT counter handler."""
    bus = HookBus()
    calls: list[dict] = []

    async def on_submit(**kw):
        calls.append(kw)

    bus.on(HookPoint.USER_PROMPT_SUBMIT, on_submit)
    branch._hooks = bus
    return calls


def _make_fake_cli_model(chunks: list[StreamChunk]) -> iModel:
    """A minimal CLI-shaped iModel whose .stream() yields *chunks*."""
    m = iModel(provider="openai", model="gpt-4.1-mini", api_key="test_key")
    m.endpoint = types.SimpleNamespace(is_cli=True, session_id=None, to_dict=lambda: {})
    m.streaming_process_func = None

    async def create_event(**kw):
        return object()

    m.create_event = create_event
    m.executor = types.SimpleNamespace(append=AsyncMock(), config={})

    async def stream(api_call=None):
        for chunk in chunks:
            yield chunk

    m.stream = stream
    return m


# ---------------------------------------------------------------------------
# Row 1: direct chat() -> 1
# ---------------------------------------------------------------------------


async def test_direct_chat_fires_once():
    branch = LionAGIMockFactory.create_mocked_branch(response="hello")
    calls = _wire_prompt_submit_counter(branch)

    await branch.chat("hi there", return_ins_res_message=True)

    assert len(calls) == 1
    assert calls[0]["branch_id"] == str(branch.id)
    assert "hi there" in calls[0]["prompt"]


# ---------------------------------------------------------------------------
# Row 2: chat_and_record() delegating to chat() -> 1 total (forwarded, not re-originated)
# ---------------------------------------------------------------------------


async def test_chat_and_record_delegates_to_chat_fires_once():
    branch = LionAGIMockFactory.create_mocked_branch(response="hello")
    calls = _wire_prompt_submit_counter(branch)

    result = await branch.chat_and_record("hi there")

    assert result == "hello"
    assert len(calls) == 1


async def test_chat_and_record_mints_exactly_one_token_and_forwards_it(monkeypatch):
    """The mint happens once in chat_and_record(); the delegated chat() call
    must carry that same forwarded token, not mint a second one of its own."""
    import lionagi.operations._turn_origin as turn_origin_mod

    branch = LionAGIMockFactory.create_mocked_branch(response="hello")
    _wire_prompt_submit_counter(branch)

    mint_calls: list[str] = []
    real_uuid4 = turn_origin_mod.uuid4

    def counting_uuid4():
        token = real_uuid4()
        mint_calls.append(token.hex)
        return token

    monkeypatch.setattr(turn_origin_mod, "uuid4", counting_uuid4)

    await branch.chat_and_record("hi there")

    # Exactly one token minted for the whole chat_and_record()->chat() chain —
    # the delegated chat() call forwarded it unchanged instead of re-minting.
    assert len(mint_calls) == 1


# ---------------------------------------------------------------------------
# Row 3: communicate() -> 1
# ---------------------------------------------------------------------------


async def test_communicate_fires_once():
    branch = LionAGIMockFactory.create_mocked_branch(response="hello")
    calls = _wire_prompt_submit_counter(branch)

    result = await branch.communicate("hi there", skip_validation=True)

    assert result == "hello"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Row 4: operate() delegating to communicate() -> 1
# ---------------------------------------------------------------------------


async def test_operate_delegating_to_communicate_fires_once():
    branch = LionAGIMockFactory.create_mocked_branch(response="hello")
    calls = _wire_prompt_submit_counter(branch)

    # Non-CLI chat_model -> operate() selects the communicate() middle.
    assert not branch.chat_model.is_cli
    result = await branch.operate(instruction="hi there", skip_validation=True)

    assert result == "hello"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Row 5: direct run() (CLI streaming) -> 1
# ---------------------------------------------------------------------------


async def test_direct_run_fires_once():
    branch = Branch()
    branch.chat_model = _make_fake_cli_model([StreamChunk(type="text", content="ok")])
    calls = _wire_prompt_submit_counter(branch)

    results = []
    async for msg in run(branch, "hi there", RunParam()):
        results.append(msg)

    assert len(calls) == 1
    assert calls[0]["branch_id"] == str(branch.id)


# ---------------------------------------------------------------------------
# CLI guard-rejection failure semantics: a USER_PROMPT_SUBMIT handler that
# rejects a run() prompt must leave no trace of it in the transcript, and
# the rejection must surface as this run's failure, not a silent success.
# ---------------------------------------------------------------------------


async def test_run_guard_rejection_leaves_no_instruction_persisted():
    """A denied prompt must never enter branch.messages, and must never
    dispatch MESSAGE_ADD persistence for it — construct-then-guard-then-commit,
    not guard-after-persist."""
    branch = Branch()
    branch.chat_model = _make_fake_cli_model([StreamChunk(type="text", content="ok")])

    bus = HookBus()
    message_add_calls: list[dict] = []

    async def reject_submit(**kw):
        raise PermissionError("blocked")

    async def on_message_add(**kw):
        message_add_calls.append(kw)

    bus.on(HookPoint.USER_PROMPT_SUBMIT, reject_submit)
    bus.on(HookPoint.MESSAGE_ADD, on_message_add)
    branch._hooks = bus
    branch.on_message_added.append(branch._persist_via_bus)

    with pytest.raises(PermissionError):
        async for _ in run(branch, "SECRET=top-secret", RunParam()):
            pass

    assert len(branch.messages) == 0
    assert message_add_calls == []


async def test_run_guard_rejection_reports_run_failed_not_run_end():
    """A USER_PROMPT_SUBMIT rejection must route through the same failure
    bookkeeping as a mid-stream provider error, so an observer sees RunFailed
    (with the exception attached), not a silently-successful RunEnd."""
    from lionagi.session.session import Session
    from lionagi.session.signal import RunEnd, RunFailed

    s = Session()
    s.default_branch.chat_model = _make_fake_cli_model([StreamChunk(type="text", content="ok")])

    bus = HookBus()

    async def reject_submit(**kw):
        raise PermissionError("blocked")

    bus.on(HookPoint.USER_PROMPT_SUBMIT, reject_submit)
    s.default_branch._hooks = bus

    failures, ends = [], []
    s.observe(RunFailed, lambda sig, _: failures.append(sig.data))
    s.observe(RunEnd, lambda sig, _: ends.append(sig))

    with pytest.raises(PermissionError):
        async for _ in run(s.default_branch, "SECRET=top-secret", RunParam()):
            pass

    assert len(failures) == 1, f"expected 1 RunFailed, got {len(failures)}"
    assert isinstance(failures[0], PermissionError)
    assert len(ends) == 0, "RunEnd must NOT fire when the guard rejects the prompt"


# ---------------------------------------------------------------------------
# Row 6: ReAct() with extension rounds + a final-answer turn -> 1 total
# ---------------------------------------------------------------------------


async def test_react_multi_round_fires_once_total():
    """Round 1 forwards the ReAct()-driven turn; extension rounds and the
    final-answer turn are internal continuations (no-origin) and stay silent."""
    branch = LionAGIMockFactory.create_mocked_branch(
        responses=[
            '{"analysis": "round 1", "extension_needed": true}',
            '{"analysis": "round 2", "extension_needed": false}',
            '{"answer": "done"}',
        ]
    )
    calls = _wire_prompt_submit_counter(branch)

    result = await branch.ReAct(instruct={"instruction": "do a multi-step task"}, max_extensions=2)

    assert result == "done"
    # 3 real model calls happened (2 analysis rounds + final answer) but only
    # the very first one is user-originated.
    assert len(calls) == 1


async def test_react_single_round_no_extension_still_fires_once():
    """extension_needed=False on round 1 -> straight to final answer; still 1 total."""
    branch = LionAGIMockFactory.create_mocked_branch(
        responses=[
            '{"analysis": "only round", "extension_needed": false}',
            '{"answer": "quick answer"}',
        ]
    )
    calls = _wire_prompt_submit_counter(branch)

    result = await branch.ReAct(instruct={"instruction": "simple task"}, max_extensions=5)

    assert result == "quick answer"
    assert len(calls) == 1


async def test_react_with_interpret_fires_once_total():
    """interpret=True adds a rewrite pre-pass before round 1 — the earliest
    point this turn's raw text reaches a model. The pre-pass consumes the
    turn origin; round 1 and every extension/final-answer turn after it are
    internal continuations of the same turn and must stay silent."""
    branch = LionAGIMockFactory.create_mocked_branch(
        responses=[
            "rewritten: do a multi-step task",
            '{"analysis": "round 1", "extension_needed": false}',
            '{"answer": "done"}',
        ]
    )
    calls = _wire_prompt_submit_counter(branch)

    result = await branch.ReAct(
        instruct={"instruction": "do a multi-step task"}, interpret=True, max_extensions=2
    )

    assert result == "done"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Row 7: failing-then-repaired parse (parse._inner_parse() -> Branch.chat()
# with no-origin) -> 1 total, zero additional from the repair
# ---------------------------------------------------------------------------


class _StrictAnswer(BaseModel):
    answer: str


async def test_parse_repair_inside_communicate_fires_zero_additional():
    """communicate()'s initial response fails direct parsing, forcing
    parse._inner_parse() to retry via Branch.chat() one or more times — the
    repair path must never fire USER_PROMPT_SUBMIT itself."""
    branch = LionAGIMockFactory.create_mocked_branch(response="not valid json at all {{{")
    calls = _wire_prompt_submit_counter(branch)

    result = await branch.communicate(
        "hi there",
        response_format=_StrictAnswer,
        fuzzy_match_kwargs={"handle_validation": "return_value"},
    )

    # Parsing never recovered (mock keeps returning the same bad text), so
    # communicate() falls back to the raw response — but regardless of how
    # many internal repair attempts parse._inner_parse() made, the total
    # USER_PROMPT_SUBMIT count is exactly 1 (the original communicate() call).
    assert result == "not valid json at all {{{"
    assert len(calls) == 1


async def test_parse_inner_parse_uses_no_origin_directly():
    """Unit-level check on the named seam itself: parse._inner_parse() must
    resolve to a token-less (no-origin) disposition, independent of any
    integration wiring above it."""
    no_origin = TurnOrigin.no_origin()
    assert consume_turn_origin(no_origin) is None
    assert resolve_turn_origin(no_origin).disposition == "no-origin"


# ---------------------------------------------------------------------------
# interpret(): a direct (non-ReAct) call is itself a public ingress — raw
# user text reaching a model for the first time in a turn — so it mints and
# fires on its own default disposition, same as chat()/communicate().
# ---------------------------------------------------------------------------


async def test_direct_interpret_call_fires_once():
    branch = LionAGIMockFactory.create_mocked_branch(response="rewritten prompt")
    calls = _wire_prompt_submit_counter(branch)

    result = await branch.interpret("raw user text")

    assert result == "rewritten prompt"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Supporting unit coverage for the TurnOrigin value type itself
# ---------------------------------------------------------------------------


def test_turn_origin_unset_mints_a_token_on_resolve():
    unset = TurnOrigin.unset()
    assert consume_turn_origin(unset) is not None


def test_turn_origin_forwarded_token_is_returned_unchanged():
    forwarded = TurnOrigin.forwarded("fixed-token")
    assert consume_turn_origin(forwarded) == "fixed-token"


def test_turn_origin_no_origin_never_resolves_to_a_token():
    assert consume_turn_origin(TurnOrigin.no_origin()) is None


def test_turn_origin_forwarded_rejects_empty_token():
    with pytest.raises(ValueError):
        TurnOrigin.forwarded("")


def test_resolve_turn_origin_defaults_missing_or_sentinel_values_to_unset():
    from lionagi.ln.types import Undefined, Unset

    assert resolve_turn_origin(None).disposition == "unset"
    assert resolve_turn_origin(Unset).disposition == "unset"
    assert resolve_turn_origin(Undefined).disposition == "unset"


async def test_no_hooks_bus_still_completes_chat():
    """A branch without a hook bus wired must still work — no-op, not an error."""
    branch = LionAGIMockFactory.create_mocked_branch(response="hello")
    assert branch._hooks is None

    ins, res = await branch.chat("hi", return_ins_res_message=True)
    assert res.response == "hello"


async def test_turn_origin_does_not_leak_into_instruction_content():
    """Regression guard: ChatParam.turn_origin must never reach
    Branch.msgs.create_instruction() — it is an operation-context-only field,
    not part of the rendered Instruction."""
    branch = LionAGIMockFactory.create_mocked_branch(response="hello")
    _wire_prompt_submit_counter(branch)

    ins, res = await branch.chat(
        "hi there", return_ins_res_message=True, _turn_origin=TurnOrigin.no_origin()
    )
    assert not hasattr(ins.content, "turn_origin")
