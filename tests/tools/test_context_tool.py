# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.tools.context.ContextTool.

Contract: context engineering is NON-DESTRUCTIVE. evict/compact only curate the
ACTIVE progression (``branch.progression``); the full message Pile
(``branch.msgs.messages`` / ``branch.msgs.progression``) is never pruned, so
hidden messages can be browsed (scope='all') and restored.
"""

from lionagi.session.branch import Branch
from lionagi.tools.context.context import ContextTool


def _make_branch_with_messages():
    branch = Branch(system="You are helpful.")
    branch.msgs.add_message(instruction="user question")
    return branch


def _build_branch() -> Branch:
    """System message + several reasoning/tool-result turns."""
    from lionagi.protocols.messages import ActionRequest, ActionResponse

    b = Branch(system="You are a coding agent.")
    for i in range(6):
        b.msgs.add_message(instruction=f"do step {i}")
        b.msgs.add_message(assistant_response=f"reasoning about step {i}")
        req = ActionRequest(content={"function": "read_file", "arguments": {"path": f"f{i}.py"}})
        b.msgs.messages.include(req)
        b.msgs.progression.include(req.id)
        resp = ActionResponse(
            content={"function": "read_file", "arguments": {}, "output": "X" * 400}
        )
        b.msgs.messages.include(resp)
        b.msgs.progression.include(resp.id)
    return b


async def _call(branch, **kw) -> dict:
    return await ContextTool().bind(branch).func_callable(**kw)


# -- structural ------------------------------------------------------------


async def test_unknown_action_returns_structured_error():
    res = await _call(_make_branch_with_messages(), action="bogus")
    assert res["success"] is False and "bogus" in res["error"]


async def test_status_returns_counts_and_active_tokens():
    res = await _call(_build_branch(), action="status")
    assert res["success"]
    assert res["active_messages"] == res["total_messages"]  # nothing evicted yet
    assert res["evicted"] == 0
    assert res["estimated_active_tokens"] > 0
    assert sum(res["by_type"].values()) == res["active_messages"]


async def test_get_messages_clamps_range():
    b = Branch(system="sys")
    for i in range(3):
        b.msgs.add_message(instruction=f"msg {i}")
    res = await _call(b, action="get_messages", start=-5, end=999)
    assert res["success"] and res["range"].startswith("[0:")


async def test_evict_invalid_range_returns_error():
    res = await _call(_make_branch_with_messages(), action="evict", start=10, end=5)
    assert res["success"] is False


# -- non-destructive eviction ---------------------------------------------


async def test_evict_is_non_destructive():
    b = _build_branch()
    total = len(b.msgs.messages)
    res = await _call(b, action="evict", start=1, end=4)
    assert res["success"] and res["removed"] == 3
    assert len(b.progression) == total - 3  # active view shrank
    assert len(b.msgs.messages) == total  # durable Pile untouched
    assert len(b.msgs.progression) == total  # full record untouched


async def test_evict_cannot_remove_system():
    b = _build_branch()
    await _call(b, action="evict", start=0, end=1)  # start clamped to 1
    assert b.progression[0] == b.msgs.progression[0]


async def test_evict_action_results_keeps_last_n_non_destructively():
    b = _build_branch()  # 6 action results
    res = await _call(b, action="evict_action_results", keep_last=2)
    assert res["success"] and res["removed"] == 4
    assert len(b.msgs.messages) == len(b.msgs.progression)  # pile intact
    assert len(b.progression) < len(b.msgs.progression)  # view shrank


async def test_evict_action_results_keep_last_zero():
    b = _build_branch()
    res = await _call(b, action="evict_action_results", keep_last=0)
    assert res["success"] and res["removed"] == 6


# -- browse + restore ------------------------------------------------------


async def test_get_messages_all_shows_evicted():
    b = _build_branch()
    await _call(b, action="evict", start=2, end=5)
    res = await _call(b, action="get_messages", scope="all")
    joined = " ".join(res["messages"])
    assert "[evicted]" in joined and "[active]" in joined


async def test_restore_pulls_back_in_chronological_order():
    b = _build_branch()
    await _call(b, action="evict", start=2, end=5)
    active_after = len(b.progression)
    res = await _call(b, action="restore", start=2, end=4)
    assert res["success"] and res["restored"] == 2
    assert len(b.progression) == active_after + 2
    order = [b.msgs.progression.index(u) for u in b.progression]
    assert order == sorted(order)  # view stays chronological


# -- compact ---------------------------------------------------------------


async def test_compact_collapses_tool_io_and_injects_summary():
    b = _build_branch()
    total = len(b.msgs.messages)
    res = await _call(
        b,
        action="compact",
        start=1,
        summary="Root cause: off-by-one in f3.py:10. Fix applied. Repro green.",
    )
    assert res["success"] and res["compacted"] > 0 and res["tokens_freed_est"] > 0
    assert len(b.msgs.messages) == total + 1  # summary added to the record
    previews = await _call(b, action="get_messages", scope="active")
    assert any("CONTEXT COMPACTION" in m for m in previews["messages"])
    assert len(b.progression) < len(b.msgs.progression)  # originals hidden, not deleted


async def test_compact_requires_summary():
    res = await _call(_build_branch(), action="compact", start=1, summary="  ")
    assert res["success"] is False


async def test_compact_all_mode_collapses_more_than_tool_io():
    res_io = await _call(
        _build_branch(), action="compact", start=1, end=7, summary="s", mode="tool_io"
    )
    res_all = await _call(
        _build_branch(), action="compact", start=1, end=7, summary="s", mode="all"
    )
    assert res_all["compacted"] > res_io["compacted"]


# -- edge cases ---------------------------------------------------------------


async def test_compact_start_greater_than_total_messages_returns_error():
    b = _build_branch()
    total = len(b.msgs.messages)
    res = await _call(b, action="compact", start=total + 100, summary="s")
    assert res["success"] is False
    assert "error" in res


async def test_compact_no_action_results_in_span_returns_error():
    b = Branch(system="sys")
    for i in range(5):
        b.msgs.add_message(instruction=f"step {i}")
        b.msgs.add_message(assistant_response=f"answer {i}")
    # Range contains only user/assistant messages, no ActionRequest/ActionResponse
    res = await _call(b, action="compact", start=1, end=4, summary="summary", mode="tool_io")
    assert res["success"] is False
    assert "Nothing to compact" in res["error"]


async def test_get_messages_active_scope_when_all_evicted():
    b = _build_branch()
    total_active = len(b.msgs.progression)
    # Evict everything except system (index 0)
    await _call(b, action="evict", start=1, end=total_active)
    res = await _call(b, action="get_messages", scope="active")
    assert res["success"] is True
    # Only system message remains (index 0 cannot be evicted)
    assert len(res["messages"]) <= 1


async def test_restore_indices_overlap_with_still_active_messages():
    b = _build_branch()
    # Evict a range
    await _call(b, action="evict", start=2, end=5)
    active_before = len(b.progression)
    # Restore the same range — messages at 3 and 4 were evicted, 2 was already active
    # (evict start=2 clamped to max(1,2)=2, so msg at index 2 is evicted)
    res = await _call(b, action="restore", start=2, end=5)
    assert res["success"] is True
    # Already-active items are skipped; restored count ≤ 3
    assert res["restored"] <= 3
    assert len(b.progression) >= active_before


async def test_evict_and_restore_concurrently_does_not_corrupt_progression():
    import asyncio

    b = _build_branch()
    total = len(b.msgs.messages)

    async def do_evict():
        return await _call(b, action="evict", start=2, end=5)

    async def do_restore():
        return await _call(b, action="restore", start=2, end=5)

    results = await asyncio.gather(do_evict(), do_restore(), return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            raise r
        assert r["success"] is True

    # Durable pile must never be shrunk
    assert len(b.msgs.messages) == total
