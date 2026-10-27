# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for lionagi.tools.context.ContextTool.

evict/compact curate only the active progression; the full message Pile is never pruned.
"""

import pytest

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


async def test_read_result_recovers_two_raw_mapping_bodies_and_json_list_after_roundtrip():
    from lionagi.protocols.messages import ActionResponse, Message

    first = 'Aé🦁 "alpha"\n' * 1700 + "A-END\n"
    second = '雪B "beta"\nnext 🧭\n' * 1400 + 'B-END "fin"\n'
    output = {
        "result": first,
        "notes": second,
        "items": ["é", 'quoted "item"', "line\nitem", {"z": 2, "a": "🦁"}],
        "success": True,
        "error": None,
    }
    original = ActionResponse(content={"function": "reader", "output": output})
    stored = Message.from_dict(original.to_dict())
    branch = _make_branch_with_messages()
    await branch.msgs._a_add_built_response(stored)
    before_ids = list(branch.progression)
    before_record = stored.to_dict()
    index = branch.msgs.progression.index(stored.id)
    for start, stop, expected in ((809, 20191, first[768:-256]), (21256, 44044, second[768:-256])):
        recovered = ""
        offset = start
        while offset < stop:
            page = await _call(
                branch,
                action="read_result",
                index=index,
                offset=offset,
                limit=min(16000, stop - offset),
            )
            assert page["success"] is True
            assert page["total"] == 44478
            recovered += page["text"]
            offset = page["next_offset"]
        assert recovered == expected
    page = await _call(branch, action="read_result", index=index, offset=809, limit=20000)
    assert len(page["text"]) == 16000
    assert page["text"] == first[768:16768]
    listed = await _call(branch, action="read_result", index=index, offset=44339, limit=52)
    assert listed["text"] == r'["é","quoted \"item\"","line\nitem",{"a":"🦁","z":2}]'
    assert list(branch.progression) == before_ids
    assert stored.to_dict() == before_record
    cloned = branch.clone()
    clone_page = await _call(cloned, action="read_result", index=index, offset=809, limit=16000)
    assert clone_page == page


async def test_read_result_string_offsets_are_characters_and_never_change_the_view():
    from lionagi.protocols.messages import ActionResponse

    branch = _make_branch_with_messages()
    response = ActionResponse(content={"function": "bash", "output": 'é🦁 "yes"\nnext'})
    await branch.msgs._a_add_built_response(response)
    before = list(branch.progression)
    index = branch.msgs.progression.index(response.id)
    result = await _call(branch, action="read_result", index=index, offset=1, limit=3)
    assert result == {"success": True, "text": '🦁 "', "total": 13, "next_offset": 4}
    assert list(branch.progression) == before


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


async def test_evict_is_non_destructive():
    b = _build_branch()
    total = len(b.msgs.messages)
    res = await _call(b, action="evict", start=1, end=4)
    assert res["success"] and res["removed"] == 3
    assert len(b.progression) == total - 3  # active view shrank
    assert len(b.msgs.messages) == total  # durable Pile untouched
    assert len(b.msgs.progression) == total  # full record untouched


async def test_a_message_added_after_the_view_exists_enters_it():
    b = _build_branch()
    res = await _call(b, action="evict", start=1, end=4)
    assert res["success"]
    later = b.msgs.add_message(instruction="a later question")
    answer = await b.msgs.a_add_message(assistant_response="a later answer")

    assert list(b.progression)[-2:] == [later.id, answer.id]
    assert len(b.progression) == len(b.msgs.progression) - 3  # the evicted three stay out


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


async def test_compact_all_mode_keeps_the_first_instruction_in_view():
    b = _build_branch()
    first_instruction = b.progression[1]
    res = await _call(b, action="compact", start=1, end=7, summary="s", mode="all")
    assert res["success"] is True
    assert b.progression[1] == first_instruction
    assert "CONTEXT COMPACTION" in b.msgs.messages[b.progression[2]].content.assistant_response


async def test_compact_summary_takes_the_place_of_the_first_collapsed_message():
    b = _build_branch()
    first_tool_request = b.progression[3]  # system, instruction, reasoning, request
    res = await _call(b, action="compact", start=1, end=5, summary="s")
    assert res["success"] is True and res["compacted"] == 2
    assert b.progression[3] != first_tool_request
    assert "CONTEXT COMPACTION" in b.msgs.messages[b.progression[3]].content.assistant_response


async def test_the_compaction_summary_is_in_the_view_once():
    b = _build_branch()
    res = await _call(b, action="compact", start=1, end=5, summary="s")
    assert res["success"] is True

    notes = [
        u
        for u in b.progression
        if "CONTEXT COMPACTION"
        in str(getattr(b.msgs.messages[u].content, "assistant_response", ""))
    ]
    assert notes == [b.progression[3]]


async def test_a_replacement_system_message_heads_the_view_in_place_of_the_old_one():
    b = _build_branch()
    await _call(b, action="evict", start=2, end=3)
    old = b.msgs.system.id
    b.msgs.add_message(system="You are a reviewing agent.")

    assert b.progression[0] == b.msgs.system.id
    assert old not in b.progression
    res = await _call(b, action="compact", start=1, end=5, summary="s")
    assert res["success"] is True, res


async def test_a_system_message_set_directly_heads_the_view_and_restore_still_works():
    b = _build_branch()
    await _call(b, action="evict", start=1, end=4)
    old = b.msgs.system.id
    b.msgs.set_system(b.msgs.create_system(system="You are a reviewing agent."))

    assert b.progression[0] == b.msgs.system.id
    assert old not in b.progression
    assert all(uid in b.msgs.messages for uid in b.progression)
    res = await _call(b, action="restore", start=1, end=4)
    assert res["success"] is True, res
    res = await _call(b, action="compact", start=1, end=5, summary="s")
    assert res["success"] is True, res


async def test_clearing_the_messages_leaves_the_system_message_as_the_view():
    b = _build_branch()
    await _call(b, action="evict", start=2, end=3)
    b.msgs.clear_messages()

    assert list(b.progression) == [b.msgs.system.id]
    msg = b.msgs.add_message(instruction="start over")
    assert list(b.progression) == [b.msgs.system.id, msg.id]


async def test_a_restored_view_keeps_only_messages_the_stored_record_holds():
    b = _build_branch()
    await _call(b, action="evict", start=2, end=5)
    view = list(b.progression)
    stray = b.msgs.create_message(instruction="never added")
    b.metadata["current_progression"].append(stray.id)

    restored = Branch.from_dict(b.to_dict())
    assert list(restored.progression) == view


async def test_a_restored_branch_keeps_its_view_and_admits_new_messages():
    b = _build_branch()
    await _call(b, action="evict", start=2, end=5)
    view = list(b.progression)

    restored = Branch.from_dict(b.to_dict())
    assert list(restored.progression) == view
    msg = restored.msgs.add_message(instruction="next step")
    assert list(restored.progression) == [*view, msg.id]
    res = await _call(restored, action="compact", start=1, end=6, summary="s")
    assert res["success"] is True, res


async def test_compact_keeps_working_when_the_view_already_has_two_instructions_in_a_row(
    monkeypatch,
):
    b = Branch(system="sys")
    b.msgs.add_message(instruction="first")
    b.msgs.add_message(assistant_response="first answer")
    b.msgs.add_message(instruction="second")
    b.msgs.add_message(assistant_response="second answer")
    await _call(b, action="evict", start=2, end=3)  # view: sys, first, second, answer
    record = len(b.msgs.messages)
    view = list(b.progression)
    called = {"chat": False}

    async def fake_chat(self, *, instruction=None, **kw):
        called["chat"] = True
        return "unused"

    monkeypatch.setattr(Branch, "chat", fake_chat)
    res = await _call(b, action="compact", start=3, end=4, mode="all", auto=True)

    assert res["success"] is True
    assert called["chat"] is True
    assert len(b.msgs.messages) == record + 1
    assert list(b.progression) != view


async def test_compact_refuses_a_view_that_would_begin_with_the_summary():
    b = _build_branch()
    await _call(b, action="evict", start=1, end=3)  # drop the first instruction and reply
    record = len(b.msgs.messages)
    view = list(b.progression)
    res = await _call(b, action="compact", start=1, end=3, summary="s")
    assert res["success"] is False
    assert "begin with an assistant response" in res["error"]
    assert len(b.msgs.messages) == record
    assert list(b.progression) == view


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


async def _branch_with_bound_result(bound, output):
    from lionagi.protocols.messages import ActionResponse, Message
    from lionagi.service.imodel import iModel

    model = iModel(
        provider="anthropic",
        model="claude-haiku-5-5",
        api_key="test-not-a-key",
        max_tokens=64,
        tool_result_max_chars=bound,
    )
    branch = Branch(system="Keep the original tool result.", chat_model=model)
    branch.msgs.add_message(instruction="Read the result.")
    original = ActionResponse(content={"function": "reader", "output": output})
    stored = Message.from_dict(original.to_dict())
    await branch.msgs._a_add_built_response(stored)
    return branch, stored, branch.msgs.progression.index(stored.id)


@pytest.mark.parametrize(
    ("bound", "requested", "offset_kind"),
    [
        (1024, 20000, "start"),
        (4000, 20000, "unicode"),
        (20000, 20000, "unicode"),
        (4000, 51, "unicode"),
        (4000, 20000, "tail"),
        (4000, 51, "beyond"),
    ],
)
async def test_read_result_uses_applied_model_bound_not_remaining_character_count(
    bound, requested, offset_kind
):
    text = 'é🦁"A\n' * 5000
    offsets = {"start": 0, "unicode": 1, "tail": len(text) - 7, "beyond": len(text) + 9}
    offset = offsets[offset_kind]
    branch, stored, index = await _branch_with_bound_result(bound, text)
    view_before = list(branch.progression)
    full_before = list(branch.msgs.progression)
    record_before = stored.to_dict()
    applied = min(requested, bound, 16000)
    stop = min(len(text), offset + applied)

    result = await _call(branch, action="read_result", index=index, offset=offset, limit=requested)

    assert result["success"] is True
    assert result["text"] == text[offset:stop]
    assert result.get("limit") == applied
    assert result == {
        "success": True,
        "text": text[offset:stop],
        "total": len(text),
        "next_offset": stop if stop < len(text) else None,
        "limit": applied,
    }
    assert list(branch.progression) == view_before
    assert list(branch.msgs.progression) == full_before
    assert stored.to_dict() == record_before
    expected_ranges = [[offset, stop]] if stop > offset else []
    assert (
        branch.metadata.get("context_read_results", {}).get(str(stored.id), []) == expected_ranges
    )


def _mapping_recovery_fixture():
    first = 'Aé🦁 "one"\n' * 1100
    second = '雪B "two"\n' * 1300
    items = ["é", 'quoted "item"', "line\nitem", {"z": 2, "a": "🦁"}]
    json_items = r'["é","quoted \"item\"","line\nitem",{"a":"🦁","z":2}]'
    output = {"result": first, "notes": second, "items": items, "success": True, "error": None}
    first_label = f'[body key="result" kind=raw chars={len(first)}]\n'
    second_label = f'[body key="notes" kind=raw chars={len(second)}]\n'
    items_label = f'[body key="items" kind=json chars={len(json_items)}]\n'
    catalog = (
        first_label + first + "\n" + second_label + second + "\n" + items_label + json_items + "\n"
        '[body key="success" kind=json chars=4]\ntrue\n'
        '[body key="error" kind=json chars=4]\nnull\n'
    )
    return output, catalog, len(first_label), len(first_label) + len(first) + 1 + len(second_label)


@pytest.mark.parametrize("region", ["first", "second", "separator", "json"])
async def test_bound_recovery_uses_global_raw_mapping_offsets_after_roundtrip(region):
    output, catalog, first_start, second_start = _mapping_recovery_fixture()
    offset = {
        "first": first_start + 3,
        "second": second_start + 1,
        "separator": second_start - 9,
        "json": catalog.index('["é","quoted'),
    }[region]
    branch, stored, index = await _branch_with_bound_result(4000, output)
    record_before = stored.to_dict()
    view_before = list(branch.progression)
    result = await _call(branch, action="read_result", index=index, offset=offset, limit=20000)

    assert result["success"] is True
    assert result["text"] == catalog[offset : offset + 4000]
    assert result.get("limit") == 4000
    assert result["total"] == len(catalog)
    stop = min(len(catalog), offset + 4000)
    assert result["next_offset"] == (stop if stop < len(catalog) else None)
    assert stored.to_dict() == record_before
    assert list(branch.progression) == view_before


async def test_bound_recovery_pages_a_long_original_without_overlap_or_view_mutation():
    text = 'é🦁 "raw"\n' * 4000
    branch, stored, index = await _branch_with_bound_result(4000, text)
    record_before = stored.to_dict()
    view_before = list(branch.progression)
    offset = 17
    end = offset + 20003
    recovered = []
    for _ in range(6):
        requested = min(20000, end - offset)
        result = await _call(
            branch, action="read_result", index=index, offset=offset, limit=requested
        )
        applied = min(requested, 4000, 16000)
        assert result["success"] is True
        assert result["text"] == text[offset : offset + applied]
        assert result.get("limit") == applied
        assert result["next_offset"] == offset + applied
        recovered.append(result["text"])
        offset = result["next_offset"]
    assert offset == end
    assert "".join(recovered) == text[17:end]
    assert stored.to_dict() == record_before
    assert list(branch.progression) == view_before


async def test_unset_result_bound_keeps_exact_old_recovery_keys_and_cap():
    text = 'é🦁"\n' * 5000
    branch, stored, index = await _branch_with_bound_result(None, text)
    record_before = stored.to_dict()
    view_before = list(branch.progression)
    result = await _call(branch, action="read_result", index=index, offset=1, limit=20000)
    assert result == {
        "success": True,
        "text": text[1:16001],
        "total": len(text),
        "next_offset": 16001,
    }
    assert stored.to_dict() == record_before
    assert list(branch.progression) == view_before


async def test_bound_context_summary_projects_before_assembling_source(monkeypatch):
    from lionagi.protocols.messages import ActionResponse
    from lionagi.service.imodel import iModel

    model = iModel(
        provider="anthropic",
        model="claude-haiku-5-5",
        api_key="test-not-a-key",
        max_tokens=64,
        tool_result_max_chars=1024,
    )
    branch = Branch(system="Keep evidence.", chat_model=model, parse_model=model)
    await branch.msgs.a_add_message(instruction="first task")
    output = "head " * 300 + "HIDDEN_SUMMARY_MIDDLE" + "tail " * 20000
    original = ActionResponse(content={"function": "reader", "output": output})
    await branch.msgs._a_add_built_response(original)
    before = original.to_dict()
    prompts = []

    async def fake_chat(self, *, instruction=None, **kwargs):
        prompts.append(instruction)
        return "Summary of the projected evidence."

    monkeypatch.setattr(Branch, "chat", fake_chat)
    try:
        result = await _call(branch, action="compact", start=1, mode="all", auto=True)
        assert result["success"] is True and result["compacted"] == 1
        assert len(prompts) == 1
        assert "read_result" in prompts[0]
        assert "HIDDEN_SUMMARY_MIDDLE" not in prompts[0]
        assert original.to_dict() == before
    finally:
        await branch.mdls.shutdown()


async def test_bound_read_result_reports_applied_limit_without_changing_original():
    from lionagi.protocols.messages import ActionResponse
    from lionagi.service.imodel import iModel
    from lionagi.tools.context.context import _result_text

    model = iModel(
        provider="anthropic",
        model="claude-haiku-5-5",
        api_key="test-not-a-key",
        max_tokens=64,
        tool_result_max_chars=4000,
    )
    branch = Branch(system="Keep evidence.", chat_model=model, parse_model=model)
    await branch.msgs.a_add_message(instruction="first task")
    original = ActionResponse(content={"function": "reader", "output": "é🦁 " * 10000})
    await branch.msgs._a_add_built_response(original)
    text, _bodies = _result_text(original.content.output)
    index = branch.msgs.progression.index(original.id)
    before = original.to_dict()
    progression = list(branch.progression)
    try:
        page = await _call(branch, action="read_result", index=index, offset=37, limit=20000)
        assert page == {
            "success": True,
            "text": text[37:4037],
            "total": len(text),
            "next_offset": 4037,
            "limit": 4000,
        }
        final = await _call(
            branch, action="read_result", index=index, offset=len(text) - 2, limit=51
        )
        assert final["text"] == text[-2:] and final["limit"] == 51
        assert final["next_offset"] is None
        assert original.to_dict() == before
        assert list(branch.progression) == progression
    finally:
        await branch.mdls.shutdown()


async def test_restore_range_uses_one_record_map_without_index_scans(monkeypatch):
    import sys

    from lionagi.protocols.generic.pile import Pile
    from lionagi.protocols.generic.progression import Progression

    branch = _build_branch()
    assert (await _call(branch, action="evict", start=2, end=18))["success"]
    full = branch.msgs.progression
    expected = list(full)
    calls = []
    original = Progression.index
    original_iter = Progression.__iter__
    iterations = []

    def iterate(self):
        if self is full:
            iterations.append(sys._getframe(1).f_code)
        return original_iter(self)

    def index(self, item, start=0, end=None):
        if self is full:
            calls.append(item)
        return original(self, item, start, end)

    monkeypatch.setattr(Progression, "index", index)
    monkeypatch.setattr(Progression, "__iter__", iterate)
    result = await _call(branch, action="restore", start=2, end=18)
    assert result["success"] is True and result["restored"] == 16
    assert list(branch.progression) == expected
    assert calls == []
    # The clipped-result inventory takes one existing Pile snapshot. The
    # chronology lookup adds one shared record map for the whole restore.
    assert iterations.count(Pile.__iter__.__code__) == 1
    assert sum(code is not Pile.__iter__.__code__ for code in iterations) == 1


async def test_compact_uses_one_record_position_map_without_index_scans(monkeypatch):
    from lionagi.protocols.generic.progression import Progression

    branch = _build_branch()
    full = branch.msgs.progression
    before = list(full)
    calls = []
    original = Progression.index

    def index(self, item, start=0, end=None):
        if self is full:
            calls.append(item)
        return original(self, item, start, end)

    monkeypatch.setattr(Progression, "index", index)
    result = await _call(branch, action="compact", start=1, mode="tool_io", summary="kept")
    assert result["success"] is True and result["compacted"] == 12
    note = branch.msgs.messages[list(full)[-1]]
    assert note.metadata["context_first_index"] == 3
    assert list(full)[:-1] == before
    assert calls == []


async def test_restore_preserves_first_greater_placement_in_an_unsorted_view():
    from lionagi.protocols.generic.progression import Progression

    branch = _build_branch()
    full = list(branch.msgs.progression)
    branch.metadata["current_progression"] = Progression(
        order=[full[0], full[1], full[9], full[2], full[3], full[4]]
    )
    result = await _call(branch, action="restore", start=5, end=6)
    assert result["success"] is True and result["restored"] == 1
    assert list(branch.progression) == [
        full[0],
        full[1],
        full[5],
        full[9],
        full[2],
        full[3],
        full[4],
    ]


async def test_restore_record_position_map_keeps_first_duplicate_occurrence():
    from lionagi.protocols.generic.progression import Progression

    branch = _build_branch()
    full = branch.msgs.progression
    first, middle, later = full[2], full[3], full[4]
    full.append(first)
    branch.metadata["current_progression"] = Progression(order=[full[0], full[1], first, later])
    result = await _call(branch, action="restore", start=3, end=4)
    assert result["success"] is True and result["restored"] == 1
    assert list(branch.progression) == [full[0], full[1], first, middle, later]


async def test_noop_restore_keeps_a_curated_unavailable_tail_untouched():
    from lionagi.protocols.generic.progression import Progression

    branch = _build_branch()
    full = list(branch.msgs.progression)
    stray = branch.msgs.create_message(instruction="not in the stored record")
    view = [full[0], full[1], full[9], stray.id]
    branch.metadata["current_progression"] = Progression(order=view)
    result = await _call(branch, action="restore", start=1, end=2)
    assert result["success"] is True and result["restored"] == 0
    assert list(branch.progression) == view


async def test_restore_copy_lookup_reads_each_visible_metadata_only_linearly():
    from lionagi.protocols.generic.progression import Progression
    from lionagi.protocols.messages import ActionResponse

    branch = _make_branch_with_messages()
    prefix = list(branch.progression)
    originals = []
    for number in range(12):
        original = ActionResponse(
            content={"function": "reader", "output": f"original result {number}"}
        )
        await branch.msgs._a_add_built_response(original)
        originals.append(original)

    first_copies, second_copies = [], []
    for number, original in enumerate(originals):
        pair = []
        for suffix in ("first", "second"):
            copy = ActionResponse(
                content={"function": "reader", "output": f"copy {number} {suffix}"},
                metadata={"clipped_from": str(original.id)},
            )
            await branch.msgs._a_add_built_response(copy)
            pair.append(copy)
        first_copies.append(pair[0])
        second_copies.append(pair[1])

    full = branch.msgs.progression
    record_before = list(full)
    start = full.index(originals[0].id)
    end = full.index(originals[-1].id) + 1
    assert record_before[start:end] == [message.id for message in originals]
    outputs_before = [message.content.output for message in originals]
    copies = [*reversed(first_copies), *second_copies]
    view = [*prefix, *(message.id for message in copies)]
    branch.metadata["current_progression"] = Progression(order=view)
    metadata_reads = []
    observed_metadata = set()

    class CountedMetadata(dict):
        def get(self, key, default=None):
            if key == "clipped_from":
                metadata_reads.append(id(self))
            return dict.get(self, key, default)

    for uid in view:
        message = branch.msgs.messages[uid]
        wrapped = CountedMetadata(message.metadata)
        message.metadata = wrapped
        assert message.metadata is wrapped
        observed_metadata.add(id(wrapped))

    result = await _call(branch, action="restore", start=start, end=end)
    reads_during_restore = list(metadata_reads)
    assert result == {
        "success": True,
        "restored": len(originals),
        "active": len(prefix) + len(originals),
        "total": len(record_before),
    }
    assert list(branch.progression) == [
        *prefix,
        *(message.id for message in reversed(originals)),
    ]
    assert list(full) == record_before
    assert [message.content.output for message in originals] == outputs_before
    assert all(message.id in branch.msgs.messages for message in copies)
    assert all(message.id not in branch.progression for message in copies)
    assert set(reads_during_restore) == observed_metadata
    assert len(reads_during_restore) <= 3 * len(view)
