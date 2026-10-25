# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Bounded tool results on the real API ReAct and LNDL request paths."""

from __future__ import annotations

import copy
import json
import re

import pytest
import yaml

from lionagi.agent.context_fold import ContextFold, estimate_next_request_tokens
from lionagi.cli.agent import _api_loop_pass
from lionagi.operations.chat._prepare import _prepare_run_kwargs
from lionagi.operations.types import ChatParam
from lionagi.protocols.messages import ActionResponse, Instruction
from lionagi.providers.anthropic.messages import (
    AnthropicMessagesEndpoint,
    CreateMessageRequest,
)
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch
from lionagi.tools.coding import CodingToolkit
from lionagi.tools.context.context import _result_text
from tests.cli.test_agent_api_provider import (
    _answer,
    _last_round,
    _last_user_text,
    _lndl_answer,
    _reply,
    _text,
)
from tests.cli.test_api_leg_unset_payload_identity import (
    _ANNOTATION,
    _FixedNudge,
    _json_bytes,
    _payload_text,
)

_TEXT = ("head é🦁 " * 180 + "HIDDEN_SUMMARY_SOURCE " + "evidence 雪 0123456789 " * 6000)[:100045]
_MARKER = re.compile(
    r"\[\.\.\. (?P<count>[\d,]+) chars omitted from \[(?P<start>\d+), (?P<end>\d+)\); "
    r'recover with context\(action="read_result", index=(?P<index>\d+), '
    r"offset=(?P<offset>\d+), limit=(?P<limit>\d+)\); continue at next_offset with "
    r"limit=min\((?P<page_cap>\d+), (?P<until>\d+)-next_offset\), "
    r"stopping at (?P<stop>\d+)\.\]"
)


def _walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _sent_results(payload, function):
    found = []
    for message in payload["messages"]:
        content = message["content"]
        blocks = (
            [content]
            if isinstance(content, str)
            else [part["text"] for part in content if isinstance(part, dict) and "text" in part]
        )
        for text in blocks:
            try:
                document = yaml.safe_load(text)
            except yaml.YAMLError:
                continue  # Assistant prose need not be a YAML document.
            found.extend(
                item
                for item in _walk(document)
                if item.get("function") == function and "output" in item
            )
    return found


def _check_output(original, actual, bound, index):
    """Check actual decoded request values against canonical stored-original spans."""
    source, bodies = _result_text(original)
    if isinstance(original, dict):
        assert isinstance(actual, dict) and list(actual) == list(original)
    markers = {}
    body_keys = {body.key for body in bodies}
    if isinstance(original, dict):
        for key in original.keys() - body_keys:
            assert actual[key] == original[key] and type(actual[key]) is type(original[key])
    for body in bodies:
        before = original[body.key] if isinstance(original, dict) else original
        shown = actual[body.key] if isinstance(actual, dict) else actual
        length = body.end - body.start
        if body.protected or length <= bound:
            # Message content tracks mutable values with private dict/list subclasses.
            # The provider payload preserves their serialized container kind and value.
            kind = (
                dict
                if isinstance(before, dict)
                else list
                if isinstance(before, list)
                else type(before)
            )
            assert shown == before and type(shown) is kind
            continue
        assert isinstance(shown, str) and len(shown) <= bound
        matches = list(_MARKER.finditer(shown))
        assert len(matches) == shown.count('context(action="read_result"') == 1
        match = matches[0]
        marker = {key: int(value.replace(",", "")) for key, value in match.groupdict().items()}
        start, stop = marker["start"], marker["end"]
        assert marker["index"] == index and marker["offset"] == start
        assert marker["count"] == stop - start > 0
        assert marker["until"] == marker["stop"] == stop
        assert marker["page_cap"] == min(bound, 16000)
        assert 0 < marker["limit"] <= min(bound, 16000, stop - start)
        head, tail = start - body.start, body.end - stop
        assert head > 0 and tail > 0 and head == 3 * (head + tail) // 4
        prefix = "[JSON preview]\n" if body.is_json else ""
        expected = (
            prefix + source[body.start : start] + "\n" + match[0] + "\n" + source[stop : body.end]
        )
        assert shown == expected
        markers[body.key] = marker
    return source, markers


def _compiled(branch):
    latest = next(
        branch.msgs.messages[uid]
        for uid in reversed(list(branch.progression))
        if isinstance(branch.msgs.messages[uid], Instruction)
    )
    _ins, request = _prepare_run_kwargs(
        branch, None, ChatParam.from_branch(branch), ins=latest, imodel=branch.chat_model
    )
    return branch.chat_model.endpoint.create_payload(request=request)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("loop", ["react", "lndl"])
@pytest.mark.parametrize("bound", [1024, 4000, 20000])
async def test_api_loop_bounds_originals_and_delivers_exact_recovery_pages(
    tmp_path, monkeypatch, loop, bound
):
    (tmp_path / "notes.txt").write_text(_TEXT, encoding="utf-8")
    structured = {
        "items": [{"z": 2, "a": "é🦁"}] * 6000,
        "nested": {"z": "雪 " * 20000, "a": list(range(4000))},
        "short": {"z": 2, "a": 1},
        "success": "protected success " * 1500,
        "error": "protected error " * 1500,
        "unsupported": {"JSON-refused set"},
    }

    async def fixture_text() -> str:
        return _TEXT

    async def fixture_structured() -> dict:
        return structured

    model = iModel(
        provider="anthropic",
        model="claude-haiku-5-5",
        api_key="test-not-a-key",
        max_tokens=64,
        tool_result_max_chars=bound,
    )
    model.endpoint.config.request_options = CreateMessageRequest
    assert model.is_cli is False
    branch = Branch(
        system="Keep the stored evidence and answer concisely.", chat_model=model, parse_model=model
    )
    nudge = _FixedNudge()
    tools = CodingToolkit(
        workspace_root=tmp_path,
        tools=("reader", "search", "context"),
        notify=True,
        nudge_engine=nudge,
    ).bind(branch)
    branch.register_tools([*tools, fixture_text, fixture_structured])
    assert next(tool for tool in tools if tool.function == "context").postprocessor is not None
    phase, replies, sent = "", [], []

    async def call(self, request, *args, **kwargs):
        assert replies, f"unexpected provider request during {phase}"
        sent.append((phase, json.loads(_json_bytes(request))))
        return replies.pop(0)

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", call)

    async def main(label, *, initial=False):
        nonlocal phase
        phase = label
        answer = f"{label} complete"
        if initial:
            actions = [
                {"function": "reader", "arguments": {"action": "read", "path": "notes.txt"}},
                {"function": "fixture_text", "arguments": {}},
                {"function": "fixture_structured", "arguments": {}},
            ]
            if loop == "react":
                replies.append(
                    _reply(
                        {
                            "analysis": "inspect all evidence",
                            "action_requests": actions,
                            "action_strategy": "sequential",
                            "extension_needed": True,
                            "reason": {"title": "inspect", "content": "read the stored output"},
                        }
                    )
                )
            else:
                replies.append(
                    _text(
                        '```lndl\n<lact r>reader(action="read", path="notes.txt")</lact>\n'
                        "<lact t>fixture_text()</lact>\n"
                        "<lact s>fixture_structured()</lact>\n```"
                    )
                )
        replies.extend(
            [_last_round(), _answer(answer)] if loop == "react" else [_lndl_answer(answer)]
        )
        start = len(sent)
        result = await _api_loop_pass(branch, "Inspect the evidence and answer.", loop, 3)
        assert getattr(result, "answer", result) == answer and not replies
        requests = [payload for actual_phase, payload in sent[start:] if actual_phase == label]
        assert requests
        return requests

    async def context(arguments):
        before = len(nudge.calls)
        results = await branch.act(
            {"function": "context", "arguments": arguments},
            strategy="sequential",
            suppress_errors=False,
        )
        assert len(results) == 1 and len(nudge.calls) == before + 1
        assert nudge.calls[-1] == ("context", "", True)
        output = results[0].output
        assert output["system"] == _ANNOTATION
        return output

    try:
        initial = await main("initial", initial=True)
        originals = {
            message.content.function: message
            for message in branch.msgs.messages
            if isinstance(message, ActionResponse)
            and message.content.function in ("reader", "fixture_text", "fixture_structured")
        }
        assert set(originals) == {"reader", "fixture_text", "fixture_structured"}
        assert originals["fixture_text"].content.output == _TEXT
        assert originals["fixture_structured"].content.output == structured
        snapshots = {key: copy.deepcopy(message.to_dict()) for key, message in originals.items()}
        view, record_size = list(branch.progression), len(branch.msgs.messages)
        model.tool_result_max_chars = None
        try:
            unbounded = _compiled(branch)
        finally:
            model.tool_result_max_chars = bound
        bounded = _compiled(branch)
        assert list(branch.progression) == view and len(branch.msgs.messages) == record_size
        assert all(message.to_dict() == snapshots[key] for key, message in originals.items())
        assert not any(message.metadata.get("clipped_from") for message in branch.msgs.messages)
        projections = {}
        for function, original in originals.items():
            actual = [item for payload in initial for item in _sent_results(payload, function)]
            assert actual, f"the actual {loop} request carried no {function} result"
            index = branch.msgs.progression.index(original.id)
            for item in actual:
                _check_output(original.content.output, item["output"], bound, index)
            projections[function] = copy.deepcopy(actual[-1]["output"])

        assert len(_json_bytes(unbounded)) > len(_json_bytes(bounded))

        reader = originals["reader"]
        index = branch.msgs.progression.index(reader.id)
        source, markers = _check_output(reader.content.output, projections["reader"], bound, index)
        marker = markers["content"]
        offset, end = marker["start"], marker["end"]
        assert end - offset > 16000
        oversized = bound + 20000
        probe = await context(
            {"action": "read_result", "index": index, "offset": offset, "limit": oversized}
        )
        assert probe["success"] is True and probe["limit"] == min(bound, 16000)
        assert probe["text"] == source[offset : offset + min(bound, 16000)]
        probe_record = (offset, oversized, copy.deepcopy(probe))
        recovered, pages = "", []
        while offset < end:
            requested = (
                marker["limit"]
                if not pages
                else min(17 if len(pages) == 1 else marker["page_cap"], end - offset)
            )
            page = await context(
                {"action": "read_result", "index": index, "offset": offset, "limit": requested}
            )
            applied = min(requested, bound, 16000)
            assert page["success"] is True and page["limit"] == applied
            assert page["text"] == source[offset : min(offset + applied, len(source))]
            assert len(page["text"]) == applied and page["total"] == len(source)
            assert page["next_offset"] == offset + applied
            assert 'context(action="read_result"' not in page["text"]
            pages.append((offset, requested, copy.deepcopy(page)))
            recovered += page["text"]
            offset = page["next_offset"]
        assert offset == end and recovered == source[marker["start"] : end]
        assert len(recovered) > bound
        after_pages = await main("after_pages")
        for payload in after_pages:
            for function, original in originals.items():
                actual = _sent_results(payload, function)
                assert actual and all(item["output"] == projections[function] for item in actual)
            actual_pages = _sent_results(payload, "context")
            for page_offset, requested, page in [probe_record, *pages]:
                matching = [
                    item["output"]
                    for item in actual_pages
                    if item["arguments"]
                    == {
                        "action": "read_result",
                        "index": index,
                        "offset": page_offset,
                        "limit": requested,
                    }
                ]
                assert matching and all(shown["text"] == page["text"] for shown in matching)
                assert all(shown["limit"] == page["limit"] for shown in matching)
                assert all(shown["system"] == _ANNOTATION for shown in matching)

        # A real admission check distinguishes the projected request from its whole originals.
        bounded_count = estimate_next_request_tokens(branch)
        model.tool_result_max_chars = None
        try:
            whole_count = estimate_next_request_tokens(branch)
        finally:
            model.tool_result_max_chars = bound
        cap = (bounded_count + whole_count) // 2
        assert bounded_count < cap < whole_count
        guard = ContextFold(branch, cap=cap)
        active_index = list(branch.progression).index(reader.id)
        evicted = await context({"action": "evict", "start": active_index, "end": active_index + 1})
        assert evicted["success"] is True
        assert reader.id not in branch.progression
        restored = await context({"action": "restore", "start": index})
        assert restored["success"] is True and restored["restored"] == 1
        assert reader.id in branch.progression and guard.estimate() < cap
        for payload in await main("after_restore"):
            actual = _sent_results(payload, "reader")
            assert actual and all(item["output"] == projections["reader"] for item in actual)
        assert all(message.to_dict() == snapshots[key] for key, message in originals.items())

        if bound == 1024 and loop == "react":
            phase = "context_summary"
            replies.append(_text("Summary of projected evidence."))
            before = len(sent)
            reader_slot = list(branch.progression).index(reader.id)
            compacted = await context(
                {
                    "action": "compact",
                    "start": reader_slot,
                    "end": reader_slot + 1,
                    "mode": "all",
                    "auto": True,
                }
            )
            assert compacted["success"] is True and len(sent) == before + 1 and not replies
            # Recovery pages may legitimately expose the hidden range in older history.
            # Inspect only this summary request's new source instruction.
            summary_source = _last_user_text(sent[-1][1])
            assert "HIDDEN_SUMMARY_SOURCE" not in summary_source
            assert "read_result" in summary_source
            assert any(
                "Summary of projected evidence." in _payload_text(payload)
                for payload in await main("after_summary")
            )
    finally:
        await branch.mdls.shutdown()
