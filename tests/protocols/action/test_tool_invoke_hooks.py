# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for the tool-event hook layer at ActionManager.invoke.

Covers: ordering (external pre -> spec-level security_pre -> tool ->
post), deny/ask/unrecognized-decision fail-closed semantics, rewrite +
revalidation (including a rewrite that fails revalidation and is
rejected), post hooks firing on both success and failure, the documented
direct-FunctionCalling bypass, and no-op behavior with no hooks
registered.
"""

import pytest
from pydantic import BaseModel

from lionagi.agent.factory import _chain_pre_hooks
from lionagi.protocols.action.function_calling import FunctionCalling
from lionagi.protocols.action.manager import ActionManager
from lionagi.protocols.action.tool import Tool
from lionagi.protocols.action.tool_hooks import (
    ToolHookDeniedError,
    ToolPostDecision,
    ToolPreDecision,
)
from lionagi.protocols.generic.event import EventStatus


class AddArgs(BaseModel):
    a: int
    b: int


def _build_manager(order: list[str], calls: list[tuple[int, int]], *, with_security: bool = False):
    """Return (manager, tool_name) for an 'add' tool that records call order."""

    async def add(a: int, b: int) -> int:
        order.append("invoke")
        calls.append((a, b))
        return a + b

    preprocessor = None
    if with_security:

        async def security_guard(tool_name: str, action: str, args: dict) -> dict | None:
            order.append("security_pre")
            return None

        preprocessor = _chain_pre_hooks("add", [security_guard])

    tool = Tool(func_callable=add, request_options=AddArgs, preprocessor=preprocessor)
    manager = ActionManager(tool)
    return manager, "add"


# ── Ordering ──────────────────────────────────────────────────────────────


async def test_pre_then_security_pre_then_invoke_then_post_ordering():
    order: list[str] = []
    calls: list[tuple[int, int]] = []
    manager, tool_name = _build_manager(order, calls, with_security=True)

    async def external_pre(name: str, arguments: dict) -> None:
        order.append("pre")
        return None

    async def external_post(name: str, arguments: dict, result, error) -> None:
        order.append("post")

    manager.add_tool_pre_hook(external_pre)
    manager.add_tool_post_hook(external_post)

    fc = await manager.invoke({"function": tool_name, "arguments": {"a": 1, "b": 2}})

    assert order == ["pre", "security_pre", "invoke", "post"]
    assert fc.status == EventStatus.COMPLETED
    assert fc.response == 3


async def test_security_pre_sees_external_rewrite_with_no_user_hook():
    """D3: security_pre always validates the post-rewrite values, even with
    no spec-level user pre-hook present."""
    order: list[str] = []
    calls: list[tuple[int, int]] = []
    seen_args: list[dict] = []

    async def add(a: int, b: int) -> int:
        calls.append((a, b))
        return a + b

    async def security_guard(tool_name: str, action: str, args: dict) -> dict | None:
        seen_args.append(dict(args))
        return None

    preprocessor = _chain_pre_hooks("add", [security_guard])
    tool = Tool(func_callable=add, request_options=AddArgs, preprocessor=preprocessor)
    manager = ActionManager(tool)

    async def external_pre(name: str, arguments: dict) -> ToolPreDecision:
        return ToolPreDecision(decision="allow", updated_input={"a": 100, "b": 200})

    manager.add_tool_pre_hook(external_pre)

    fc = await manager.invoke({"function": "add", "arguments": {"a": 1, "b": 2}})

    assert seen_args == [{"a": 100, "b": 200}]
    assert calls == [(100, 200)]
    assert fc.response == 300


# ── Deny / ask / unrecognized (D5) ──────────────────────────────────────────


async def test_deny_short_circuits_before_invoke_and_captures_as_failed():
    """A denying pre-hook must fail the call closed the same way every other
    denial path in this module does: FAILED status with the denial captured
    as the error, never raised out of ActionManager.invoke. Post hooks still
    run on the deny path (matching the "failures run post hooks too"
    contract that already holds for ordinary tool exceptions and
    schema-revalidation failures), receiving an isolated snapshot that a
    mutating hook cannot leak back into the live event."""
    order: list[str] = []
    calls: list[tuple[int, int]] = []
    manager, tool_name = _build_manager(order, calls, with_security=True)

    async def denier(name: str, arguments: dict) -> ToolPreDecision:
        order.append("pre-deny")
        return ToolPreDecision(decision="deny", reason="not today")

    post_calls: list = []

    async def external_post(name: str, arguments: dict, result, error) -> None:
        if error is not None:
            error.reason = "forged"
        post_calls.append((name, result, error))

    manager.add_tool_pre_hook(denier)
    manager.add_tool_post_hook(external_post)

    fc = await manager.invoke({"function": tool_name, "arguments": {"a": 1, "b": 2}})

    assert order == ["pre-deny"], "security_pre and the tool callable must never run"
    assert calls == [], "the tool callable must never run on deny"
    assert fc.status == EventStatus.FAILED
    assert isinstance(fc.execution.error, ToolHookDeniedError)
    assert "not today" in str(fc.execution.error)
    # ToolHookDeniedError is reconstructable via __reduce__ (hook_name,
    # reason), so evidence isolation succeeds and the post hook observes the
    # denial instead of being silently skipped.
    assert len(post_calls) == 1
    name, result, observed_error = post_calls[0]
    assert name == tool_name
    assert result is None
    assert isinstance(observed_error, ToolHookDeniedError)
    # The hook mutated its snapshot ("forged") -- confirm that never leaks
    # back into the live event's error, which must still read "not today".
    assert observed_error.reason == "forged"
    assert fc.execution.error.reason == "not today"


async def test_ask_fails_closed():
    order: list[str] = []
    calls: list[tuple[int, int]] = []
    manager, tool_name = _build_manager(order, calls)

    async def asker(name: str, arguments: dict) -> ToolPreDecision:
        return ToolPreDecision(decision="ask")

    manager.add_tool_pre_hook(asker)

    fc = await manager.invoke({"function": tool_name, "arguments": {"a": 1, "b": 2}})

    assert calls == []
    assert fc.status == EventStatus.FAILED
    assert isinstance(fc.execution.error, ToolHookDeniedError)
    assert "failing closed" in str(fc.execution.error)


async def test_unrecognized_decision_fails_closed_with_diagnostic():
    order: list[str] = []
    calls: list[tuple[int, int]] = []
    manager, tool_name = _build_manager(order, calls)

    async def confused(name: str, arguments: dict) -> ToolPreDecision:
        return ToolPreDecision(decision="warn")

    manager.add_tool_pre_hook(confused)

    fc = await manager.invoke({"function": tool_name, "arguments": {"a": 1, "b": 2}})

    assert calls == []
    assert fc.status == EventStatus.FAILED
    assert isinstance(fc.execution.error, ToolHookDeniedError)
    assert "unrecognized decision 'warn'" in str(fc.execution.error)


async def test_pre_hook_raising_permission_error_denies():
    """Legacy-style hooks (raise PermissionError) are supported directly."""
    order: list[str] = []
    calls: list[tuple[int, int]] = []
    manager, tool_name = _build_manager(order, calls)

    async def legacy_guard(name: str, arguments: dict) -> None:
        raise PermissionError("blocked by legacy guard")

    manager.add_tool_pre_hook(legacy_guard)

    fc = await manager.invoke({"function": tool_name, "arguments": {"a": 1, "b": 2}})

    assert calls == []
    assert fc.status == EventStatus.FAILED
    assert isinstance(fc.execution.error, ToolHookDeniedError)
    assert "blocked by legacy guard" in str(fc.execution.error)


# ── Rewrite + revalidation ──────────────────────────────────────────────────


async def test_rewrite_via_updated_input_is_revalidated_and_used():
    order: list[str] = []
    calls: list[tuple[int, int]] = []
    manager, tool_name = _build_manager(order, calls, with_security=True)

    async def rewriter(name: str, arguments: dict) -> ToolPreDecision:
        return ToolPreDecision(decision="allow", updated_input={"a": 10, "b": 20})

    manager.add_tool_pre_hook(rewriter)

    fc = await manager.invoke({"function": tool_name, "arguments": {"a": 1, "b": 2}})

    assert calls == [(10, 20)]
    assert fc.status == EventStatus.COMPLETED
    assert fc.response == 30


async def test_rewrite_via_plain_dict_return_is_supported():
    order: list[str] = []
    calls: list[tuple[int, int]] = []
    manager, tool_name = _build_manager(order, calls)

    async def rewriter(name: str, arguments: dict) -> dict:
        return {"a": 7, "b": 8}

    manager.add_tool_pre_hook(rewriter)

    fc = await manager.invoke({"function": tool_name, "arguments": {"a": 1, "b": 2}})

    assert calls == [(7, 8)]
    assert fc.response == 15


async def test_rewrite_that_fails_revalidation_is_rejected():
    """A rewrite that violates the tool's request_options is a deny-equivalent
    block: the callable never runs, and the failure is captured on the event
    (matching the existing spec-level-preprocessor-error convention) rather
    than raised out of ActionManager.invoke."""
    order: list[str] = []
    calls: list[tuple[int, int]] = []
    manager, tool_name = _build_manager(order, calls, with_security=True)

    async def bad_rewriter(name: str, arguments: dict) -> ToolPreDecision:
        # Drops the required 'b' field -- fails AddArgs validation.
        return ToolPreDecision(decision="allow", updated_input={"a": 999})

    manager.add_tool_pre_hook(bad_rewriter)

    fc = await manager.invoke({"function": tool_name, "arguments": {"a": 1, "b": 2}})

    assert calls == [], "the callable must never run when the rewrite fails revalidation"
    assert fc.status == EventStatus.FAILED
    assert isinstance(fc.execution.error, PermissionError)
    assert "rewritten arguments failed validation" in str(fc.execution.error)
    # The spec-level security_pre chain still ran on the rewritten (invalid)
    # dict before the tool-body validation step catches the schema break.
    assert order == ["security_pre"]


# ── Post hooks: success and failure ─────────────────────────────────────────


async def test_post_hook_fires_on_success_with_result_and_no_error():
    order: list[str] = []
    calls: list[tuple[int, int]] = []
    manager, tool_name = _build_manager(order, calls)

    seen: list[tuple] = []

    async def post(name: str, arguments: dict, result, error) -> None:
        seen.append((name, arguments, result, error))

    manager.add_tool_post_hook(post)

    await manager.invoke({"function": tool_name, "arguments": {"a": 3, "b": 4}})

    assert len(seen) == 1
    name, arguments, result, error = seen[0]
    assert name == tool_name
    assert arguments == {"a": 3, "b": 4}
    assert result == 7
    assert error is None


async def test_post_hook_fires_on_tool_failure_with_error_and_no_result():
    calls: list[str] = []

    async def boom(a: int) -> int:
        calls.append("called")
        raise RuntimeError("kaboom")

    tool = Tool(func_callable=boom)
    manager = ActionManager(tool)

    seen: list[tuple] = []

    async def post(name: str, arguments: dict, result, error) -> None:
        seen.append((name, result, error))

    manager.add_tool_post_hook(post)

    fc = await manager.invoke({"function": "boom", "arguments": {"a": 1}})

    assert calls == ["called"]
    assert fc.status == EventStatus.FAILED
    assert len(seen) == 1
    name, result, error = seen[0]
    assert result is None
    assert isinstance(error, RuntimeError)
    assert "kaboom" in str(error)


async def test_post_hook_advisory_reason_collected_but_does_not_change_outcome():
    order: list[str] = []
    calls: list[tuple[int, int]] = []
    manager, tool_name = _build_manager(order, calls)

    async def annotate(name: str, arguments: dict, result, error) -> ToolPostDecision:
        return ToolPostDecision(reason="looked fine to me")

    manager.add_tool_post_hook(annotate)

    fc = await manager.invoke({"function": tool_name, "arguments": {"a": 1, "b": 1}})

    assert fc.status == EventStatus.COMPLETED
    assert fc.response == 2


async def test_post_hook_note_surfaced_on_metadata_for_success():
    """A post hook's ToolPostDecision.reason must not vanish: it is
    collected by run_tool_post_hooks and attached to the returned
    FunctionCalling event's metadata so callers/telemetry can observe it."""
    order: list[str] = []
    calls: list[tuple[int, int]] = []
    manager, tool_name = _build_manager(order, calls)

    async def annotate(name: str, arguments: dict, result, error) -> ToolPostDecision:
        return ToolPostDecision(reason="looked fine to me")

    manager.add_tool_post_hook(annotate)

    fc = await manager.invoke({"function": tool_name, "arguments": {"a": 1, "b": 1}})

    assert fc.status == EventStatus.COMPLETED
    assert fc.metadata["tool_post_hook_notes"] == ["looked fine to me"]


async def test_post_hook_note_surfaced_on_metadata_for_failure():
    """The same note-surfacing must happen when the tool itself failed --
    post hooks are advisory and still run in the finally block, and their
    notes must reach the caller on the failure path too."""

    async def boom(a: int) -> int:
        raise RuntimeError("kaboom")

    manager = ActionManager(Tool(func_callable=boom))

    async def annotate(name: str, arguments: dict, result, error) -> ToolPostDecision:
        return ToolPostDecision(reason=f"observed failure: {error}")

    manager.add_tool_post_hook(annotate)

    fc = await manager.invoke({"function": "boom", "arguments": {"a": 1}})

    assert fc.status == EventStatus.FAILED
    assert fc.metadata["tool_post_hook_notes"] == ["observed failure: kaboom"]


async def test_post_hook_multiple_notes_collected_in_order():
    order: list[str] = []
    calls: list[tuple[int, int]] = []
    manager, tool_name = _build_manager(order, calls)

    async def first_note(name: str, arguments: dict, result, error) -> ToolPostDecision:
        return ToolPostDecision(reason="first")

    async def no_note(name: str, arguments: dict, result, error) -> ToolPostDecision:
        return ToolPostDecision()

    async def second_note(name: str, arguments: dict, result, error) -> ToolPostDecision:
        return ToolPostDecision(reason="second")

    manager.add_tool_post_hook(first_note)
    manager.add_tool_post_hook(no_note)
    manager.add_tool_post_hook(second_note)

    fc = await manager.invoke({"function": tool_name, "arguments": {"a": 1, "b": 1}})

    assert fc.metadata["tool_post_hook_notes"] == ["first", "second"]


async def test_post_hook_no_notes_leaves_metadata_key_absent():
    """When no post hook returns a reason, the metadata key is never added
    -- absence, not an empty list, is the "nothing to observe" signal."""
    order: list[str] = []
    calls: list[tuple[int, int]] = []
    manager, tool_name = _build_manager(order, calls)

    async def silent(name: str, arguments: dict, result, error) -> None:
        return None

    manager.add_tool_post_hook(silent)

    fc = await manager.invoke({"function": tool_name, "arguments": {"a": 1, "b": 1}})

    assert "tool_post_hook_notes" not in fc.metadata


async def test_post_hook_error_is_isolated_and_does_not_break_the_caller():
    order: list[str] = []
    calls: list[tuple[int, int]] = []
    manager, tool_name = _build_manager(order, calls)

    async def broken_post(name: str, arguments: dict, result, error) -> None:
        raise RuntimeError("observer bug")

    manager.add_tool_post_hook(broken_post)

    fc = await manager.invoke({"function": tool_name, "arguments": {"a": 1, "b": 1}})

    assert fc.status == EventStatus.COMPLETED
    assert fc.response == 2


async def test_mutating_raising_post_hook_cannot_alter_the_completed_event():
    """A post hook that mutates its (top-level) arguments/result dict and then
    raises must never leak that mutation into the completed event."""

    async def echo(value: str) -> dict:
        return {"value": value, "echoed": True}

    tool = Tool(func_callable=echo)
    manager = ActionManager(tool)

    async def mutate_and_raise(name: str, arguments: dict, result: dict, error) -> None:
        arguments["injected"] = "from-hook"
        result["injected"] = "from-hook"
        raise RuntimeError("observer bug")

    manager.add_tool_post_hook(mutate_and_raise)

    fc = await manager.invoke({"function": "echo", "arguments": {"value": "original"}})

    assert fc.status == EventStatus.COMPLETED
    assert fc.arguments == {"value": "original"}
    assert fc.response == {"value": "original", "echoed": True}


async def test_post_hook_cannot_mutate_result_when_deepcopy_fails():
    class UncopyableResult:
        def __init__(self) -> None:
            self.value = "original"

        def __deepcopy__(self, memo):
            raise TypeError("cannot copy")

    result = UncopyableResult()

    async def return_uncopyable() -> UncopyableResult:
        return result

    manager = ActionManager(Tool(func_callable=return_uncopyable))

    async def mutate(name: str, arguments: dict, observed_result, error) -> None:
        observed_result.value = "forged"

    manager.add_tool_post_hook(mutate)

    fc = await manager.invoke({"function": "return_uncopyable", "arguments": {}})

    assert fc.status == EventStatus.COMPLETED
    assert fc.response is result
    assert fc.response.value == "original"


async def test_each_post_hook_receives_independent_argument_and_result_snapshots():
    async def echo(value: str) -> dict:
        return {"value": value}

    manager = ActionManager(Tool(func_callable=echo))
    seen: list[tuple[dict, dict]] = []

    async def forge(name: str, arguments: dict, result: dict, error) -> None:
        arguments["value"] = "forged"
        result["value"] = "forged"

    async def observe(name: str, arguments: dict, result: dict, error) -> None:
        seen.append((arguments, result))

    manager.add_tool_post_hook(forge)
    manager.add_tool_post_hook(observe)

    fc = await manager.invoke({"function": "echo", "arguments": {"value": "original"}})

    assert seen == [({"value": "original"}, {"value": "original"})]
    assert fc.arguments == {"value": "original"}
    assert fc.response == {"value": "original"}


async def test_post_hook_cannot_rewrite_completed_error():
    class MutableError(RuntimeError):
        pass

    failure = MutableError("original")
    failure.details = {"source": "tool"}

    async def fail() -> None:
        raise failure

    manager = ActionManager(Tool(func_callable=fail))

    async def rewrite_error(name: str, arguments: dict, result, error) -> None:
        error.args = ("forged",)
        error.details["source"] = "hook"

    manager.add_tool_post_hook(rewrite_error)

    fc = await manager.invoke({"function": "fail", "arguments": {}})

    assert fc.status == EventStatus.FAILED
    assert fc.execution.error is failure
    assert fc.execution.error.args == ("original",)
    assert fc.execution.error.details == {"source": "tool"}


async def test_cancellation_propagates_promptly_despite_slow_post_hook():
    """A cancellation delivered while the tool call is in flight must not be
    held up by an in-flight tool-post hook -- otherwise wait_for/cancel-based
    timeouts silently inherit the hook's latency."""
    import asyncio
    import time

    tool_started = asyncio.Event()

    async def slow_tool() -> None:
        tool_started.set()
        await asyncio.sleep(10)

    manager = ActionManager(Tool(func_callable=slow_tool))

    async def slow_post_hook(name, arguments, result, error) -> None:
        await asyncio.sleep(2)

    manager.add_tool_post_hook(slow_post_hook)

    task = asyncio.create_task(manager.invoke({"function": "slow_tool", "arguments": {}}))
    await tool_started.wait()

    task.cancel()
    start = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, "cancellation must not be delayed by a slow tool-post hook"


# ── No hooks registered: unchanged behavior ─────────────────────────────────


async def test_no_hooks_registered_behaves_as_before():
    order: list[str] = []
    calls: list[tuple[int, int]] = []
    manager, tool_name = _build_manager(order, calls)

    fc = await manager.invoke({"function": tool_name, "arguments": {"a": 5, "b": 6}})

    assert fc.status == EventStatus.COMPLETED
    assert fc.response == 11
    assert calls == [(5, 6)]


# ── Documented bypass: direct FunctionCalling construction ─────────────────


async def test_direct_function_calling_construction_bypasses_external_hooks():
    """D3's named, tested limit: constructing FunctionCalling directly (not
    via ActionManager.invoke) never sees the tool-pre/tool-post hook layer,
    even though the Tool object is the same one registered on a manager that
    has hooks attached."""
    order: list[str] = []
    calls: list[tuple[int, int]] = []
    manager, tool_name = _build_manager(order, calls)

    pre_fired: list[str] = []

    async def external_pre(name: str, arguments: dict) -> ToolPreDecision:
        pre_fired.append(name)
        return ToolPreDecision(decision="deny", reason="should never be consulted")

    manager.add_tool_pre_hook(external_pre)

    tool = manager.registry[tool_name]
    fc = FunctionCalling(func_tool=tool, arguments={"a": 1, "b": 2})
    await fc.invoke()

    assert pre_fired == [], "external hooks must not fire on a directly constructed FunctionCalling"
    assert fc.status == EventStatus.COMPLETED
    assert fc.response == 3
    assert calls == [(1, 2)]
