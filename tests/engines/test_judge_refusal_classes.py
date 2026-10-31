# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Request and policy refusals cannot be read as judge approval."""

from __future__ import annotations

import asyncio
import sys
from collections import deque
from contextlib import aclosing

import pytest

from lionagi._errors import (
    CLIInputRefusedError,
    ConfigurationError,
    ContainmentRefusedError,
    InputTokenCapError,
)
from lionagi.agent.factory import _AgentSettingError
from lionagi.ln.concurrency._compat import ExceptionGroup
from lionagi.providers._provider_errors import NestedAgentRefusedError
from lionhub.orchestration.engines.engine import _JUDGE_REFUSALS, Engine, EngineBudgetError

pytestmark = pytest.mark.asyncio

_REFUSALS = [CLIInputRefusedError, InputTokenCapError, NestedAgentRefusedError, _AgentSettingError]


async def test_request_and_policy_refusal_test_cases_cover_engine_types():
    assert tuple(_REFUSALS) == _JUDGE_REFUSALS


async def _judge_raising(error, *, contained=False):
    engine = Engine(judge_model="scripted/judge", contain_account_integrations=contained)
    run = engine.new_run()
    events = []
    run.notify = lambda kind, **kwargs: events.append((kind, kwargs))

    async def make_agent(*args, **kwargs):
        raise error

    run.make_agent = make_agent
    return await engine.judge(run, "item", "subject"), events


def _wrap(refusal, shape):
    if shape == "direct":
        return refusal
    if shape == "group":
        return ExceptionGroup("group", [refusal])
    if shape == "nested-group":
        return ExceptionGroup("outer", [ExceptionGroup("inner", [refusal])])
    if shape == "mixed-group":
        return ExceptionGroup("mixed", [RuntimeError("ordinary"), refusal])
    outer = RuntimeError("wrapper")
    if shape == "cause":
        outer.__cause__ = refusal
    else:
        outer.__context__ = refusal
    return outer


@pytest.mark.parametrize("refusal_type", _REFUSALS, ids=lambda cls: cls.__name__)
@pytest.mark.parametrize("contained", [False, True], ids=["uncontained", "contained"])
@pytest.mark.parametrize(
    "shape", ["direct", "cause", "context", "group", "nested-group", "mixed-group"]
)
async def test_request_and_policy_refusals_are_not_admitted(refusal_type, contained, shape):
    refusal = refusal_type("refused")
    with pytest.raises(refusal_type) as caught:
        await _judge_raising(_wrap(refusal, shape), contained=contained)
    assert caught.value is refusal


@pytest.mark.parametrize(
    ("first_type", "second_type"),
    [
        (CLIInputRefusedError, InputTokenCapError),
        (InputTokenCapError, CLIInputRefusedError),
    ],
)
async def test_first_request_or_policy_refusal_in_walk_order_is_raised(first_type, second_type):
    first = first_type("first refusal")
    second = second_type("second refusal")
    outer = RuntimeError("wrapper")
    outer.__context__ = first
    outer.__cause__ = second

    with pytest.raises(first_type) as caught:
        await _judge_raising(outer)
    assert caught.value is first


@pytest.mark.parametrize("refusal_type", _REFUSALS, ids=lambda cls: cls.__name__)
@pytest.mark.parametrize("shape", ["cause", "context", "group"])
async def test_selected_refusal_does_not_expose_or_mutate_its_outer_error(refusal_type, shape):
    secret = "outer-error-secret-marker"
    refusal = refusal_type("refused")
    refusal_cause = ValueError("safe refusal cause")
    refusal.__cause__ = refusal_cause
    other_link = RuntimeError("unchanged outer link")
    if shape == "group":
        outer = ExceptionGroup(secret, [refusal])
        outer.__cause__ = other_link
    else:
        outer = RuntimeError(secret)
        if shape == "cause":
            outer.__cause__ = refusal
            outer.__context__ = other_link
        else:
            outer.__context__ = refusal
            outer.__cause__ = other_link
    original_links = (outer.__cause__, outer.__context__)

    with pytest.raises(refusal_type) as caught:
        await _judge_raising(outer)

    assert caught.value is refusal
    assert refusal.__cause__ is refusal_cause
    assert (outer.__cause__, outer.__context__) == original_links
    pending = [caught.value]
    seen = set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        assert secret not in str(error)
        pending.extend(link for link in (error.__cause__, error.__context__) if link is not None)
        if isinstance(error, ExceptionGroup):
            pending.extend(error.exceptions)


@pytest.mark.parametrize("shape", ["cause", "context", "group"])
async def test_selected_refusal_does_not_retain_outer_error_in_judge_frame(shape):
    refusal = CLIInputRefusedError("refused")
    outer = _wrap(refusal, shape)
    with pytest.raises(CLIInputRefusedError) as caught:
        await _judge_raising(outer)

    traceback = caught.value.__traceback__
    judge_frames = []
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_code.co_name == "judge" and frame.f_code.co_filename.endswith("engine.py"):
            judge_frames.append(frame)
        traceback = traceback.tb_next
    assert len(judge_frames) == 1

    def retains_outer(value):
        if value is outer:
            return True
        if isinstance(value, dict):
            return any(item is outer for item in (*value.keys(), *value.values()))
        if isinstance(value, (list, tuple, deque)):
            return any(item is outer for item in value)
        return False

    retained = [name for name, value in judge_frames[0].f_locals.items() if retains_outer(value)]
    assert retained == []


async def test_selected_refusal_does_not_retain_other_group_chain_objects_in_judge_frame():
    refusal = CLIInputRefusedError("refused")
    intermediate = RuntimeError("intermediate")
    intermediate.__cause__ = refusal
    sibling = RuntimeError("ordinary sibling")
    outer = ExceptionGroup("outer", [intermediate, sibling])

    with pytest.raises(CLIInputRefusedError) as caught:
        await _judge_raising(outer)
    assert caught.value is refusal

    traceback = caught.value.__traceback__
    judge_frames = []
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_code.co_name == "judge" and frame.f_code.co_filename.endswith("engine.py"):
            judge_frames.append(frame)
        traceback = traceback.tb_next
    assert len(judge_frames) == 1
    unwanted = (outer, intermediate, sibling)

    def retains_unwanted(value):
        if any(value is item for item in unwanted):
            return True
        if isinstance(value, dict):
            return any(
                entry is item for entry in (*value.keys(), *value.values()) for item in unwanted
            )
        if isinstance(value, (list, tuple, deque)):
            return any(entry is item for entry in value for item in unwanted)
        return False

    retained = [name for name, value in judge_frames[0].f_locals.items() if retains_unwanted(value)]
    assert retained == []


async def test_containment_refusal_keeps_its_existing_raise_context():
    refusal = ContainmentRefusedError("containment refused")
    original_cause = ValueError("cause")
    refusal.__cause__ = original_cause
    outer = RuntimeError("wrapper")
    outer.__cause__ = refusal

    with pytest.raises(ContainmentRefusedError) as caught:
        await _judge_raising(outer)
    assert caught.value is refusal
    assert refusal.__cause__ is original_cause
    assert refusal.__context__ is outer
    traceback = caught.value.__traceback__
    names = []
    while traceback is not None:
        names.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    assert "judge" in names
    assert "_judge_refusal" not in names


def _chain_with_refusal_at(position, refusal, *, with_group):
    tail = ExceptionGroup("group", [refusal]) if with_group else refusal
    for index in range(position - (2 if with_group else 1)):
        outer = RuntimeError(f"wrapper {index}")
        outer.__cause__ = tail
        tail = outer
    return tail


@pytest.mark.parametrize("refusal_type", _REFUSALS, ids=lambda cls: cls.__name__)
@pytest.mark.parametrize("with_group", [False, True], ids=["links", "group-member"])
@pytest.mark.parametrize("position", [100, 101])
async def test_refusal_walk_has_one_hundred_object_bound(refusal_type, with_group, position):
    refusal = refusal_type("refused")
    error = _chain_with_refusal_at(position, refusal, with_group=with_group)
    if position == 100:
        with pytest.raises(refusal_type) as caught:
            await _judge_raising(error)
        assert caught.value is refusal
    else:
        allowed, events = await _judge_raising(error)
        assert allowed is True
        assert [event for event, _ in events] == ["judge_error"]


async def test_inner_containment_refusal_beats_outer_cli_input_refusal():
    containment = ContainmentRefusedError("containment refusal")
    outer = CLIInputRefusedError("input refusal")
    outer.__cause__ = containment
    with pytest.raises(ContainmentRefusedError) as caught:
        await _judge_raising(outer)
    assert caught.value is containment


async def test_direct_setting_error_still_beats_its_inner_containment_refusal():
    outer = _AgentSettingError("settings refusal")
    outer.__cause__ = ContainmentRefusedError("containment refusal")
    with pytest.raises(_AgentSettingError) as caught:
        await _judge_raising(outer)
    assert caught.value is outer


async def test_selected_refusal_with_unreadable_cause_is_still_raised():
    class BrokenCause(CLIInputRefusedError):
        def __getattribute__(self, name):
            if name == "__cause__":
                raise OSError("cause cannot be read")
            return super().__getattribute__(name)

    refusal = BrokenCause("input refusal")
    with pytest.raises(BrokenCause) as caught:
        await _judge_raising(refusal)
    assert caught.value is refusal


@pytest.mark.parametrize("position", [100, 101])
async def test_repeated_group_members_consume_the_traversal_budget(position):
    refusal = CLIInputRefusedError("refused")
    repeated = RuntimeError("repeated member")
    group = ExceptionGroup("group", [repeated] * (position - 2) + [refusal])
    if position == 100:
        with pytest.raises(CLIInputRefusedError) as caught:
            await _judge_raising(group)
        assert caught.value is refusal
    else:
        allowed, events = await _judge_raising(group)
        assert allowed is True
        assert [event for event, _ in events] == ["judge_error"]


async def test_wide_group_members_are_read_lazily_within_the_shared_budget():
    reads = []
    repeated = RuntimeError("repeated member")

    class WideGroup(ExceptionGroup):
        def __getattribute__(self, name):
            if name == "exceptions":

                def members():
                    for _ in range(10_000):
                        reads.append(1)
                        if len(reads) > 100:
                            raise AssertionError("too many group members were read")
                        yield repeated

                return members()
            return super().__getattribute__(name)

    group = WideGroup("wide", [repeated])
    allowed, events = await _judge_raising(group)
    assert allowed is True
    assert [event for event, _ in events] == ["judge_error"]
    assert 0 < len(reads) <= 99


async def test_group_iteration_failure_keeps_prior_members_and_skips_the_rest():
    first = CLIInputRefusedError("first refusal")
    unread = InputTokenCapError("unread refusal")
    reads = []

    class Members:
        def __iter__(self):
            return self

        def __next__(self):
            reads.append(1)
            if len(reads) == 1:
                return first
            if len(reads) == 2:
                raise OSError("members cannot be read")
            return unread

    class PartiallyReadableGroup(ExceptionGroup):
        def __getattribute__(self, name):
            if name == "exceptions":
                return Members()
            return super().__getattribute__(name)

    group = PartiallyReadableGroup("group", [first, unread])
    with pytest.raises(CLIInputRefusedError) as caught:
        await _judge_raising(group)
    assert caught.value is first
    assert reads == [1, 1]


@pytest.mark.parametrize(
    "error", [RuntimeError("backend unavailable"), ConfigurationError("configuration")]
)
async def test_ordinary_judge_errors_remain_advisory(error):
    allowed, events = await _judge_raising(error)
    assert allowed is True
    assert [event for event, _ in events] == ["judge_error"]


async def test_direct_budget_exhaustion_still_gates():
    allowed, events = await _judge_raising(EngineBudgetError("budget"))
    assert allowed is False
    assert events == []


async def test_cancellation_still_propagates():
    with pytest.raises(asyncio.CancelledError):
        await _judge_raising(asyncio.CancelledError())


async def test_oversized_codex_request_construction_is_refused_before_spawn(monkeypatch, tmp_path):
    from lionagi.providers.openai import codex

    spawned = []

    async def spawn(*args, **kwargs):
        spawned.append(True)
        raise AssertionError("an oversized request started a subprocess")

    monkeypatch.setattr(codex, "CODEX_CLI", sys.executable)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    prompt = "x" * (codex.CODEX_MAX_TURN_INPUT_CHARS + 1)

    with pytest.raises(CLIInputRefusedError):
        codex.CodexCodeRequest(prompt=prompt, repo=tmp_path)
    assert spawned == []

    class Agent:
        async def operate(self, *, instruction):
            codex.CodexCodeRequest(prompt=prompt, repo=tmp_path)

    engine = Engine(judge_model="scripted/judge")
    run = engine.new_run()

    async def make_agent(*args, **kwargs):
        return Agent()

    run.make_agent = make_agent
    with pytest.raises(CLIInputRefusedError):
        await engine.judge(run, "item", "subject")
    assert spawned == []


async def test_oversized_copied_codex_request_stays_advisory_before_spawn(monkeypatch, tmp_path):
    from lionagi.providers.openai import codex

    spawned = []

    async def spawn(*args, **kwargs):
        spawned.append(True)
        raise AssertionError("an oversized request started a subprocess")

    monkeypatch.setattr(codex, "CODEX_CLI", sys.executable)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    request = codex.CodexCodeRequest(prompt="ok", repo=tmp_path).model_copy(
        update={"prompt": "x" * (codex.CODEX_MAX_TURN_INPUT_CHARS + 1)}
    )

    class Agent:
        async def operate(self, *, instruction):
            async with aclosing(codex._ndjson_from_cli(request)) as stream:
                async for _ in stream:
                    pass

    with pytest.raises(ValueError):
        await Agent().operate(instruction="subject")
    engine = Engine(judge_model="scripted/judge")
    run = engine.new_run()
    events = []
    run.notify = lambda kind, **kwargs: events.append((kind, kwargs))

    async def make_agent(*args, **kwargs):
        return Agent()

    run.make_agent = make_agent
    assert await engine.judge(run, "item", "subject") is True
    assert [event for event, _ in events] == ["judge_error"]
    assert spawned == []


@pytest.mark.parametrize("refusal_type", _REFUSALS, ids=lambda cls: cls.__name__)
async def test_synthetic_pre_spawn_argv_refusal_redacts_chain_and_only_preserves_cli_type(
    monkeypatch, refusal_type
):
    from lionagi.providers import _cli_subprocess as cli

    secret = "synthetic-secret-value"
    spawned = []
    released = []

    class Lease:
        slots = []

        def release(self):
            released.append(True)

    async def admit(**kwargs):
        return Lease()

    async def fill(env):
        return env, frozenset({"API_KEY"})

    async def spawn(*args, **kwargs):
        spawned.append(True)
        raise AssertionError("a refused request started a subprocess")

    def refuse(_env):
        raise refusal_type(f"refused: {secret}") from RuntimeError(f"cause: {secret}")

    monkeypatch.setattr(cli, "admit_subagent", admit)
    monkeypatch.setattr(cli, "fill_declared_secrets_and_names", fill)
    monkeypatch.setattr(cli, "refuse_nested_agent", lambda _engine: None)
    monkeypatch.setattr(cli.asyncio, "create_subprocess_exec", spawn)

    class Agent:
        async def operate(self, *, instruction):
            stream = cli.ndjson_from_cli(
                [sys.executable], env={"API_KEY": secret}, _prepare_argv=refuse
            )
            async with aclosing(stream):
                async for _ in stream:
                    pass

    engine = Engine(judge_model="scripted/judge")
    run = engine.new_run()

    async def make_agent(*args, **kwargs):
        return Agent()

    run.make_agent = make_agent
    expected = CLIInputRefusedError if refusal_type is CLIInputRefusedError else RuntimeError
    with pytest.raises(expected) as direct:
        await Agent().operate(instruction="subject")
    assert type(direct.value) is expected
    assert secret not in str(direct.value)
    assert direct.value.__cause__ is None
    assert direct.value.__context__ is None

    if refusal_type is CLIInputRefusedError:
        with pytest.raises(CLIInputRefusedError) as caught:
            await engine.judge(run, "item", "subject")
        assert secret not in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
    else:
        events = []
        run.notify = lambda kind, **kwargs: events.append((kind, kwargs))
        assert await engine.judge(run, "item", "subject") is True
        assert [event for event, _ in events] == ["judge_error"]
    assert spawned == []
    assert released == [True, True]
