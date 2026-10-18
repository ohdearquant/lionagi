# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Characterization of the two paths into a tool.

The act path (``operations/act/act.py``) consults Branch authority before the
manager; a direct ``ActionManager.invoke`` or ``FunctionCalling`` call does not.
Pinned here: the full order of the act path, what still refuses a direct call
(manager pre-hooks, plugin trust, a Tool's processors and argument validation),
the record a manager pre-hook denial leaves and what the caller receives, and
where the manager hooks sit relative to a Tool's own processors.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from lionagi.hooks.bus import HookBus, HookPoint
from lionagi.operations.act.act import _act
from lionagi.plugins.discovery import discover_plugins
from lionagi.plugins.registry import PluginRegistry
from lionagi.plugins.trust import trust_plugin
from lionagi.protocols.action.function_calling import FunctionCalling
from lionagi.protocols.action.manager import ActionManager
from lionagi.protocols.action.tool import Tool
from lionagi.protocols.action.tool_hooks import (
    ActionGovernanceDeniedError,
    ToolHookDeniedError,
    ToolPostDecision,
    ToolPreDecision,
)
from lionagi.protocols.generic.event import EventStatus
from lionagi.protocols.messages import ActionRequest, ActionResponse
from lionagi.session.branch import Branch
from lionagi.session.control import ToolInvocation
from lionagi.session.session import Session
from lionagi.utils import Unset

PLUGIN_MANIFEST = """\
name: greeter
version: "0.1.0"
lionagi: ">=0.0,<100.0"

capabilities:
  tools:
    - name: greet
      target: tools/impl.py:do_greet
"""

PLUGIN_FILES = {"tools/impl.py": "def do_greet():\n    return 'hi from plugin'\n"}


# ``match_tool`` reaches the plugin registry for every name: a registered tool is
# checked for a plugin that shadows it, and a miss is resolved against the plugins.
# Either way the registry discovers bundles under the git root, the cwd and its
# parents, and ``~/.lionagi``. Without this the file would read the developer's real
# plugins.
pytestmark = pytest.mark.usefixtures("plugin_home")


class AddArgs(BaseModel):
    a: int
    b: int


async def _deny_add(name: str, arguments: dict) -> ToolPreDecision:
    return ToolPreDecision(decision="deny", reason="not today")


def _branch_with_add() -> tuple[Branch, list[tuple[int, int]]]:
    calls: list[tuple[int, int]] = []

    def add(a, b):
        """Add two numbers."""
        calls.append((a, b))
        return a + b

    branch = Branch()
    branch.register_tools([add])
    return branch, calls


def _bus_recording(order: list[str]) -> HookBus:
    bus = HookBus()
    bus.on(HookPoint.TOOL_PRE, lambda **kw: order.append("bus-tool-pre"))
    bus.on(HookPoint.TOOL_POST, lambda **kw: order.append("bus-tool-post"))
    bus.on(HookPoint.TOOL_ERROR, lambda **kw: order.append("bus-tool-error"))
    return bus


# ── the act path checks Branch authority; a direct call does not ────────────


async def test_act_path_runs_authorize_then_bus_pre_then_manager_hooks_then_tool():
    branch, _ = _branch_with_add()
    order: list[str] = []

    def tool(a, b):
        order.append("tool")
        return a + b

    branch.register_tools([tool], update=True)
    session = Session(default_branch=branch)

    def gate(invocation):
        if isinstance(invocation, ToolInvocation):
            order.append("authorize")
        return True

    session.gate(gate)
    branch._hooks = _bus_recording(order)
    branch._action_manager.add_tool_pre_hook(lambda name, arguments: order.append("manager-pre"))
    branch._action_manager.add_tool_post_hook(
        lambda name, arguments, result, error: order.append("manager-post")
    )

    result = await _act(branch, {"function": "tool", "arguments": {"a": 1, "b": 2}})

    assert result.output == 3
    assert order == [
        "authorize",
        "bus-tool-pre",
        "manager-pre",
        "tool",
        "manager-post",
        "bus-tool-post",
    ]


async def test_act_gate_denial_reaches_no_hook_no_tool_and_no_log():
    branch, calls = _branch_with_add()
    session = Session(default_branch=branch)
    reached: list[str] = []
    seen: list[ToolInvocation] = []

    def gate(invocation):
        if isinstance(invocation, ToolInvocation):
            seen.append(invocation)
            return False
        return True

    session.gate(gate)
    branch._hooks = _bus_recording(reached)
    branch._action_manager.add_tool_pre_hook(lambda name, arguments: reached.append("manager-pre"))

    result = await _act(branch, {"function": "add", "arguments": {"a": 1, "b": 2}})

    assert reached == []
    assert calls == []
    assert len(seen) == 1
    assert isinstance(seen[0], ToolInvocation)
    assert (seen[0].function, seen[0].arguments, seen[0].branch_id) == (
        "add",
        {"a": 1, "b": 2},
        str(branch.id),
    )
    assert result.output == {"error": "denied by governance gate", "function": "add"}
    assert len(branch._log_manager.logs) == 0
    responses = [m for m in branch.messages if isinstance(m, ActionResponse)]
    assert [r.output for r in responses] == [result.output]


async def test_direct_manager_invoke_never_consults_the_session_gate():
    branch, calls = _branch_with_add()
    session = Session(default_branch=branch)
    gate_calls: list[str] = []
    session.gate(lambda inv: gate_calls.append(inv.function) or False)

    func_call = await branch._action_manager.invoke(
        {"function": "add", "arguments": {"a": 2, "b": 3}}
    )

    assert gate_calls == []
    assert func_call.status == EventStatus.COMPLETED
    assert func_call.response == 5
    assert calls == [(2, 3)]


async def test_direct_function_calling_runs_tool_processors_but_no_authorize_or_manager_hooks():
    order: list[str] = []

    def preprocessor(args):
        order.append("tool-preprocessor")
        return args

    def postprocessor(result):
        order.append("tool-postprocessor")
        return result

    def add(a, b):
        order.append("tool")
        return a + b

    branch = Branch()
    branch.register_tools(
        [Tool(func_callable=add, preprocessor=preprocessor, postprocessor=postprocessor)]
    )
    session = Session(default_branch=branch)

    def gate(invocation):
        if isinstance(invocation, ToolInvocation):
            order.append("authorize")
            return False
        return True

    session.gate(gate)
    branch._action_manager.add_tool_pre_hook(lambda name, arguments: order.append("manager-pre"))
    branch._action_manager.add_tool_post_hook(
        lambda name, arguments, result, error: order.append("manager-post")
    )

    func_call = FunctionCalling(
        func_tool=branch._action_manager.registry["add"], arguments={"a": 1, "b": 1}
    )
    await func_call.invoke()

    assert func_call.status == EventStatus.COMPLETED
    assert func_call.response == 2
    assert order == ["tool-preprocessor", "tool", "tool-postprocessor"]


async def test_a_raising_bus_tool_pre_guard_propagates_out_of_the_act_path():
    branch, calls = _branch_with_add()
    bus = HookBus()

    def guard(**kwargs):
        raise RuntimeError("bus guard says no")

    bus.on(HookPoint.TOOL_PRE, guard)
    branch._hooks = bus

    with pytest.raises(RuntimeError, match="bus guard says no"):
        await _act(branch, {"function": "add", "arguments": {"a": 1, "b": 2}})

    assert calls == []
    assert len(branch._log_manager.logs) == 0
    assert [m for m in branch.messages if isinstance(m, ActionResponse)] == []


# ── what can still refuse a call on the direct path ────────────────────────


async def test_manager_pre_hook_refuses_a_direct_call_that_skipped_authorize():
    branch, calls = _branch_with_add()
    session = Session(default_branch=branch)
    gate_calls: list[str] = []
    session.gate(lambda inv: gate_calls.append(inv.function) or True)
    branch._action_manager.add_tool_pre_hook(_deny_add)

    func_call = await branch._action_manager.invoke(
        {"function": "add", "arguments": {"a": 1, "b": 2}}
    )

    assert gate_calls == []
    assert calls == []
    assert func_call.status == EventStatus.FAILED
    assert isinstance(func_call.execution.error, ToolHookDeniedError)


async def test_untrusted_plugin_tool_is_refused_before_any_manager_hook(write_plugin):
    write_plugin("p1", PLUGIN_MANIFEST, files=PLUGIN_FILES)
    PluginRegistry.reset()
    manager = ActionManager()
    seen: list[str] = []
    manager.add_tool_pre_hook(lambda name, arguments: seen.append(name) or None)

    with pytest.raises(ValueError, match="Function greet is not registered"):
        await manager.invoke({"function": "greet", "arguments": {}})

    assert seen == []


async def test_trusted_plugin_tool_runs_direct_but_a_manager_pre_hook_can_still_refuse_it(
    write_plugin,
):
    write_plugin("p1", PLUGIN_MANIFEST, files=PLUGIN_FILES)
    trust_plugin(next(d for d in discover_plugins() if d.dir_name == "p1"))
    PluginRegistry.reset()
    manager = ActionManager()

    allowed = await manager.invoke({"function": "greet", "arguments": {}})
    assert allowed.status == EventStatus.COMPLETED
    assert allowed.response == "hi from plugin"

    manager.add_tool_pre_hook(_deny_add)
    refused = await manager.invoke({"function": "greet", "arguments": {}})
    assert refused.status == EventStatus.FAILED
    assert isinstance(refused.execution.error, ToolHookDeniedError)
    assert refused.response is None


async def test_plugin_tool_is_resolved_fresh_on_each_call_and_never_enters_the_registry(
    write_plugin,
):
    write_plugin("p1", PLUGIN_MANIFEST, files=PLUGIN_FILES)
    trust_plugin(next(d for d in discover_plugins() if d.dir_name == "p1"))
    PluginRegistry.reset()
    manager = ActionManager()

    first = manager.match_tool({"function": "greet", "arguments": {}}).func_tool
    second = manager.match_tool({"function": "greet", "arguments": {}}).func_tool

    assert first.id != second.id
    assert "greet" not in manager.registry


async def test_tool_preprocessor_permission_error_is_captured_as_a_plain_permission_error():
    calls: list[tuple[int, int]] = []
    post_seen: list[tuple] = []

    def add(a, b):
        calls.append((a, b))
        return a + b

    def refuse(args):
        raise PermissionError("preprocessor says no")

    manager = ActionManager(Tool(func_callable=add, request_options=AddArgs, preprocessor=refuse))
    manager.add_tool_post_hook(
        lambda name, arguments, result, error: post_seen.append((result, error))
    )

    func_call = await manager.invoke({"function": "add", "arguments": {"a": 1, "b": 2}})

    assert calls == []
    assert func_call.status == EventStatus.FAILED
    assert isinstance(func_call.execution.error, PermissionError)
    assert not isinstance(func_call.execution.error, ActionGovernanceDeniedError)
    assert post_seen[0][0] is None
    assert str(post_seen[0][1]) == "preprocessor says no"


@pytest.mark.parametrize(
    "arguments",
    [{"a": 1}, {"a": "not-a-number", "b": 2}],
    ids=["missing-required-field", "wrong-type"],
)
async def test_invalid_arguments_refuse_the_call_before_any_hook(arguments):
    calls: list[tuple[int, int]] = []

    def add(a, b):
        calls.append((a, b))
        return a + b

    manager = ActionManager(Tool(func_callable=add, request_options=AddArgs))
    seen: list[str] = []
    manager.add_tool_pre_hook(lambda name, arguments: seen.append(name) or None)
    manager.add_tool_post_hook(lambda name, arguments, result, error: seen.append("post"))

    with pytest.raises(ValueError, match="validation error for FunctionCalling"):
        await manager.invoke({"function": "add", "arguments": arguments})

    assert seen == []
    assert calls == []


async def test_strict_tool_rejects_extra_arguments_before_any_hook():
    def add(a, b):
        return a + b

    manager = ActionManager(Tool(func_callable=add, strict_func_call=True))
    seen: list[str] = []
    manager.add_tool_pre_hook(lambda name, arguments: seen.append(name) or None)

    with pytest.raises(ValueError, match="arguments must match the function schema"):
        await manager.invoke({"function": "add", "arguments": {"a": 1, "b": 2, "c": 3}})

    assert seen == []


# ── a manager pre-hook denial: the record and what the caller receives ─────


async def test_direct_denial_record_shape():
    def add(a, b):
        return a + b

    manager = ActionManager(Tool(func_callable=add, request_options=AddArgs))
    manager.add_tool_pre_hook(_deny_add)

    func_call = await manager.invoke({"function": "add", "arguments": {"a": 1, "b": 2}})

    assert func_call.status == EventStatus.FAILED
    assert func_call.response is None
    assert func_call.arguments == {"a": 1, "b": 2}
    error = func_call.execution.error
    assert isinstance(error, ToolHookDeniedError)
    assert (error.hook_name, error.reason) == ("_deny_add", "not today")
    assert str(error) == "_deny_add: not today"
    assert func_call.execution.duration is Unset
    assert func_call.to_dict()["execution"] == {
        "status": "failed",
        "duration": None,
        "response": None,
        "error": {"error": "ToolHookDeniedError", "message": "_deny_add: not today"},
        "retryable": None,
    }


async def test_a_hook_that_raises_is_recorded_as_a_denial_naming_the_hook():
    def add(a, b):
        return a + b

    def broken_hook(name: str, arguments: dict) -> None:
        raise RuntimeError("policy backend down")

    manager = ActionManager(Tool(func_callable=add))
    manager.add_tool_pre_hook(broken_hook)

    func_call = await manager.invoke({"function": "add", "arguments": {"a": 1, "b": 2}})

    assert func_call.status == EventStatus.FAILED
    assert isinstance(func_call.execution.error, ToolHookDeniedError)
    assert func_call.execution.error.hook_name == "broken_hook"
    assert func_call.execution.error.reason == "hook error: policy backend down"


async def test_denial_discards_earlier_rewrites_and_skips_later_hooks():
    seen_by_hook: list[tuple[str, dict]] = []
    seen_by_post: list[dict] = []

    def add(a, b):
        return a + b

    def rewriter(name: str, arguments: dict) -> dict:
        seen_by_hook.append(("rewriter", dict(arguments)))
        return {"a": 100, "b": 200}

    def denier(name: str, arguments: dict) -> ToolPreDecision:
        seen_by_hook.append(("denier", dict(arguments)))
        return ToolPreDecision(decision="deny", reason="no")

    def never_reached(name: str, arguments: dict) -> None:
        seen_by_hook.append(("never_reached", dict(arguments)))

    manager = ActionManager(Tool(func_callable=add))
    for hook in (rewriter, denier, never_reached):
        manager.add_tool_pre_hook(hook)
    manager.add_tool_post_hook(
        lambda name, arguments, result, error: seen_by_post.append(dict(arguments))
    )

    func_call = await manager.invoke({"function": "add", "arguments": {"a": 1, "b": 2}})

    assert func_call.status == EventStatus.FAILED
    assert func_call.execution.error.reason == "no"
    assert seen_by_hook == [("rewriter", {"a": 1, "b": 2}), ("denier", {"a": 100, "b": 200})]
    assert func_call.arguments == {"a": 1, "b": 2}
    assert seen_by_post == [{"a": 1, "b": 2}]


async def test_pre_hooks_chain_rewrites_in_registration_order():
    seen: list[dict] = []

    def add(a, b):
        return a + b

    def first(name: str, arguments: dict) -> dict:
        seen.append(dict(arguments))
        return {"a": arguments["a"] + 10, "b": arguments["b"]}

    def second(name: str, arguments: dict) -> ToolPreDecision:
        seen.append(dict(arguments))
        return ToolPreDecision(decision="allow", updated_input={"a": arguments["a"], "b": 0})

    manager = ActionManager(Tool(func_callable=add))
    manager.add_tool_pre_hook(first)
    manager.add_tool_pre_hook(second)

    func_call = await manager.invoke({"function": "add", "arguments": {"a": 1, "b": 2}})

    assert seen == [{"a": 1, "b": 2}, {"a": 11, "b": 2}]
    assert func_call.response == 11
    assert func_call.arguments == {"a": 11, "b": 0}


async def test_act_returns_a_manager_denial_as_error_and_message_and_records_the_failed_call():
    branch, calls = _branch_with_add()
    branch._action_manager.add_tool_pre_hook(_deny_add)

    result = await _act(
        branch, {"function": "add", "arguments": {"a": 1, "b": 2}}, suppress_errors=True
    )

    assert calls == []
    assert result.function == "add"
    assert result.arguments == {"a": 1, "b": 2}
    assert result.output == {
        "error": "_deny_add: not today",
        "message": "Error invoking action 'add': _deny_add: not today",
    }

    requests = [m for m in branch.messages if isinstance(m, ActionRequest)]
    responses = [m for m in branch.messages if isinstance(m, ActionResponse)]
    assert len(requests) == 1
    assert [r.output for r in responses] == [
        {
            "error": "_deny_add: not today",
            "function": "add",
            "arguments": {"a": 1, "b": 2},
        }
    ]

    logs = list(branch._log_manager.logs)
    assert len(logs) == 1
    assert logs[0].content["function"] == "add"
    assert logs[0].content["execution"]["status"] == "failed"
    assert logs[0].content["execution"]["error"] == {
        "error": "ToolHookDeniedError",
        "message": "_deny_add: not today",
    }


async def test_act_suppressed_failures_split_governance_from_ordinary_outputs():
    def add(a, b):
        return a + b

    def refuse(args):
        raise PermissionError("preprocessor says no")

    def break_args(args):
        return {"a": 1}

    def build(preprocessor):
        branch = Branch()
        branch.register_tools(
            [Tool(func_callable=add, request_options=AddArgs, preprocessor=preprocessor)]
        )
        return branch

    request = {"function": "add", "arguments": {"a": 1, "b": 2}}

    ordinary = await _act(build(refuse), request, suppress_errors=True)
    assert ordinary.output is None

    governance = await _act(build(break_args), request, suppress_errors=True)
    assert governance.output["error"].startswith("rewritten arguments failed validation")
    assert governance.output["message"].startswith("Error invoking action 'add':")


async def test_act_denial_with_suppress_errors_false_raises_and_leaves_one_failed_log_and_one_response():
    branch, _ = _branch_with_add()
    branch._action_manager.add_tool_pre_hook(_deny_add)

    with pytest.raises(ToolHookDeniedError) as raised:
        await _act(
            branch, {"function": "add", "arguments": {"a": 1, "b": 2}}, suppress_errors=False
        )

    assert raised.value.hook_name == "_deny_add"

    logs = list(branch._log_manager.logs)
    assert len(logs) == 1
    assert logs[0].content["function"] == "add"
    assert logs[0].content["execution"]["status"] == "failed"
    assert logs[0].content["execution"]["error"] == {
        "error": "ToolHookDeniedError",
        "message": "_deny_add: not today",
    }

    responses = [m for m in branch.messages if isinstance(m, ActionResponse)]
    assert len(responses) == 1


# ── hook order relative to a Tool's own processors ─────────────────────────


async def test_manager_hooks_wrap_tool_processors_in_full_order():
    order: list[str] = []
    seen: dict[str, object] = {}

    def add(a, b):
        order.append("callable")
        seen["callable-args"] = (a, b)
        return a + b

    def preprocessor(args):
        order.append("tool-preprocessor")
        seen["preprocessor-args"] = dict(args)
        return {"a": args["a"], "b": args["b"] * 10}

    def postprocessor(result):
        order.append("tool-postprocessor")
        return f"sum={result}"

    def manager_pre(name: str, arguments: dict) -> ToolPreDecision:
        order.append("manager-pre")
        seen["pre-args"] = dict(arguments)
        return ToolPreDecision(decision="allow", updated_input={"a": 5, "b": 2})

    def manager_post(name: str, arguments: dict, result, error) -> None:
        order.append("manager-post")
        seen["post"] = (dict(arguments), result, error)

    manager = ActionManager(
        Tool(func_callable=add, preprocessor=preprocessor, postprocessor=postprocessor)
    )
    manager.add_tool_pre_hook(manager_pre)
    manager.add_tool_post_hook(manager_post)

    func_call = await manager.invoke({"function": "add", "arguments": {"a": 1, "b": 1}})

    assert order == [
        "manager-pre",
        "tool-preprocessor",
        "callable",
        "tool-postprocessor",
        "manager-post",
    ]
    assert seen["pre-args"] == {"a": 1, "b": 1}
    assert seen["preprocessor-args"] == {"a": 5, "b": 2}
    assert seen["callable-args"] == (5, 20)
    assert seen["post"] == ({"a": 5, "b": 20}, "sum=25", None)
    assert func_call.response == "sum=25"


async def test_post_hooks_run_after_a_failing_postprocessor_with_no_result():
    seen: list[tuple] = []

    def add(a, b):
        return a + b

    def postprocessor(result):
        raise ValueError("postprocessor failed")

    manager = ActionManager(Tool(func_callable=add, postprocessor=postprocessor))
    manager.add_tool_post_hook(lambda name, arguments, result, error: seen.append((result, error)))

    func_call = await manager.invoke({"function": "add", "arguments": {"a": 1, "b": 2}})

    assert func_call.status == EventStatus.FAILED
    assert func_call.response is None
    assert isinstance(func_call.execution.error, ValueError)
    assert len(seen) == 1
    assert seen[0][0] is None
    assert str(seen[0][1]) == "postprocessor failed"


async def test_post_hook_notes_are_attached_after_the_tool_postprocessor():
    def add(a, b):
        return a + b

    def postprocessor(result):
        return result * 2

    manager = ActionManager(Tool(func_callable=add, postprocessor=postprocessor))
    manager.add_tool_post_hook(
        lambda name, arguments, result, error: ToolPostDecision(reason=f"saw {result}")
    )

    func_call = await manager.invoke({"function": "add", "arguments": {"a": 1, "b": 2}})

    assert func_call.response == 6
    assert func_call.metadata["tool_post_hook_notes"] == ["saw 6"]


async def test_post_hook_notes_are_attached_on_the_deny_path_too():
    def add(a, b):
        return a + b

    manager = ActionManager(Tool(func_callable=add))
    manager.add_tool_pre_hook(_deny_add)
    manager.add_tool_post_hook(
        lambda name, arguments, result, error: ToolPostDecision(reason="note-on-deny")
    )

    func_call = await manager.invoke({"function": "add", "arguments": {"a": 1, "b": 2}})

    assert func_call.status == EventStatus.FAILED
    assert func_call.metadata["tool_post_hook_notes"] == ["note-on-deny"]
