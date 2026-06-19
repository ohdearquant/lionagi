# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for TOOL_PRE / TOOL_POST / TOOL_ERROR hook points wired in _act()."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lionagi.hooks.bus import HookBus, HookPoint, StopHook

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_branch(hooks: HookBus | None = None):
    """Return a minimal branch-like mock that _act() can drive."""
    branch = MagicMock()
    branch._hooks = hooks
    branch.id = "branch-test"

    # authorize: always allow
    branch.authorize = AsyncMock(return_value=True)

    # _log_manager
    branch._log_manager.log = MagicMock()

    # msgs
    branch.msgs.a_add_message = AsyncMock()
    branch.messages = []

    # emit_and_log
    branch.emit_and_log = AsyncMock()

    return branch


def _make_func_call(tool_name: str, response=None, duration: float = 0.01):
    """Return a FunctionCalling-like mock."""
    fc = MagicMock()
    fc.response = response
    fc.execution.duration = duration
    fc.status = "completed"
    fc.func_tool.id = "tool-id"
    return fc


# ── TOOL_PRE ──────────────────────────────────────────────────────────────────


async def test_tool_pre_fires_before_invocation():
    bus = HookBus()
    pre_calls: list[dict] = []

    async def on_pre(**kw):
        pre_calls.append(kw)

    bus.on(HookPoint.TOOL_PRE, on_pre)

    fc = _make_func_call("my_tool", response="ok")
    branch = _make_branch(hooks=bus)
    branch._action_manager.invoke = AsyncMock(return_value=fc)

    from lionagi.operations.act.act import _act

    with patch("lionagi.operations.act.act.uuid.uuid4", return_value="fixed-uuid"):
        await _act(branch, {"function": "my_tool", "arguments": {"x": 1}})

    assert len(pre_calls) == 1
    assert pre_calls[0]["tool_name"] == "my_tool"
    assert pre_calls[0]["call_id"] == "fixed-uuid"
    assert "x" in pre_calls[0]["args_summary"]


async def test_tool_pre_fires_before_invoke_is_called():
    """TOOL_PRE must fire before the actual tool invocation."""
    bus = HookBus()
    order: list[str] = []

    async def on_pre(**kw):
        order.append("pre")

    bus.on(HookPoint.TOOL_PRE, on_pre)

    async def fake_invoke(req):
        order.append("invoke")
        return _make_func_call("my_tool", response="ok")

    branch = _make_branch(hooks=bus)
    branch._action_manager.invoke = fake_invoke

    from lionagi.operations.act.act import _act

    await _act(branch, {"function": "my_tool", "arguments": {}})

    assert order == ["pre", "invoke"]


async def test_tool_pre_guard_blocks_invocation():
    """A TOOL_PRE handler raising PermissionError must propagate and skip invocation."""
    bus = HookBus()
    invoke_calls: list = []

    async def guard(**kw):
        raise PermissionError("blocked")

    bus.on(HookPoint.TOOL_PRE, guard)

    async def fake_invoke(req):
        invoke_calls.append(req)
        return _make_func_call("rm", response=None)

    branch = _make_branch(hooks=bus)
    branch._action_manager.invoke = fake_invoke

    from lionagi.operations.act.act import _act

    with pytest.raises(PermissionError, match="blocked"):
        await _act(branch, {"function": "rm", "arguments": {}})

    assert invoke_calls == [], "Invocation must not occur when TOOL_PRE guard raises"


async def test_tool_pre_stophook_blocks_invocation():
    """StopHook from a TOOL_PRE handler must not propagate but does stop the chain."""
    bus = HookBus()
    order: list[str] = []

    async def stopper(**kw):
        order.append("stopper")
        raise StopHook

    async def never(**kw):  # pragma: no cover
        order.append("never")

    bus.on(HookPoint.TOOL_PRE, stopper)
    bus.on(HookPoint.TOOL_PRE, never)

    fc = _make_func_call("ls", response="output")
    branch = _make_branch(hooks=bus)
    branch._action_manager.invoke = AsyncMock(return_value=fc)

    from lionagi.operations.act.act import _act

    await _act(branch, {"function": "ls", "arguments": {}})

    assert order == ["stopper"]


# ── TOOL_POST ─────────────────────────────────────────────────────────────────


async def test_tool_post_fires_on_success():
    bus = HookBus()
    post_calls: list[dict] = []

    async def on_post(**kw):
        post_calls.append(kw)

    bus.on(HookPoint.TOOL_POST, on_post)

    fc = _make_func_call("adder", response=42, duration=0.05)
    branch = _make_branch(hooks=bus)
    branch._action_manager.invoke = AsyncMock(return_value=fc)

    from lionagi.operations.act.act import _act

    with patch("lionagi.operations.act.act.uuid.uuid4", return_value="uid-post"):
        await _act(branch, {"function": "adder", "arguments": {"a": 1}})

    assert len(post_calls) == 1
    assert post_calls[0]["tool_name"] == "adder"
    assert post_calls[0]["call_id"] == "uid-post"
    assert post_calls[0]["duration"] == 0.05
    assert "42" in post_calls[0]["result_summary"]


async def test_tool_post_call_id_matches_pre():
    """The call_id emitted in TOOL_POST must match the one from TOOL_PRE."""
    bus = HookBus()
    ids: dict[str, str] = {}

    async def on_pre(**kw):
        ids["pre"] = kw["call_id"]

    async def on_post(**kw):
        ids["post"] = kw["call_id"]

    bus.on(HookPoint.TOOL_PRE, on_pre)
    bus.on(HookPoint.TOOL_POST, on_post)

    fc = _make_func_call("echo", response="hi")
    branch = _make_branch(hooks=bus)
    branch._action_manager.invoke = AsyncMock(return_value=fc)

    from lionagi.operations.act.act import _act

    await _act(branch, {"function": "echo", "arguments": {}})

    assert ids["pre"] == ids["post"]


async def test_tool_post_does_not_fire_on_error():
    """TOOL_POST must NOT fire when the tool invocation raises."""
    bus = HookBus()
    post_calls: list = []

    async def on_post(**kw):
        post_calls.append(kw)

    bus.on(HookPoint.TOOL_POST, on_post)

    async def failing_invoke(req):
        raise RuntimeError("boom")

    branch = _make_branch(hooks=bus)
    branch._action_manager.invoke = failing_invoke

    from lionagi.operations.act.act import _act

    with pytest.raises(RuntimeError, match="boom"):
        await _act(branch, {"function": "bad_tool", "arguments": {}}, suppress_errors=False)

    assert post_calls == []


# ── TOOL_ERROR ────────────────────────────────────────────────────────────────


async def test_tool_error_fires_on_invocation_exception():
    bus = HookBus()
    error_calls: list[dict] = []

    async def on_error(**kw):
        error_calls.append(kw)

    bus.on(HookPoint.TOOL_ERROR, on_error)

    async def failing_invoke(req):
        raise ValueError("fail")

    branch = _make_branch(hooks=bus)
    branch._action_manager.invoke = failing_invoke

    from lionagi.operations.act.act import _act

    with pytest.raises(ValueError, match="fail"):
        await _act(branch, {"function": "bad_tool", "arguments": {}}, suppress_errors=False)

    assert len(error_calls) == 1
    assert error_calls[0]["tool_name"] == "bad_tool"
    assert isinstance(error_calls[0]["error"], ValueError)


async def test_tool_error_call_id_matches_pre():
    """The call_id in TOOL_ERROR must match the one from TOOL_PRE."""
    bus = HookBus()
    ids: dict[str, str] = {}

    async def on_pre(**kw):
        ids["pre"] = kw["call_id"]

    async def on_error(**kw):
        ids["error"] = kw["call_id"]

    bus.on(HookPoint.TOOL_PRE, on_pre)
    bus.on(HookPoint.TOOL_ERROR, on_error)

    async def failing_invoke(req):
        raise RuntimeError("x")

    branch = _make_branch(hooks=bus)
    branch._action_manager.invoke = failing_invoke

    from lionagi.operations.act.act import _act

    with pytest.raises(RuntimeError):
        await _act(branch, {"function": "fail_tool", "arguments": {}}, suppress_errors=False)

    assert ids["pre"] == ids["error"]


async def test_tool_error_does_not_fire_on_success():
    """TOOL_ERROR must NOT fire when invocation succeeds."""
    bus = HookBus()
    error_calls: list = []

    async def on_error(**kw):
        error_calls.append(kw)

    bus.on(HookPoint.TOOL_ERROR, on_error)

    fc = _make_func_call("ok_tool", response="all good")
    branch = _make_branch(hooks=bus)
    branch._action_manager.invoke = AsyncMock(return_value=fc)

    from lionagi.operations.act.act import _act

    await _act(branch, {"function": "ok_tool", "arguments": {}})

    assert error_calls == []


# ── No-op when no hook bus ────────────────────────────────────────────────────


async def test_no_hooks_registered_still_invokes():
    """Branches without a hook bus must still invoke tools correctly."""
    branch = _make_branch(hooks=None)
    fc = _make_func_call("tool_x", response="result")
    branch._action_manager.invoke = AsyncMock(return_value=fc)

    from lionagi.operations.act.act import _act

    result = await _act(branch, {"function": "tool_x", "arguments": {}})
    assert result.output == "result"
