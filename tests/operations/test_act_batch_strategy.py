# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Pins the current behavior of ``act()`` with one turn's action requests, single
or several: what the ``concurrent`` and ``sequential`` batch strategies do about
completion order, the order responses are written back, one failing or denied
request, cancellation mid-batch, and what tool preprocessing does with a rewrite
of the arguments."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from lionagi.operations.act.act import _act, _get_default_call_params, act
from lionagi.operations.fields import ActionResponseModel
from lionagi.operations.types import ActionParam
from lionagi.protocols.action.function_calling import RevalidationDeniedError
from lionagi.protocols.action.tool import Tool
from lionagi.protocols.messages import ActionRequest, ActionResponse
from lionagi.session.branch import Branch
from lionagi.session.session import Session

# Every test that reaches ActionManager.match_tool reaches the plugin registry,
# which scans `.lionagi` from the git root, the cwd and its parents, and $HOME.
# Point HOME and cwd at a scratch dir so that scan never reads the real tree.
pytestmark = pytest.mark.usefixtures("plugin_home")


def _action_param(strategy: str = "concurrent", *, suppress_errors: bool = True) -> ActionParam:
    return ActionParam(
        action_call_params=_get_default_call_params(),
        tools=None,
        strategy=strategy,
        suppress_errors=suppress_errors,
    )


def _response_outputs(messages) -> list:
    return [m.output for m in messages if isinstance(m, ActionResponse)]


# concurrent vs sequential: completion order and write-back order


@pytest.mark.asyncio
async def test_concurrent_strategy_starts_requests_together():
    """Under 'concurrent' both requests are in flight before either finishes:
    each tool waits for the other to have started, which a one-by-one run
    cannot satisfy."""
    branch = Branch()
    a_started = asyncio.Event()
    b_started = asyncio.Event()

    async def tool_a() -> str:
        a_started.set()
        await asyncio.wait_for(b_started.wait(), timeout=1.0)
        return "a"

    async def tool_b() -> str:
        b_started.set()
        await asyncio.wait_for(a_started.wait(), timeout=1.0)
        return "b"

    branch.register_tools([tool_a, tool_b])
    requests = [
        {"function": "tool_a", "arguments": {}},
        {"function": "tool_b", "arguments": {}},
    ]

    results = await act(branch, requests, _action_param("concurrent"))

    assert [r.output for r in results] == ["a", "b"]


@pytest.mark.asyncio
async def test_concurrent_strategy_completes_out_of_request_order_but_returns_in_request_order():
    """'concurrent' lets the second request finish first; the returned list
    still follows request order."""
    branch = Branch()
    completion: list[str] = []
    gate = asyncio.Event()

    async def slow() -> str:
        # Bounded so a serialized batch fails fast and by name rather than
        # hanging until the suite's 300 s timeout.
        await asyncio.wait_for(gate.wait(), timeout=5.0)
        completion.append("slow")
        return "slow"

    async def fast() -> str:
        completion.append("fast")
        gate.set()
        return "fast"

    branch.register_tools([slow, fast])
    requests = [
        {"function": "slow", "arguments": {}},
        {"function": "fast", "arguments": {}},
    ]

    results = await act(branch, requests, _action_param("concurrent"))

    assert completion == ["fast", "slow"]
    assert [r.function for r in results] == ["slow", "fast"]
    assert [r.output for r in results] == ["slow", "fast"]


@pytest.mark.asyncio
async def test_concurrent_strategy_writes_action_responses_in_completion_order():
    """The response messages land in branch.messages in completion order, not
    request order: the first request only finishes after the second request's
    response is already written, and its own response is written second."""
    branch = Branch()
    second_written = asyncio.Event()

    def _on_added(msg) -> None:
        if isinstance(msg, ActionResponse):
            second_written.set()

    branch.on_message_added.append(_on_added)

    async def first() -> str:
        await asyncio.wait_for(second_written.wait(), timeout=5.0)
        return "first"

    def second() -> str:
        return "second"

    branch.register_tools([first, second])
    requests = [
        {"function": "first", "arguments": {}},
        {"function": "second", "arguments": {}},
    ]

    await act(branch, requests, _action_param("concurrent"))

    assert _response_outputs(branch.messages) == ["second", "first"]


@pytest.mark.asyncio
async def test_sequential_strategy_runs_requests_one_at_a_time_in_request_order():
    """'sequential' finishes each request before starting the next, in request
    order."""
    branch = Branch()
    events: list[str] = []

    async def tool_one() -> str:
        events.append("one:start")
        await asyncio.sleep(0)
        events.append("one:end")
        return "one"

    async def tool_two() -> str:
        events.append("two:start")
        events.append("two:end")
        return "two"

    branch.register_tools([tool_one, tool_two])
    requests = [
        {"function": "tool_one", "arguments": {}},
        {"function": "tool_two", "arguments": {}},
    ]

    results = await act(branch, requests, _action_param("sequential"))

    assert events == ["one:start", "one:end", "two:start", "two:end"]
    assert [r.output for r in results] == ["one", "two"]
    assert _response_outputs(branch.messages) == ["one", "two"]


# one request failing: what happens to the others


@pytest.mark.asyncio
async def test_concurrent_suppressed_failure_leaves_other_requests_running():
    """With suppress_errors=True a failing request degrades to a response with
    output None; its siblings still run and the list keeps request order."""
    branch = Branch()
    ran: list[str] = []

    def ok_one() -> str:
        ran.append("ok_one")
        return "ok_one"

    def boom() -> None:
        ran.append("boom")
        raise RuntimeError("tool exploded")

    def ok_two() -> str:
        ran.append("ok_two")
        return "ok_two"

    branch.register_tools([ok_one, boom, ok_two])
    requests = [
        {"function": "ok_one", "arguments": {}},
        {"function": "boom", "arguments": {}},
        {"function": "ok_two", "arguments": {}},
    ]

    results = await act(branch, requests, _action_param("concurrent", suppress_errors=True))

    assert [r.output for r in results] == ["ok_one", None, "ok_two"]
    assert sorted(ran) == ["boom", "ok_one", "ok_two"]


@pytest.mark.asyncio
async def test_sequential_suppressed_failure_leaves_later_requests_running():
    """The sequential path also continues past a suppressed failure, in order."""
    branch = Branch()
    ran: list[str] = []

    def ok_one() -> str:
        ran.append("ok_one")
        return "ok_one"

    def boom() -> None:
        ran.append("boom")
        raise RuntimeError("tool exploded")

    def ok_two() -> str:
        ran.append("ok_two")
        return "ok_two"

    branch.register_tools([ok_one, boom, ok_two])
    requests = [
        {"function": "ok_one", "arguments": {}},
        {"function": "boom", "arguments": {}},
        {"function": "ok_two", "arguments": {}},
    ]

    results = await act(branch, requests, _action_param("sequential", suppress_errors=True))

    assert [r.output for r in results] == ["ok_one", None, "ok_two"]
    assert ran == ["ok_one", "boom", "ok_two"]


@pytest.mark.asyncio
async def test_concurrent_unsuppressed_failure_raises_and_aborts_the_batch():
    """With suppress_errors=False a failing request raises out of the whole
    concurrent batch: a sibling still running when it fails is cancelled, so it
    never finishes."""
    branch = Branch()
    sibling_started = asyncio.Event()
    sibling_cancelled: list[str] = []

    async def sibling() -> str:
        sibling_started.set()
        try:
            # Bounded so a regression that never cancels the sibling fails in
            # seconds instead of sleeping to completion.
            await asyncio.wait_for(asyncio.sleep(30), timeout=5.0)
        except asyncio.CancelledError:
            sibling_cancelled.append("sibling")
            raise
        return "sibling"

    async def boom() -> None:
        await asyncio.wait_for(sibling_started.wait(), timeout=5.0)
        raise RuntimeError("tool exploded")

    branch.register_tools([sibling, boom])
    requests = [
        {"function": "sibling", "arguments": {}},
        {"function": "boom", "arguments": {}},
    ]

    with pytest.raises(RuntimeError, match="tool exploded"):
        await act(branch, requests, _action_param("concurrent", suppress_errors=False))

    assert sibling_cancelled == ["sibling"]


@pytest.mark.asyncio
async def test_sequential_unsuppressed_failure_stops_before_later_requests():
    """The sequential path re-raises at the failing request; requests after it
    never run."""
    branch = Branch()
    ran: list[str] = []

    def boom() -> None:
        ran.append("boom")
        raise RuntimeError("tool exploded")

    def later() -> str:
        ran.append("later")
        return "later"

    branch.register_tools([boom, later])
    requests = [
        {"function": "boom", "arguments": {}},
        {"function": "later", "arguments": {}},
    ]

    with pytest.raises(RuntimeError, match="tool exploded"):
        await act(branch, requests, _action_param("sequential", suppress_errors=False))

    assert ran == ["boom"]


# one request denied by the governance gate


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["concurrent", "sequential"])
async def test_gate_denial_is_a_response_and_does_not_stop_the_batch(strategy):
    """A gate denial never raises; it becomes a response and the other requests
    in the batch still run, under either strategy."""
    branch = Branch()
    ran: list[str] = []

    def add(a, b) -> int:
        ran.append("add")
        return a + b

    def mul(a, b) -> int:
        ran.append("mul")
        return a * b

    branch.register_tools([add, mul])
    session = Session(default_branch=branch)
    session.gate(lambda inv: inv.function != "add")

    requests = [
        {"function": "add", "arguments": {"a": 1, "b": 2}},
        {"function": "mul", "arguments": {"a": 3, "b": 4}},
    ]

    results = await act(branch, requests, _action_param(strategy))

    assert isinstance(results[0], ActionResponseModel)
    assert results[0].output == {"error": "denied by governance gate", "function": "add"}
    assert results[1].output == 12
    assert ran == ["mul"]


@pytest.mark.asyncio
@pytest.mark.parametrize("strategy", ["concurrent", "sequential"])
async def test_gate_denial_does_not_raise_even_with_suppress_errors_false(strategy):
    """The gate denial path returns a response before suppress_errors is
    consulted, so it is a response under suppress_errors=False too."""
    branch = Branch()

    def add(a, b) -> int:
        return a + b

    branch.register_tools([add])
    session = Session(default_branch=branch)
    session.gate(lambda inv: False)

    results = await act(
        branch,
        [{"function": "add", "arguments": {"a": 1, "b": 2}}],
        _action_param(strategy, suppress_errors=False),
    )

    assert results[0].output == {"error": "denied by governance gate", "function": "add"}


@pytest.mark.asyncio
async def test_concurrent_tool_pre_hook_denial_stays_visible_and_leaves_siblings_running():
    """A tool-pre hook denial surfaces as a response carrying the reason (not a
    bare None) and the other request in the batch still runs."""
    from lionagi.protocols.action.tool_hooks import ToolPreDecision

    branch = Branch()

    def add(a, b) -> int:
        return a + b

    def mul(a, b) -> int:
        return a * b

    branch.register_tools([add, mul])

    async def denier(name, arguments):
        if name == "add":
            return ToolPreDecision(decision="deny", reason="not today")
        return ToolPreDecision(decision="allow")

    # branch.acts is Branch's public ActionManager accessor; add_tool_pre_hook
    # itself has no public alias, so the accessor is the public surface here.
    branch.acts.add_tool_pre_hook(denier)

    requests = [
        {"function": "add", "arguments": {"a": 1, "b": 2}},
        {"function": "mul", "arguments": {"a": 3, "b": 4}},
    ]

    results = await act(branch, requests, _action_param("concurrent", suppress_errors=True))

    assert results[0].output is not None
    assert "not today" in str(results[0].output["error"])
    assert results[1].output == 12


# preprocessor rewrite: revalidation after the rewrite, and what the pre-hook sees


class _GuardedArgs(BaseModel):
    value: int


@pytest.mark.asyncio
async def test_preprocessor_rewrite_that_breaks_the_declared_schema_fails_revalidation():
    """A tool with ``request_options`` revalidates after its preprocessor rewrites
    the arguments: a rewrite into an invalid shape is denied, not run."""
    branch = Branch()
    ran: list = []

    def guarded(value):
        ran.append(value)
        return value

    branch.register_tools(
        [
            Tool(
                func_callable=guarded,
                request_options=_GuardedArgs,
                preprocessor=lambda args: {"value": "not-an-int"},
            )
        ]
    )

    result = await _act(
        branch, {"function": "guarded", "arguments": {"value": 5}}, suppress_errors=True
    )

    assert ran == []
    assert "rewritten arguments failed validation" in str(result.output["error"])


@pytest.mark.asyncio
async def test_preprocessor_rewrite_revalidation_raises_when_errors_are_not_suppressed():
    """The revalidation denial re-raises under suppress_errors=False, like any
    other action failure."""
    branch = Branch()
    ran: list = []

    def guarded(value):
        ran.append(value)
        return value

    branch.register_tools(
        [
            Tool(
                func_callable=guarded,
                request_options=_GuardedArgs,
                preprocessor=lambda args: {"value": "not-an-int"},
            )
        ]
    )

    with pytest.raises(RevalidationDeniedError, match="failed validation"):
        await _act(
            branch, {"function": "guarded", "arguments": {"value": 5}}, suppress_errors=False
        )

    assert ran == []


@pytest.mark.asyncio
async def test_pre_hook_sees_the_arguments_from_before_the_preprocessor_rewrite():
    """The manager pre-hook chain runs before the tool's own preprocessor, and is
    not run again after it: the pre-hook observes pre-rewrite arguments even
    though the tool receives the rewritten ones."""
    branch = Branch()
    seen: list[dict] = []
    received: list = []

    def guarded(value):
        received.append(value)
        return value

    async def observer(name, arguments):
        seen.append(dict(arguments))
        return None

    branch.register_tools(
        [
            Tool(
                func_callable=guarded,
                request_options=_GuardedArgs,
                preprocessor=lambda args: {"value": args["value"] + 1},
            )
        ]
    )
    branch.acts.add_tool_pre_hook(observer)

    result = await _act(branch, {"function": "guarded", "arguments": {"value": 5}})

    assert result.output == 6
    assert received == [6]
    # ActionManager.invoke runs its pre-hook chain before FunctionCalling runs the
    # tool's preprocessor, so the hook sees the arguments from before the rewrite.
    # This flips if that chain moves after the preprocessor; a recheck inside the
    # spec's security chain would not change it, since that chain is part of the
    # preprocessor.
    assert seen == [{"value": 5}]


# cancellation mid-batch


@pytest.mark.asyncio
async def test_cancelling_concurrent_batch_cancels_the_in_flight_tool():
    """Cancelling a concurrent batch propagates to the running tool and
    re-raises out of act()."""
    branch = Branch()
    started = asyncio.Event()
    cancelled: list[str] = []

    async def hanging() -> str:
        started.set()
        try:
            await asyncio.wait_for(asyncio.sleep(30), timeout=5.0)
        except asyncio.CancelledError:
            cancelled.append("hanging")
            raise
        return "hanging"

    def instant() -> str:
        return "instant"

    branch.register_tools([hanging, instant])
    requests = [
        {"function": "hanging", "arguments": {}},
        {"function": "instant", "arguments": {}},
    ]

    task = asyncio.create_task(act(branch, requests, _action_param("concurrent")))
    await asyncio.wait_for(started.wait(), timeout=5.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled == ["hanging"]


@pytest.mark.asyncio
async def test_cancelling_sequential_batch_cancels_the_in_flight_tool():
    """Cancelling a sequential batch propagates to the running tool and
    re-raises out of act()."""
    branch = Branch()
    started = asyncio.Event()
    cancelled: list[str] = []

    async def hanging() -> str:
        started.set()
        try:
            await asyncio.wait_for(asyncio.sleep(30), timeout=5.0)
        except asyncio.CancelledError:
            cancelled.append("hanging")
            raise
        return "hanging"

    def later() -> str:
        return "later"

    branch.register_tools([hanging, later])
    requests = [
        {"function": "hanging", "arguments": {}},
        {"function": "later", "arguments": {}},
    ]

    task = asyncio.create_task(act(branch, requests, _action_param("sequential")))
    await asyncio.wait_for(started.wait(), timeout=5.0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert cancelled == ["hanging"]


# response bookkeeping pinned by the batch


@pytest.mark.asyncio
async def test_batch_writes_one_request_and_one_response_per_action():
    """Each executed action leaves one ActionRequest and one ActionResponse in
    branch.messages, and each response names its own request's id: the response
    for a=1,b=10 links to the request for a=1,b=10, and likewise for a=2,b=20."""
    branch = Branch()

    def add(a, b) -> int:
        return a + b

    branch.register_tools([add])
    requests = [
        {"function": "add", "arguments": {"a": 1, "b": 10}},
        {"function": "add", "arguments": {"a": 2, "b": 20}},
    ]

    await act(branch, requests, _action_param("sequential"))

    action_requests = [m for m in branch.messages if isinstance(m, ActionRequest)]
    action_responses = [m for m in branch.messages if isinstance(m, ActionResponse)]
    assert len(action_requests) == 2
    assert len(action_responses) == 2
    # Request and response carry the same arguments, so the pairing is checkable
    # by content: each response's action_request_id must be its own request's id.
    request_id_by_arguments = {str(m.content.arguments): str(m.id) for m in action_requests}
    assert len(request_id_by_arguments) == 2
    for response in action_responses:
        assert (
            response.content.action_request_id
            == request_id_by_arguments[str(response.content.arguments)]
        )
