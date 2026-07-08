# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.tools.context.ContextTool.

evict/compact curate only the active progression; the full message Pile is never pruned.
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


# -- compact auto=True (model-generated summary) ---------------------------


async def test_compact_auto_generates_summary_via_model_call(monkeypatch):
    b = _build_branch()
    called: dict = {}

    async def fake_chat(self, *, instruction=None, **kw):
        called["prompt"] = instruction
        return "Root cause: off-by-one in f3.py. Fix applied. Verified green."

    monkeypatch.setattr(Branch, "chat", fake_chat)
    res = await _call(b, action="compact", start=1, auto=True)
    assert res["success"] is True and res["compacted"] > 0
    assert "prompt" in called

    previews = await _call(b, action="get_messages", scope="active")
    assert any("Root cause: off-by-one" in m for m in previews["messages"])


async def test_compact_auto_failure_returns_error_never_raises(monkeypatch):
    async def failing_chat(self, *, instruction=None, **kw):
        raise RuntimeError("model unavailable")

    b = _build_branch()
    monkeypatch.setattr(Branch, "chat", failing_chat)
    res = await _call(b, action="compact", start=1, auto=True)
    assert res["success"] is False
    assert "summary" in res["error"].lower()


async def test_compact_auto_model_returns_blank_text_fails_gracefully(monkeypatch):
    async def blank_chat(self, *, instruction=None, **kw):
        return "   "

    b = _build_branch()
    monkeypatch.setattr(Branch, "chat", blank_chat)
    res = await _call(b, action="compact", start=1, auto=True)
    assert res["success"] is False


async def test_compact_explicit_summary_wins_even_with_auto_true(monkeypatch):
    called = {"used": False}

    async def fake_chat(self, *, instruction=None, **kw):
        called["used"] = True
        return "should not be used"

    b = _build_branch()
    monkeypatch.setattr(Branch, "chat", fake_chat)
    res = await _call(b, action="compact", start=1, summary="Explicit summary here.", auto=True)
    assert res["success"] is True
    assert called["used"] is False


async def test_compact_auto_false_without_summary_still_requires_summary():
    res = await _call(_build_branch(), action="compact", start=1, auto=False)
    assert res["success"] is False
    assert "summary" in res["error"].lower()
