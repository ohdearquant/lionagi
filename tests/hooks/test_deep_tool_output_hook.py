# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A completed tool call remains successful when its output cannot be rendered."""

import pytest

from lionagi.hooks.bus import HookBus, HookPoint
from lionagi.operations.act.act import _act
from lionagi.protocols.messages.action_response import _OUTPUT_TOO_DEEP
from lionagi.session.branch import Branch


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.delenv("LIONAGI_STATE_DB_URL", raising=False)
    monkeypatch.setattr("lionagi._paths._find_git_root", lambda _cwd: None)


def _nested_output(depth: int, leaf: str):
    value = leaf
    for _ in range(depth):
        value = [value]
    return value


def _branch_with_tool(output):
    calls = []
    events = []

    def tool(payload: str):
        """Return the already prepared value."""
        calls.append(payload)
        return output

    branch = Branch(log_config={"auto_save_on_exit": False})
    branch.register_tools([tool])
    bus = HookBus()
    for point in (HookPoint.TOOL_PRE, HookPoint.TOOL_POST, HookPoint.TOOL_ERROR):

        async def record(_point=point, **kwargs):
            events.append((_point, kwargs))

        bus.on(point, record)
    branch._hooks = bus
    return branch, calls, events


async def _tool_post_summary(output, suppress_errors):
    branch, calls, events = _branch_with_tool(output)

    try:
        result = await _act(
            branch,
            {"function": "tool", "arguments": {"payload": "input"}},
            suppress_errors=suppress_errors,
        )
    except Exception as exc:  # Do not render the deep output in the failure message.
        pytest.fail(f"completed tool call raised {type(exc).__name__}", pytrace=False)

    assert calls == ["input"]
    if result.output is not output:
        pytest.fail("the completed tool did not return its original result", pytrace=False)
    assert [point for point, _ in events] == [HookPoint.TOOL_PRE, HookPoint.TOOL_POST]
    pre, post = events[0][1], events[1][1]
    assert pre["tool_name"] == post["tool_name"] == "tool"
    assert pre["call_id"] == post["call_id"]
    return post["result_summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("suppress_errors", [False, True])
async def test_unrenderable_return_is_tool_post_and_keeps_original_result(suppress_errors):
    secret = "deep-output-sensitive-leaf"

    class UnrenderableResult:
        def __str__(self):
            raise RecursionError(secret)

        def __repr__(self):
            raise RecursionError(secret)

    output = UnrenderableResult()
    summary = await _tool_post_summary(output, suppress_errors)
    assert _OUTPUT_TOO_DEEP == "<output too deeply nested>"
    assert summary == _OUTPUT_TOO_DEEP
    assert secret not in summary


@pytest.mark.asyncio
@pytest.mark.parametrize("suppress_errors", [False, True])
async def test_deep_return_uses_the_rendered_summary_when_available(suppress_errors):
    secret = "deep-output-sensitive-leaf"
    output = _nested_output(50_000, secret)
    try:
        expected = str(output)[:200]
    except RecursionError:
        expected = _OUTPUT_TOO_DEEP

    summary = await _tool_post_summary(output, suppress_errors)

    assert summary == expected
    assert secret not in summary


@pytest.mark.asyncio
async def test_renderable_return_keeps_existing_tool_post_summary():
    output = {"items": ["ok", 2]}
    branch, calls, events = _branch_with_tool(output)

    result = await _act(branch, {"function": "tool", "arguments": {"payload": "input"}})

    assert calls == ["input"]
    assert result.output is output
    assert [point for point, _ in events] == [HookPoint.TOOL_PRE, HookPoint.TOOL_POST]
    pre, post = events[0][1], events[1][1]
    assert pre["tool_name"] == post["tool_name"] == "tool"
    assert pre["call_id"] == post["call_id"]
    assert post["result_summary"] == str(output)
