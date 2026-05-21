# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.tools.context.ContextTool — public tool callable."""


from lionagi.session.branch import Branch
from lionagi.tools.context.context import ContextTool


def _make_branch_with_messages():
    """Branch with system + user instruction + assistant response."""
    branch = Branch(system="You are helpful.")
    branch.msgs.add_message(
        instruction=branch.msgs.create_instruction(instruction="user question")
    )
    return branch


async def test_context_tool_unknown_action_returns_structured_error():
    """Unrecognised action returns success=False with descriptive error."""
    branch = _make_branch_with_messages()
    tool = ContextTool().bind(branch)
    result = await tool.func_callable(action="bogus")
    assert result["success"] is False
    assert "bogus" in result["error"]


async def test_context_tool_status_returns_counts_and_estimated_tokens():
    """status action returns total count, by_type, and non-negative token estimate."""
    branch = _make_branch_with_messages()
    tool = ContextTool().bind(branch)
    result = await tool.func_callable(action="status")
    assert result["success"] is True
    assert result["total_messages"] >= 1
    assert isinstance(result["by_type"], dict)
    assert result["estimated_tokens"] >= 0


async def test_context_tool_get_messages_empty_range_returns_empty_list():
    """get_messages with start==end returns an empty messages list."""
    branch = Branch()
    branch.msgs.add_message(
        instruction=branch.msgs.create_instruction(instruction="msg1")
    )
    tool = ContextTool().bind(branch)
    # start=1, end=1 is an empty range — no summaries iterated
    result = await tool.func_callable(action="get_messages", start=1, end=1)
    assert result["success"] is True
    assert result["messages"] == []


async def test_context_tool_evict_does_not_remove_first_message():
    """evict clamps start to 1, preserving the system message at index 0."""
    branch = Branch(system="sys msg")
    # Add two non-system messages
    branch.msgs.add_message(
        instruction=branch.msgs.create_instruction(instruction="q1")
    )
    branch.msgs.add_message(
        instruction=branch.msgs.create_instruction(instruction="q2")
    )
    count_before = len(branch.msgs.progression)
    assert count_before >= 3  # system + 2 instructions

    tool = ContextTool().bind(branch)
    # start=1, end=2 targets the first non-system message
    result = await tool.func_callable(action="evict", start=1, end=2)
    assert result["success"] is True
    assert result["removed"] == 1

    # System message (index 0) must still be present
    first_uid = branch.msgs.progression[0]
    first_msg = branch.msgs.messages[first_uid]
    assert hasattr(first_msg, "role")


async def test_context_tool_evict_invalid_range_returns_error():
    """Evicting an empty range returns success=False."""
    branch = _make_branch_with_messages()
    tool = ContextTool().bind(branch)
    # start > end is an invalid range
    result = await tool.func_callable(action="evict", start=10, end=5)
    assert result["success"] is False


async def test_context_tool_status_counts_roles_and_estimated_tokens():
    """status returns per-role counts and non-negative estimated_tokens."""
    from lionagi.protocols.messages import ActionResponse

    branch = Branch(system="sys msg")
    branch.msgs.add_message(
        instruction=branch.msgs.create_instruction(instruction="user q")
    )
    ar_resp = ActionResponse(
        content={
            "function": "test",
            "arguments": {},
            "output": {"result": "ok"},
        },
    )
    branch.msgs.messages.include(ar_resp)
    branch.msgs.progression.include(ar_resp.id)

    tool = ContextTool().bind(branch)
    result = await tool.func_callable(action="status")
    assert result["success"] is True
    assert result["total_messages"] >= 3
    by_type = result["by_type"]
    assert isinstance(by_type, dict)
    assert sum(by_type.values()) == result["total_messages"]
    assert result["estimated_tokens"] >= 0


async def test_context_tool_get_messages_clamps_requested_range():
    """get_messages clamps out-of-range start/end to valid bounds."""
    branch = Branch(system="sys")
    for i in range(3):
        branch.msgs.add_message(
            instruction=branch.msgs.create_instruction(instruction=f"msg {i}")
        )
    n = len(branch.msgs.progression)

    tool = ContextTool().bind(branch)
    result = await tool.func_callable(action="get_messages", start=-5, end=999)
    assert result["success"] is True
    assert result["range"].startswith("[0:")
    assert f"of {n}" in result["range"]
    for summary in result["messages"]:
        assert summary.startswith("[")


# ---------------------------------------------------------------------------
# C4: keep_last=0 evicts all action results
# ---------------------------------------------------------------------------


async def test_context_tool_keep_last_zero_evicts_all_context():
    """keep_last=0 removes all ActionResponse messages; non-action messages remain."""
    from lionagi.protocols.messages import ActionResponse

    branch = Branch(system="sys")
    branch.msgs.add_message(
        instruction=branch.msgs.create_instruction(instruction="user q")
    )
    for i in range(3):
        ar = ActionResponse(
            content={
                "function": f"fn_{i}",
                "arguments": {},
                "output": {"i": i},
            }
        )
        branch.msgs.messages.include(ar)
        branch.msgs.progression.include(ar.id)

    non_action_count = len(branch.msgs.progression) - 3  # system + instruction

    tool = ContextTool().bind(branch)
    result = await tool.func_callable(action="evict_action_results", keep_last=0)

    assert result["success"] is True
    assert result["removed"] == 3
    assert len(branch.msgs.progression) == non_action_count


async def test_context_tool_evict_action_results_respects_keep_last_zero():
    """evict_action_results with keep_last=0 removes all action responses."""
    from lionagi.protocols.messages import ActionResponse

    branch = Branch(system="sys")
    branch.msgs.add_message(
        instruction=branch.msgs.create_instruction(instruction="user q")
    )
    for i in range(3):
        ar_resp = ActionResponse(
            content={
                "function": f"tool_{i}",
                "arguments": {},
                "output": {"idx": i},
            },
        )
        branch.msgs.messages.include(ar_resp)
        branch.msgs.progression.include(ar_resp.id)

    count_before = len(branch.msgs.progression)
    tool = ContextTool().bind(branch)
    result = await tool.func_callable(action="evict_action_results", keep_last=0)
    assert result["success"] is True
    assert result["removed"] == 3
    assert len(branch.msgs.progression) == count_before - 3
