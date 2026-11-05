# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Characterize the shape of a tool-call denial at each enforcement layer.

A refusal can come from four places along the action path, and each answers
differently today: an observer denial becomes an action response, a blocking
hook point raises, a manager pre-hook records a failed call, and a processor
raises a governance error. Each group below pins, for one source, what the
model sees next (the chat history), what the caller receives, and what is
persisted, so a later consolidation can map every source onto one typed
outcome while keeping the old shape observable.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from lionagi.agent.factory import _chain_pre_hooks
from lionagi.agent.gate import ControlUnavailableError, GateDeniedError
from lionagi.hooks.bus import HookBus, HookPoint, HookSignal, StopHook
from lionagi.operations.act.act import _act
from lionagi.operations.fields import ActionResponseModel
from lionagi.protocols.action.tool import Tool
from lionagi.protocols.action.tool_hooks import (
    ActionGovernanceDeniedError,
    ToolHookDeniedError,
    ToolPreDecision,
)
from lionagi.protocols.generic.event import EventStatus
from lionagi.protocols.messages import ActionResponse
from lionagi.session.branch import Branch
from lionagi.session.observer import SessionObserver
from lionagi.session.session import Session
from lionagi.session.signal import GateDenied, Signal

# Every test that reaches ActionManager.match_tool runs the plugin-shadow lookup,
# which scans `.lionagi` from the git root, the cwd and its parents, and $HOME.
# Point HOME and cwd at a scratch dir so that lookup never reads the real tree.
pytestmark = pytest.mark.usefixtures("plugin_home")


def _add_tool():
    """An 'add' tool that records every execution, so a test can prove a denial never ran it.

    The callable is deliberately unannotated: a function schema is built from
    ``inspect.signature`` annotations, and under ``from __future__ import
    annotations`` they arrive as strings the schema builder cannot read.
    """
    calls: list[tuple[int, int]] = []

    def add(a, b):
        calls.append((a, b))
        return a + b

    return add, calls


def _responses(branch: Branch) -> list[ActionResponse]:
    return [m for m in branch.messages if isinstance(m, ActionResponse)]


def _log_status(branch: Branch) -> list[tuple[str, str | None]]:
    """(execution.status, error class name) for every logged call, in order."""
    out: list[tuple[str, str | None]] = []
    for log in branch.logs:
        execution = log.content["execution"]
        error = execution["error"]
        out.append((execution["status"], error.get("error") if isinstance(error, dict) else None))
    return out


# ── an observer denial becomes an action response ─────────────────────────────


def _gate_raises(invocation):
    raise RuntimeError("policy engine down")


_OBSERVER_GATE_ROWS = [
    ("deny-all", lambda invocation: False),
    ("deny-by-name", lambda invocation: invocation.function != "add"),
    ("raising-gate", _gate_raises),
]


@pytest.mark.parametrize(
    "label,gate", _OBSERVER_GATE_ROWS, ids=[row[0] for row in _OBSERVER_GATE_ROWS]
)
async def test_observer_denial_becomes_an_action_response(label, gate):
    """A session gate denial is returned, not raised; the tool never runs, no
    call is logged, and the denial is audited as a GateDenied observer signal."""
    add, calls = _add_tool()
    branch = Branch()
    branch.register_tools([add])
    session = Session(default_branch=branch)
    session.gate(gate)

    result = await _act(
        branch, {"function": "add", "arguments": {"a": 1, "b": 2}}, suppress_errors=True
    )

    # what the caller receives
    assert isinstance(result, ActionResponseModel)
    assert result.function == "add"
    assert result.arguments == {"a": 1, "b": 2}
    assert result.output == {"error": "denied by governance gate", "function": "add"}

    # what the model sees next: the request, then a matching action response
    assert [(type(m).__name__, getattr(m, "output", None)) for m in branch.messages] == [
        ("ActionRequest", None),
        ("ActionResponse", {"error": "denied by governance gate", "function": "add"}),
    ]

    # what is persisted: the denial on the observer flow, nothing in the call log
    assert calls == []
    assert _log_status(branch) == []
    denials = session.observer.by_type(GateDenied)
    assert len(denials) == 1
    assert denials[0].data is None


async def test_observer_denial_never_raises_even_without_error_suppression():
    add, _ = _add_tool()
    branch = Branch()
    branch.register_tools([add])
    session = Session(default_branch=branch)
    session.gate(lambda invocation: False)

    result = await _act(
        branch, {"function": "add", "arguments": {"a": 1, "b": 2}}, suppress_errors=False
    )

    assert result.output == {"error": "denied by governance gate", "function": "add"}


@pytest.mark.parametrize("allowed", [True, False], ids=["allowed", "denied"])
async def test_observer_emit_gate_denial_stores_the_event_but_skips_routes_and_subscribers(allowed):
    """An emit-time gate denial still appends the event to the flow, but
    suppresses its stream routing and its subscribers."""
    observer = SessionObserver()
    seen: list = []
    observer.observe(Signal, handler=lambda event, ctx: seen.append(event))
    observer.route(lambda payload: True, into="stream")
    observer.gate(lambda payload: allowed)

    await observer.emit(Signal(data={"k": 1}))

    assert len(observer.flow.items) == 1, "the event is stored whatever the verdict"
    assert len(seen) == (1 if allowed else 0)
    assert len(observer.stream("stream")) == (1 if allowed else 0)


# ── a blocking hook point raises ──────────────────────────────────────────────


def _hook_raising(exc_type, message, *, is_async=False):
    if is_async:

        async def guard(**kwargs):
            raise exc_type(message)

    else:

        def guard(**kwargs):
            raise exc_type(message)

    return guard


# The bus has no case for ControlUnavailableError, so that row takes the same
# path as PermissionError. It is kept because a control that cannot answer must
# still refuse the call at TOOL_PRE: a case that swallowed, rewrapped or
# reclassified that class would turn this row red.
_HOOK_DENY_ROWS = [
    (
        "sync-permission-error",
        _hook_raising(PermissionError, "hook says no"),
        PermissionError,
        "hook says no",
    ),
    (
        "async-permission-error",
        _hook_raising(PermissionError, "hook says no", is_async=True),
        PermissionError,
        "hook says no",
    ),
    (
        "control-unavailable",
        _hook_raising(ControlUnavailableError, "backend unreachable"),
        ControlUnavailableError,
        "backend unreachable",
    ),
]


@pytest.mark.parametrize(
    "label,guard,expected_exception,message",
    _HOOK_DENY_ROWS,
    ids=[row[0] for row in _HOOK_DENY_ROWS],
)
async def test_blocking_hook_point_raises(label, guard, expected_exception, message):
    """A blocking TOOL_PRE handler that raises escapes to the caller: no
    response, no history, no log row -- only a denied HookSignal is recorded."""
    add, calls = _add_tool()
    branch = Branch()
    branch.register_tools([add])
    session = Session(default_branch=branch)
    bus = HookBus(observer=session.observer)
    bus.on(HookPoint.TOOL_PRE, guard)
    branch.attach_hook_bus(bus)

    # what the caller receives: the hook's own exception, neither rewritten nor
    # reclassified by the bus, so its exact type names which row fired.
    with pytest.raises(PermissionError) as excinfo:
        await _act(branch, {"function": "add", "arguments": {"a": 1, "b": 2}}, suppress_errors=True)
    assert type(excinfo.value) is expected_exception

    # what the model sees next: nothing, the raise precedes any message write
    assert len(branch.messages) == 0

    # what is persisted: no call log; the block is audited on the observer flow
    assert calls == []
    assert _log_status(branch) == []
    signals = [i for i in session.observer.flow.items.values() if isinstance(i, HookSignal)]
    assert [signal.point for signal in signals] == [HookPoint.TOOL_PRE]
    assert signals[0].kwargs["tool_name"] == "add"
    assert signals[0].kwargs["denied"] is True
    assert signals[0].kwargs["exception"] == f"{expected_exception.__name__}: {message}"


async def test_stop_hook_at_a_blocking_point_ends_the_chain_without_denying():
    add, calls = _add_tool()
    branch = Branch()
    branch.register_tools([add])
    order: list[str] = []
    bus = HookBus()

    def stopper(**kwargs):
        order.append("stopper")
        raise StopHook

    def never(**kwargs):  # pragma: no cover - must never run
        order.append("never")

    bus.on(HookPoint.TOOL_PRE, stopper)
    bus.on(HookPoint.TOOL_PRE, never)
    branch.attach_hook_bus(bus)

    result = await _act(
        branch, {"function": "add", "arguments": {"a": 1, "b": 2}}, suppress_errors=True
    )

    assert order == ["stopper"], "StopHook ends the chain but is not a denial"
    assert result.output == 3
    assert calls == [(1, 2)]


# ── a manager pre-hook records a failed call ──────────────────────────────────


def _manager_hook(result=None, *, raises=None):
    async def hook(name, arguments):
        if raises is not None:
            raise raises
        return result

    return hook


_MANAGER_HOOK_ROWS = [
    (
        "deny",
        _manager_hook(ToolPreDecision(decision="deny", reason="manager says no")),
        "manager says no",
    ),
    (
        "ask",
        _manager_hook(ToolPreDecision(decision="ask", reason="needs a human")),
        "approval ('ask')",
    ),
    (
        "unrecognized-decision",
        _manager_hook(ToolPreDecision(decision="defer", reason="?")),
        "unrecognized decision 'defer'",
    ),
    ("unsupported-return", _manager_hook("not a decision"), "unsupported type str"),
    ("hook-raises", _manager_hook(raises=RuntimeError("hook broke")), "hook broke"),
]


@pytest.mark.parametrize(
    "label,hook,fragment", _MANAGER_HOOK_ROWS, ids=[row[0] for row in _MANAGER_HOOK_ROWS]
)
async def test_manager_pre_hook_records_a_failed_call(label, hook, fragment):
    """A manager pre-hook refusal (deny, ask, an unrecognized or unsupported
    return, or a raising hook) is captured as a failed FunctionCalling: the
    caller still gets a response carrying the reason, the history shows it, and
    the failed call is logged."""
    add, calls = _add_tool()
    branch = Branch()
    branch.register_tools([add])
    branch.acts.add_tool_pre_hook(hook)

    result = await _act(
        branch, {"function": "add", "arguments": {"a": 1, "b": 2}}, suppress_errors=True
    )

    # what the caller receives: a visible error, never a bare None
    assert isinstance(result, ActionResponseModel)
    assert result.output["error"] is not None
    assert fragment in str(result.output["error"])
    assert result.output["message"] == f"Error invoking action 'add': {result.output['error']}"

    # what the model sees next: the error response in history
    responses = _responses(branch)
    assert len(responses) == 1
    assert fragment in str(responses[0].output["error"])

    # what is persisted: a failed call recorded as the hook-denied governance error
    assert calls == []
    assert _log_status(branch) == [("failed", "ToolHookDeniedError")]
    failed = branch.logs[0].content
    assert failed["function"] == "add"
    assert failed["metadata"]["lion_class"].endswith("FunctionCalling")


async def test_manager_pre_hook_denial_raises_without_error_suppression():
    add, _ = _add_tool()
    branch = Branch()
    branch.register_tools([add])
    branch.acts.add_tool_pre_hook(
        _manager_hook(ToolPreDecision(decision="deny", reason="manager says no"))
    )

    with pytest.raises(ToolHookDeniedError, match="manager says no"):
        await _act(
            branch, {"function": "add", "arguments": {"a": 1, "b": 2}}, suppress_errors=False
        )


async def test_manager_pre_hook_denial_emits_tool_error_under_the_tool_pre_call_id():
    """With a hook bus attached, a manager pre-hook denial is audited on the bus as
    TOOL_PRE then TOOL_ERROR, both under one call id (and never TOOL_POST)."""
    add, calls = _add_tool()
    branch = Branch()
    branch.register_tools([add])
    bus = HookBus()
    pre: list[dict] = []
    post: list[dict] = []
    error: list[dict] = []
    bus.on(HookPoint.TOOL_PRE, lambda **kw: pre.append(kw))
    bus.on(HookPoint.TOOL_POST, lambda **kw: post.append(kw))
    bus.on(HookPoint.TOOL_ERROR, lambda **kw: error.append(kw))
    branch.attach_hook_bus(bus)
    branch.acts.add_tool_pre_hook(
        _manager_hook(ToolPreDecision(decision="deny", reason="manager says no"))
    )

    await _act(branch, {"function": "add", "arguments": {"a": 1, "b": 2}}, suppress_errors=True)

    assert calls == []
    assert post == []
    assert len(pre) == 1 and len(error) == 1
    assert pre[0]["call_id"] == error[0]["call_id"]
    assert pre[0]["tool_name"] == "add"
    assert isinstance(error[0]["error"], ToolHookDeniedError)


async def test_manager_pre_hook_denial_is_captured_on_the_managed_call():
    add, calls = _add_tool()
    branch = Branch()
    branch.register_tools([add])
    branch.acts.add_tool_pre_hook(
        _manager_hook(ToolPreDecision(decision="deny", reason="manager says no"))
    )

    denied = await branch.acts.invoke({"function": "add", "arguments": {"a": 1, "b": 2}})

    assert calls == []
    assert denied.status == EventStatus.FAILED
    assert isinstance(denied.execution.error, ActionGovernanceDeniedError)


# ── a processor raises a governance error ─────────────────────────────────────


class _AddArgs(BaseModel):
    a: int
    b: int


def _register(branch, add, *, chain_control=None, raises=None, rewrite=None, request_options=None):
    """Attach one spec-level preprocessor to a fresh Tool on *branch*."""
    if chain_control is not None:

        async def control(tool_name, action, args):
            raise chain_control

        preprocessor = _chain_pre_hooks("add", [control])
    elif rewrite is not None:

        async def preprocessor(args, **kwargs):
            return rewrite(args)

    else:

        async def preprocessor(args, **kwargs):
            raise raises

    branch.acts.register_tool(
        Tool(func_callable=add, request_options=request_options, preprocessor=preprocessor)
    )


_PROCESSOR_ROWS = [
    (
        "security-denial",
        {"chain_control": PermissionError("denied by security rule")},
        True,
        "denied by security rule",
        "GateDeniedError",
    ),
    (
        "control-unavailable",
        {"chain_control": ControlUnavailableError("backend unreachable")},
        True,
        "could not evaluate: backend unreachable",
        "GateDeniedError",
    ),
    (
        "evaluator-fault",
        {"chain_control": RuntimeError("evaluator exploded")},
        True,
        "evaluator error: evaluator exploded",
        "GateDeniedError",
    ),
    (
        "revalidation-denial",
        {"rewrite": lambda args: {"a": args.get("a", 0)}, "request_options": _AddArgs},
        False,
        "rewritten arguments failed validation",
        "RevalidationDeniedError",
    ),
    (
        "tool-hook-denial",
        {"raises": ToolHookDeniedError("preprocessor", "processor says no")},
        False,
        "processor says no",
        "ToolHookDeniedError",
    ),
]


@pytest.mark.parametrize(
    "label,kwargs,output_is_none,fragment,log_class",
    _PROCESSOR_ROWS,
    ids=[row[0] for row in _PROCESSOR_ROWS],
)
async def test_processor_raises_a_governance_error(
    label, kwargs, output_is_none, fragment, log_class
):
    """A spec preprocessor's governance error is captured as a failed call. A
    GateDeniedError, whether the control denied, could not answer or failed,
    still degrades the returned output to a bare None; a RevalidationDeniedError
    or ToolHookDeniedError keeps the reason visible.
    Either way the history and the persisted call both carry it."""
    add, calls = _add_tool()
    branch = Branch()
    _register(branch, add, **kwargs)

    result = await _act(
        branch, {"function": "add", "arguments": {"a": 1, "b": 2}}, suppress_errors=True
    )

    # what the caller receives
    assert isinstance(result, ActionResponseModel)
    if output_is_none:
        assert result.output is None
    else:
        assert fragment in str(result.output["error"])
        assert result.output["message"] == f"Error invoking action 'add': {result.output['error']}"

    # what the model sees next: the error response in history
    responses = _responses(branch)
    assert len(responses) == 1
    assert fragment in str(responses[0].output["error"])

    # what is persisted: a failed call naming the governance error class
    assert calls == []
    assert _log_status(branch) == [("failed", log_class)]


async def test_an_unavailable_control_marks_the_denial_errored_and_a_denying_one_does_not():
    """A control that cannot answer sets ``GateResult.errored``; a control that
    denies does not. That flag is what makes the raised error say the control "could not
    evaluate" instead of naming itself as the verdict."""
    add, _ = _add_tool()
    unavailable = Branch()
    _register(unavailable, add, chain_control=ControlUnavailableError("backend unreachable"))
    with pytest.raises(GateDeniedError) as unavailable_exc:
        await _act(
            unavailable, {"function": "add", "arguments": {"a": 1, "b": 2}}, suppress_errors=False
        )
    assert unavailable_exc.value.result.errored is True
    assert str(unavailable_exc.value) == "control could not evaluate: backend unreachable"

    denied = Branch()
    _register(denied, add, chain_control=PermissionError("denied by security rule"))
    with pytest.raises(GateDeniedError) as denied_exc:
        await _act(
            denied, {"function": "add", "arguments": {"a": 1, "b": 2}}, suppress_errors=False
        )
    assert denied_exc.value.result.errored is False
    assert str(denied_exc.value) == "control: denied by security rule"


async def test_processor_governance_denial_raises_without_error_suppression():
    add, _ = _add_tool()
    branch = Branch()
    _register(branch, add, chain_control=PermissionError("denied by security rule"))

    with pytest.raises(GateDeniedError, match="denied by security rule"):
        await _act(
            branch, {"function": "add", "arguments": {"a": 1, "b": 2}}, suppress_errors=False
        )
