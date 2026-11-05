# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Bounded folds count retained source and complete projected requests."""

import math
import re

import pytest

from lionagi.agent.context_fold import ContextFold, estimate_next_request_tokens
from lionagi.protocols.messages._result_projection import _projection_session
from tests.cli.test_agent_api_context_fold import (
    _active_results,
    _clip_fixture,
    _compiled_view_payload,
)


@pytest.mark.asyncio
async def test_small_real_deficit_keeps_source_near_current_not_floor():
    output = ("evidence é🦁 0123456789\n" * 6000)[:100045]
    assert len(output) == 100045
    branch, requests, originals = await _clip_fixture([output], cap=1000000)
    branch.chat_model.tool_result_max_chars = 4000
    original = originals[0]
    original_before = original.to_dict()
    request_before = requests[0].to_dict()
    projection = _projection_session(branch, branch.chat_model)
    initial = projection.project(original).bodies[0]
    current = initial.retained
    floor = min(1024, initial.k_n)
    before = estimate_next_request_tokens(branch)  # actual default compiler/counter
    deficit = 8
    cap = 2 * before
    target = (before - deficit) / cap
    fold = ContextFold(branch, cap=cap, target=target)  # no injected estimator
    assert fold._estimate is estimate_next_request_tokens
    assert floor + 512 < current < 4000
    fired = []
    branch.on_message_added.append(fired.append)
    assert await fold._clip_once("small-real-deficit")
    (accepted,) = _active_results(branch)
    actual = accepted.metadata["context_clip_kept"][0]
    # Seeding from the original length can eventually reach the target, but
    # overcuts. These source-preservation assertions distinguish that mutation.
    assert actual > floor + 0.8 * (current - floor)
    assert 0 < current - actual <= max(128, math.ceil(0.05 * current))
    marker = re.search(r"omitted from \[(\d+), (\d+)\)", accepted.content.output)
    assert marker is not None
    start, stop = map(int, marker.groups())
    assert actual == start + len(output) - stop
    assert accepted.content.output.startswith(output[:start])
    assert accepted.content.output.endswith(output[stop:])
    assert len(accepted.content.output) <= 4000
    assert accepted.content.output.count('context(action="read_result"') == 1
    assert accepted.content.action_request_id is None
    record = fold.records[-1]
    assert record["estimate_before"] == before
    assert record["estimate_after"] == fold.estimate() < before
    assert record["target_reached"] is True
    assert record["clipped"] == [
        {
            "index": branch.msgs.progression.index(original.id),
            "chars": len(output),
            "kept": actual,
        }
    ]
    assert original.to_dict() == original_before
    assert requests[0].to_dict() == request_before
    assert [message for message in fired if message.metadata.get("clipped_from")] == [accepted]


@pytest.mark.asyncio
async def test_bound_already_under_target_performs_no_deficit_clip():
    output = ("evidence é🦁 0123456789\n" * 6000)[:100045]
    branch, _requests, originals = await _clip_fixture([output], cap=1000000)
    branch.chat_model.tool_result_max_chars = 4000
    before = estimate_next_request_tokens(branch)
    fold = ContextFold(branch, cap=2 * before, target=0.6)
    ids = list(branch.msgs.progression)
    active = list(branch.progression)
    payload = _compiled_view_payload(branch)
    assert before < fold.target * fold.cap
    assert await fold.fold(reason="already-under-target") is False
    assert fold.records == []
    assert list(branch.msgs.progression) == ids
    assert list(branch.progression) == active
    assert _active_results(branch) == originals
    assert _compiled_view_payload(branch) == payload


@pytest.mark.asyncio
async def test_n1024_effective_floor_has_zero_extra_clip_capacity():
    branch, _requests, originals = await _clip_fixture(["x" * 100045], cap=1000000)
    branch.chat_model.tool_result_max_chars = 1024
    projection = _projection_session(branch, branch.chat_model)
    metric = projection.project(originals[0]).bodies[0]
    assert 2 <= metric.retained == metric.k_n < 1024
    before_ids = list(branch.msgs.progression)
    fold = ContextFold(branch, cap=1)  # deficit cannot create hidden-source capacity
    assert await fold._clip_once("effective-floor") is False
    assert list(branch.msgs.progression) == before_ids
    assert _active_results(branch) == originals
    record = fold.records[-1]
    assert record["clipped"] == [] and record["clipped_total"] == 0
    assert record["target_reached"] is False


@pytest.mark.asyncio
async def test_fold_summary_sends_projected_tool_source_through_real_endpoint(monkeypatch):
    from copy import deepcopy

    import yaml

    from lionhub.providers.anthropic.messages import AnthropicMessagesEndpoint
    from tests.cli.test_agent_api_provider import _last_user_text, _text

    hidden = "HIDDEN-MIDDLE-ONLY-7f1f0c"
    head, tail = "HEAD-é🦁|", "|TAIL-é🦁"
    output = (
        head
        + "a" * 49000
        + hidden
        + "b" * (100045 - len(head) - 49000 - len(hidden) - len(tail))
        + tail
    )
    branch, requests, originals = await _clip_fixture([output], cap=1000000)
    branch.chat_model.tool_result_max_chars = 4000
    original = originals[0]
    # The result-only span starts at zero; recovery keeps the larger full-record index.
    index = branch.msgs.progression.index(original.id)
    assert index > 0
    quote = "[NONRESULT-QUOTE]" + "quoted prose " * 350 + "[END-NONRESULT-QUOTE]"
    assert len(quote) > 4000
    prose = await branch.msgs.a_add_message(assistant_response=quote)
    fold = ContextFold(branch, cap=8000)
    original_before, request_before = original.to_dict(), requests[0].to_dict()
    records_before = [message.to_dict() for message in branch.msgs.messages]
    active_before = list(branch.progression)
    delivered_before = list(branch.metadata["context_delivered"])
    preview = _projection_session(branch, branch.chat_model).project(original).output
    marker = re.search(r"\n\[\.\.\. .*?stopping at \d+\.\]\n", preview)
    assert marker is not None and len(preview) <= 4000
    assert hidden not in preview
    sent = []

    async def scripted(self, request, *args, **kwargs):
        sent.append(deepcopy(request))
        return _text("bounded evidence summary")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", scripted)
    assert await fold._model_summary([original, prose]) == "bounded evidence summary"
    assert len(sent) == 1
    source = _last_user_text(sent[0])
    assert source.startswith("Write a concise, factual summary of the agent work below")
    # Decode only the nested result document for semantic source assertions.
    # The captured endpoint payload remains unchanged.
    result_document = yaml.safe_load(source.split("\n\n", 1)[1].split("\n---\n", 1)[0])
    actual_output = result_document["Output"]
    assert actual_output == preview
    assert head in actual_output and tail in actual_output
    assert hidden not in source
    assert marker.group(0).strip() in actual_output
    assert f'context(action="read_result", index={index}, ' in actual_output
    assert "continue at next_offset" in actual_output
    assert actual_output.count('context(action="read_result"') == 1
    assert quote in source  # unrelated assistant prose is neither scanned nor shortened
    assert original.to_dict() == original_before
    assert requests[0].to_dict() == request_before
    assert [message.to_dict() for message in branch.msgs.messages] == records_before
    assert list(branch.progression) == active_before
    assert branch.metadata["context_delivered"] == delivered_before


@pytest.mark.asyncio
async def test_candidate_count_uses_authoritative_original_without_storing_trials(monkeypatch):
    from copy import deepcopy

    from lionagi.protocols.messages import ActionResponse, Instruction
    from lionagi.protocols.messages._result_projection import _ProjectionError
    from lionagi.service.connections.api_calling import estimate_payload_input_tokens
    from tests.cli.test_api_leg_unset_payload_identity import _payload_text

    hidden = "ORIGINAL-HIDDEN-MIDDLE-2188"
    output = "source-head|" + "a" * 50000 + hidden + "b" * 50000 + "|source-tail"
    branch, requests, originals = await _clip_fixture(
        ["OMITTED-OTHER-RESULT-9f14", output], cap=1000000
    )
    branch.chat_model.tool_result_max_chars = 4000
    original = originals[1]
    fold = ContextFold(branch, cap=1000000)  # real default estimator, no injected count
    instruction = next(
        branch.msgs.messages[uid]
        for uid in branch.progression
        if isinstance(branch.msgs.messages[uid], Instruction)
    )
    index = branch.msgs.progression.index(original.id)
    trial = ActionResponse(
        content={
            "function": original.content.function,
            "arguments": original.content.arguments,
            "output": "TRIAL-BODY-MUST-NOT-BE-TRUSTED",
            "error": original.content.error,
        },
        sender=original.sender,
        recipient=original.recipient,
        metadata={"clipped_from": str(original.id), "context_clip_kept": [1500]},
    )
    view = [instruction, requests[1], trial]
    assert original not in view and index > len(view) - 1
    records_before = [message.to_dict() for message in branch.msgs.messages]
    active_before = list(branch.progression)
    metadata_before = deepcopy(branch.metadata)
    trial_before = trial.to_dict()
    fired = []
    branch.on_message_added.append(fired.append)
    projection = _projection_session(branch, branch.chat_model)
    expected = projection.project(trial).output
    interval = re.search(r"omitted from \[(\d+), (\d+)\)", expected)
    assert interval is not None
    start, stop = map(int, interval.groups())
    payloads = []
    endpoint_type = type(branch.chat_model.endpoint)
    real_payload = endpoint_type.create_payload

    def captured(self, *args, **kwargs):
        payload, headers = real_payload(self, *args, **kwargs)
        payloads.append(deepcopy(payload))
        return payload, headers

    monkeypatch.setattr(endpoint_type, "create_payload", captured)
    count = fold._estimate_view(view)
    assert len(payloads) == 1
    assert count == estimate_payload_input_tokens(payloads[0])
    source = _payload_text(payloads[0])
    assert f'context(action="read_result", index={index}, offset={start}, ' in source
    assert f"omitted from [{start}, {stop})" in source
    assert f"stopping at {stop}.]" in source
    assert source.count('context(action="read_result"') == 1
    assert "source-head|" in source and "|source-tail" in source
    assert hidden not in source
    assert "TRIAL-BODY-MUST-NOT-BE-TRUSTED" not in source
    assert "OMITTED-OTHER-RESULT-9f14" not in source
    assert trial.id not in branch.msgs.messages

    invalid = ActionResponse(
        content={"function": original.content.function, "arguments": {}, "output": "trial"},
        metadata={"clipped_from": str(original.id), "context_clip_kept": [1]},
    )
    with pytest.raises(_ProjectionError):
        fold._estimate_view([instruction, requests[1], invalid])
    assert fold._estimate_view(view) == count
    assert trial.to_dict() == trial_before
    assert [message.to_dict() for message in branch.msgs.messages] == records_before
    assert list(branch.progression) == active_before
    assert branch.metadata == metadata_before
    assert fired == []
    assert invalid.id not in branch.msgs.messages and trial.id not in branch.msgs.messages


@pytest.mark.asyncio
async def test_mapping_clip_preserves_hidden_read_body_and_actual_source_counts():
    output = {
        "read_body": "read evidence é🦁 " * 2400,
        "editable": "other evidence 0123456789 " * 2200,
        "success": True,
        "error": "status stays whole",
    }
    branch, requests, originals = await _clip_fixture([output], cap=1000000)
    branch.chat_model.tool_result_max_chars = 4000
    original = originals[0]
    projection = _projection_session(branch, branch.chat_model)
    source = projection.source(original)
    initial = projection.project(original)
    by_key = {metric.body.key: metric for metric in initial.bodies}
    read_body = by_key["read_body"].body
    # This read is already hidden by the standing projection. It must neither widen
    # that history nor prevent an unrelated mapping body from reducing its allowance.
    protected = (read_body.start + 15000, read_body.start + 15100)
    omitted = re.search(r"omitted from \[(\d+), (\d+)\)", initial.output["read_body"])
    assert omitted is not None
    omitted_start, omitted_stop = map(int, omitted.groups())
    assert omitted_start < protected[0] < protected[1] < omitted_stop
    branch.metadata["context_read_results"] = {str(original.id): [protected]}
    assert projection.project(original).output == initial.output
    before = estimate_next_request_tokens(branch)
    fold = ContextFold(branch, cap=2 * before, target=(before - 8) / (2 * before))
    original_before, request_before = original.to_dict(), requests[0].to_dict()
    record_ids = list(branch.msgs.progression)
    fired = []
    branch.on_message_added.append(fired.append)
    assert await fold._clip_once("unrelated-protected-body")
    (accepted,) = _active_results(branch)
    assert accepted.metadata["clipped_from"] == str(original.id)
    assert accepted.content.action_request_id is None
    assert list(accepted.content.output) == list(output)
    assert accepted.content.output["read_body"] == initial.output["read_body"]
    assert accepted.content.output["success"] is True
    assert accepted.content.output["error"] == output["error"]
    assert branch.metadata["context_read_results"] == {str(original.id): [protected]}
    actual = accepted.metadata["context_clip_kept"]
    for number, metric in enumerate(initial.bodies):
        body = metric.body
        value = accepted.content.output[body.key]
        if body.protected:
            assert actual[number] == metric.original_length
            continue
        assert len(value) <= 4000
        marker = re.search(r"omitted from \[(\d+), (\d+)\)", value)
        assert marker is not None
        start, stop = map(int, marker.groups())
        assert body.start <= start < stop <= body.end
        assert actual[number] == start - body.start + body.end - stop
        assert actual[number] >= min(1024, metric.k_n)
        assert value.startswith(source.text[body.start : start])
        assert value.endswith(source.text[stop : body.end])
        assert value.count('context(action="read_result"') == 1
    read_number = next(n for n, m in enumerate(initial.bodies) if m.body.key == "read_body")
    editable_number = next(n for n, m in enumerate(initial.bodies) if m.body.key == "editable")
    assert actual[read_number] == by_key["read_body"].retained
    assert actual[editable_number] < by_key["editable"].retained
    assert original.to_dict() == original_before and requests[0].to_dict() == request_before
    assert list(branch.msgs.progression) == record_ids + [accepted.id]
    assert [m for m in fired if m.metadata.get("clipped_from")] == [accepted]
    record = fold.records[-1]
    assert record["estimate_after"] == fold.estimate() < before
    assert record["clipped"] == [
        {
            "index": source.index,
            "chars": by_key["editable"].original_length,
            "kept": actual[editable_number],
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("priority", ["effective-largest", "oldest-tie"])
async def test_small_deficit_clips_effective_largest_then_oldest_only(priority):
    outputs = ["old source 0123456789 " * 6000, "new source 0123456789 " * 6000]
    outputs = [
        outputs[0][:100045],
        outputs[1][: 12045 if priority == "effective-largest" else 100045],
    ]
    branch, requests, originals = await _clip_fixture(outputs, cap=1000000)
    branch.chat_model.tool_result_max_chars = 4000
    projection = _projection_session(branch, branch.chat_model)
    metrics = [projection.project(message).bodies[0] for message in originals]
    if priority == "effective-largest":
        assert len(outputs[0]) > len(outputs[1])
        assert metrics[1].retained > metrics[0].retained
        chosen, untouched = 1, 0
    else:
        assert metrics[0].retained == metrics[1].retained
        chosen, untouched = 0, 1
    before = estimate_next_request_tokens(branch)
    fold = ContextFold(branch, cap=2 * before, target=(before - 8) / (2 * before))
    record_ids = list(branch.msgs.progression)
    originals_before = [m.to_dict() for m in originals]
    requests_before = [m.to_dict() for m in requests]
    fired = []
    branch.on_message_added.append(fired.append)
    assert await fold._clip_once(priority)
    active = _active_results(branch)
    assert len(active) == 2
    accepted = active[chosen]
    assert accepted.metadata["clipped_from"] == str(originals[chosen].id)
    assert active[untouched] is originals[untouched]
    assert accepted.metadata["context_clip_kept"][0] < metrics[chosen].retained
    assert accepted.content.action_request_id is None
    assert all(request.id in branch.progression for request in requests)
    assert [m.to_dict() for m in originals] == originals_before
    assert [m.to_dict() for m in requests] == requests_before
    assert list(branch.msgs.progression) == record_ids + [accepted.id]
    assert [m for m in fired if m.metadata.get("clipped_from")] == [accepted]
    record = fold.records[-1]
    assert record["estimate_after"] == fold.estimate() < before
    assert record["target_reached"] is True
    assert record["clipped"] == [
        {
            "index": branch.msgs.progression.index(originals[chosen].id),
            "chars": len(outputs[chosen]),
            "kept": accepted.metadata["context_clip_kept"][0],
        }
    ]


@pytest.mark.asyncio
async def test_bounded_sixteen_undelivered_results_reach_one_real_request(monkeypatch):
    from lionagi.agent.context_fold import install_context_fold
    from lionagi.cli.agent import _tool_results
    from lionagi.protocols.messages import ActionResponse
    from lionagi.service.connections.api_calling import estimate_payload_input_tokens
    from lionhub.providers.anthropic.messages import AnthropicMessagesEndpoint
    from tests.cli.test_agent_api_provider import _text
    from tests.cli.test_api_leg_bounded_results import _check_output, _sent_results

    outputs = [
        (f"result-{number:02} " + "evidence é🦁 雪 alpha beta 0123456789\n" * 4000)[:100045]
        for number in range(16)
    ]
    branch, requests, originals = await _clip_fixture(outputs, cap=20000)
    branch.chat_model.tool_result_max_chars = 4000
    original_states = [message.to_dict() for message in originals]
    request_states = [message.to_dict() for message in requests]
    sent = []

    async def call(self, request, *args, **kwargs):
        sent.append(request)
        return _text("all sixteen retained")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", call)
    try:
        fold = install_context_fold(branch, cap=20000, fold_at=0.9, target=0.5)
        assert fold._estimate is estimate_next_request_tokens
        assert fold.estimate() > fold.fold_at * fold.cap
        assert not any(
            str(message.id) in branch.metadata["context_delivered"] for message in originals
        )
        # Prove that a legal, original-derived floor view can reach this target.
        session = _projection_session(branch, branch.chat_model)
        replacements = {}
        for original in originals:
            floor = session.project(original, allowances=[1024], minimums=[1024])
            replacements[original.id] = ActionResponse(
                content={
                    "function": original.content.function,
                    "arguments": original.content.arguments,
                    "output": floor.output,
                },
                metadata={
                    "clipped_from": str(original.id),
                    "context_clip_kept": [floor.bodies[0].retained],
                },
            )
        view = [branch.msgs.messages[uid] for uid in branch.progression]
        assert (
            fold._estimate_view([replacements.get(message.id, message) for message in view])
            <= 10000
        )
        assert _tool_results(branch) == 16
        assert await branch.chat("inspect every result and answer") == "all sixteen retained"
        assert len(sent) == 1
        assert estimate_payload_input_tokens(sent[0]) <= 10000
        assert fold.estimate() <= 10000
        active = _active_results(branch)
        assert len(active) == 16
        assert {message.metadata.get("clipped_from", str(message.id)) for message in active} == {
            str(message.id) for message in originals
        }
        assert all(request.id in branch.progression for request in requests)
        assert _tool_results(branch) == 16
        for original in originals:
            actual = _sent_results(sent[0], original.content.function)
            assert actual, f"the request lost {original.content.function}"
            index = branch.msgs.progression.index(original.id)
            for result in actual:
                _check_output(original.content.output, result["output"], 4000, index)
        assert any(
            record["stage"] == "clip" and record["target_reached"] for record in fold.records
        )
        assert not any(record["stage"] in ("tool_io", "all") for record in fold.records)
        assert [message.to_dict() for message in originals] == original_states
        assert [message.to_dict() for message in requests] == request_states
    finally:
        await branch.mdls.shutdown()


@pytest.mark.asyncio
async def test_bounded_old_copy_survives_tightening_clone_reload_and_range_restore():
    from lionagi.cli.agent import _tool_results
    from lionagi.session.branch import Branch
    from lionagi.tools.context.context import ContextTool
    from tests.cli.test_api_leg_bounded_results import _check_output, _sent_results

    output = ("original é🦁 雪 evidence 0123456789\n" * 5000)[:100045]
    branch, requests, originals = await _clip_fixture([output], cap=20000)
    original = originals[0]
    original_before, request_before = original.to_dict(), requests[0].to_dict()
    cloned = restored = None
    try:
        assert branch.chat_model.tool_result_max_chars is None
        old_fold = ContextFold(branch, cap=20000)
        assert await old_fold._clip_once("unbounded-copy")
        (old_copy,) = _active_results(branch)
        assert old_copy.metadata["context_clip_kept"][0] > 4000
        assert old_copy.metadata["clipped_from"] == str(original.id)
        record_before = [message.to_dict() for message in branch.msgs.messages]
        view_before = list(branch.progression)
        branch.chat_model.tool_result_max_chars = 4000
        session = _projection_session(branch, branch.chat_model)
        standing = session.project(old_copy)
        assert standing.bodies[0].retained < old_copy.metadata["context_clip_kept"][0]
        index = branch.msgs.progression.index(original.id)
        actual = _sent_results(_compiled_view_payload(branch), original.content.function)
        assert actual and all(item["output"] == standing.output for item in actual)
        _check_output(output, standing.output, 4000, index)
        assert [message.to_dict() for message in branch.msgs.messages] == record_before
        assert list(branch.progression) == view_before

        before = estimate_next_request_tokens(branch)
        tighter = ContextFold(branch, cap=math.ceil(before / 0.8), fold_at=0.9, target=0.5)
        assert tighter._estimate is estimate_next_request_tokens
        assert await tighter._clip_once("bounded-copy-deficit")
        (copy,) = _active_results(branch)
        assert copy.id != old_copy.id and old_copy.id not in branch.progression
        assert copy.metadata["clipped_from"] == str(original.id)
        current = _projection_session(branch, branch.chat_model).project(copy)
        kept = current.bodies[0].retained
        assert min(1024, current.bodies[0].k_n) <= kept < standing.bodies[0].retained
        assert copy.metadata["context_clip_kept"] == [kept]
        assert tighter.estimate() < before
        assert tighter.records[-1]["estimate_after"] == tighter.estimate()
        assert copy.content.output == current.output
        _check_output(output, copy.content.output, 4000, index)
        calls = tighter._tool_calls([old_copy, copy])
        assert len(calls) == 1 and calls[0]["output_chars"] == len(output)
        note = tighter._note(1, calls, None)
        assert "stored-output size" in note and f"{len(output):,} chars" in note
        assert _tool_results(branch) == 1
        assert original.to_dict() == original_before and requests[0].to_dict() == request_before

        cloned = branch.clone()
        assert cloned.chat_model.tool_result_max_chars == 4000
        restored = Branch.from_dict(cloned.to_dict())
        assert restored.chat_model.tool_result_max_chars == 4000
        (clone_copy,) = _active_results(restored)
        clone_original = restored.msgs.messages[clone_copy.metadata["clipped_from"]]
        assert clone_original.id != original.id
        assert clone_copy.metadata["clipped_from"] == str(clone_original.id)
        assert clone_original.content.output == output
        clone_index = restored.msgs.progression.index(clone_original.id)
        _check_output(
            output,
            _sent_results(_compiled_view_payload(restored), clone_original.content.function)[0][
                "output"
            ],
            4000,
            clone_index,
        )
        context = ContextTool().bind(restored)
        restored.register_tools(context)
        work_before = _tool_results(restored)
        results = await restored.act(
            {
                "function": context.function,
                "arguments": {
                    "action": "read_result",
                    "index": clone_index,
                    "offset": 1000,
                    "limit": 20000,
                },
            },
            strategy="sequential",
            suppress_errors=False,
        )
        assert len(results) == 1
        page = results[0].output
        assert page["success"] is True and page["limit"] == 4000
        assert page["text"] == output[1000:5000] and page["next_offset"] == 5000
        assert _tool_results(restored) == work_before + 1
        page_result = next(
            message
            for message in reversed(_active_results(restored))
            if message.content.function == context.function
        )
        page_before = page_result.to_dict()
        gate = ContextFold(restored, cap=20000, fold_at=0.9, target=0.5)
        expected = [
            clone_original.id if uid == clone_copy.id else uid for uid in restored.progression
        ]
        candidate = [restored.msgs.messages[uid] for uid in expected]
        assert gate._estimate_view(candidate) < gate.cap
        restored.chat_model.tool_result_max_chars = None
        try:
            assert gate._estimate_view(candidate) > gate.cap
        finally:
            restored.chat_model.tool_result_max_chars = 4000
        last_copy_index = max(
            restored.msgs.progression.index(message.id)
            for message in restored.msgs.messages
            if message.metadata.get("clipped_from") == str(clone_original.id)
        )
        recovered = await context.func_callable(
            action="restore", start=clone_index, end=last_copy_index + 1
        )
        assert recovered["success"] is True and recovered["restored"] == 1
        assert list(restored.progression) == expected
        assert clone_copy.id not in restored.progression
        assert sum(uid == clone_original.id for uid in restored.progression) == 1
        assert not any(
            message.metadata.get("clipped_from") for message in _active_results(restored)
        )
        payload = _compiled_view_payload(restored)
        originals_sent = _sent_results(payload, clone_original.content.function)
        assert originals_sent
        for item in originals_sent:
            _check_output(output, item["output"], 4000, clone_index)
        pages_sent = _sent_results(payload, context.function)
        assert any(item["output"]["text"] == page["text"] for item in pages_sent)
        assert page_result.to_dict() == page_before
        assert _tool_results(restored) == work_before + 1
    finally:
        if restored is not None:
            await restored.mdls.shutdown()
        await branch.mdls.shutdown()


@pytest.mark.asyncio
async def test_bounded_legacy_1999_allowance_keeps_complete_original_marker():
    from lionagi.protocols.generic.progression import Progression
    from lionagi.protocols.messages import ActionResponse
    from tests.cli.test_api_leg_bounded_results import _MARKER, _sent_results

    output = ("head é🦁 " * 300)[:2000]
    branch, requests, originals = await _clip_fixture([output], cap=1000000)
    original = originals[0]
    index = branch.msgs.progression.index(original.id)
    kept = 1999
    head, tail = 3 * kept // 4, kept - 3 * kept // 4
    stop = len(output) - tail
    marker = (
        f"\n[1 characters omitted; recovery range [{head}, {stop}); "
        f'context(action="read_result", index={index}, offset={head}, limit=1); '
        f"continue at next_offset with limit=min(16000, {stop}-next_offset), "
        f"stopping at {stop}.]\n"
    )
    legacy = ActionResponse(
        content={
            "function": original.content.function,
            "arguments": original.content.arguments,
            "output": output[:head] + marker + output[-tail:],
        },
        metadata={"clipped_from": str(original.id), "context_clip_kept": [kept]},
    )
    await branch.msgs._a_add_built_response(legacy)
    branch.metadata["current_progression"] = Progression(
        order=[
            legacy.id if uid == original.id else uid
            for uid in branch.progression
            if uid != legacy.id
        ]
    )
    records = [message.to_dict() for message in branch.msgs.messages]
    view = list(branch.progression)
    try:
        assert len(output) == 2000 and len(legacy.content.output) > 2000
        branch.chat_model.tool_result_max_chars = 2000
        session = _projection_session(branch, branch.chat_model)
        projected = session.project(legacy)
        metric = projected.bodies[0]
        assert metric.original_length == metric.k_n == 2000
        assert min(1024, metric.k_n) <= metric.retained < kept
        actual = _sent_results(_compiled_view_payload(branch), original.content.function)
        assert actual and all(item["output"] == projected.output for item in actual)
        match = _MARKER.search(projected.output)
        assert match is not None and projected.output.count('context(action="read_result"') == 1
        start, end = int(match["start"]), int(match["end"])
        assert int(match["index"]) == index and int(match["count"].replace(",", "")) == end - start
        assert metric.retained == start + len(output) - end
        assert projected.output == output[:start] + "\n" + match[0] + "\n" + output[end:]
        assert len(projected.output) == metric.projected_length <= 2000
        assert [message.to_dict() for message in branch.msgs.messages] == records
        assert list(branch.progression) == view
        assert original.content.output == output and requests[0].id in branch.progression
        fold = ContextFold(branch, cap=1, fold_at=0.9, target=0.5)
        assert await fold._clip_once("legacy-boundary-floor")
        (accepted,) = _active_results(branch)
        assert accepted.metadata["clipped_from"] == str(original.id)
        assert accepted.metadata["context_clip_kept"] == [1024]
        assert len(accepted.content.output) <= 2000
        assert accepted.content.output.count('context(action="read_result"') == 1
        assert fold.records[-1]["estimate_after"] < fold.records[-1]["estimate_before"]
        assert fold.records[-1]["target_reached"] is False
        assert original.content.output == output
    finally:
        await branch.mdls.shutdown()


@pytest.mark.asyncio
async def test_bounded_forecast_differs_from_long_actual_instruction_without_cutting_it(
    monkeypatch,
):
    from lionagi._errors import InputTokenCapError
    from lionagi.agent.context_fold import install_context_fold
    from lionagi.operations.chat._prepare import _prepare_run_kwargs
    from lionagi.operations.types import ChatParam
    from lionagi.service.connections.api_calling import estimate_payload_input_tokens
    from lionhub.providers.anthropic.messages import AnthropicMessagesEndpoint
    from tests.cli.test_api_leg_bounded_results import _check_output, _sent_results
    from tests.cli.test_api_leg_unset_payload_identity import _payload_text

    output = ("evidence é🦁 0123456789\n" * 6000)[:100045]
    branch, _requests, originals = await _clip_fixture([output], cap=20000)
    branch.chat_model.tool_result_max_chars = 4000
    branch.chat_model.input_token_scale = 2.0
    task = "Preserve this exact instruction. " + "unshortened task guidance 雪 0123456789\n" * 4000
    sent = []

    async def forbidden(self, request, *args, **kwargs):
        sent.append(request)
        raise AssertionError("an over-ceiling request must not reach the transport")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", forbidden)
    try:
        fold = install_context_fold(branch, cap=20000, fold_at=0.9, target=0.5)
        assert (fold.fold_at, fold.target) == (0.9, 0.5)
        forecast_raw = estimate_next_request_tokens(branch)
        forecast = fold.estimate()
        assert forecast == math.ceil(forecast_raw * 2.0) < fold.fold_at * fold.cap
        view = [branch.msgs.messages[uid] for uid in branch.progression]
        assert fold._estimate_view(view) == forecast
        _instruction, request = _prepare_run_kwargs(
            branch, task, ChatParam.from_branch(branch), imodel=branch.chat_model
        )
        payload, _headers = branch.chat_model.endpoint.create_payload(request=request)
        assert task in _payload_text(payload)
        actual_raw = estimate_payload_input_tokens(payload)
        assert math.ceil(actual_raw * 2.0) > fold.cap > forecast
        actual_results = _sent_results(payload, originals[0].content.function)
        assert actual_results
        for item in actual_results:
            _check_output(
                output, item["output"], 4000, branch.msgs.progression.index(originals[0].id)
            )
        with pytest.raises(InputTokenCapError) as caught:
            await branch.chat(task)
        assert branch.chat_model.input_cap_refusal is caught.value
        assert sent == [] and fold.records == []
        assert originals[0].content.output == output
    finally:
        await branch.mdls.shutdown()


@pytest.mark.asyncio
async def test_bounded_unrenderable_value_refuses_before_any_clip(monkeypatch):
    from unittest.mock import AsyncMock

    from yaml.representer import RepresenterError

    bad = object()
    branch, _requests, originals = await _clip_fixture([bad], cap=6000)
    branch.chat_model.tool_result_max_chars = 4000
    fold = ContextFold(branch, cap=6000, fold_at=0.9, target=0.5)
    clip = AsyncMock(side_effect=AssertionError("clip must not run before render refusal"))
    monkeypatch.setattr(fold, "_clip_once", clip)
    ids, view = list(branch.msgs.progression), list(branch.progression)
    try:
        with pytest.raises(RepresenterError):
            _compiled_view_payload(branch)
        with pytest.raises(RepresenterError):
            await fold.fold(reason="bounded-unrenderable")
        clip.assert_not_awaited()
        assert fold.records == []
        assert list(branch.msgs.progression) == ids and list(branch.progression) == view
        assert originals[0].content.output is bad
    finally:
        await branch.mdls.shutdown()


@pytest.mark.asyncio
async def test_bounded_recovery_page_is_clip_protected_until_main_completion(monkeypatch):
    from lionagi.agent.context_fold import install_context_fold
    from lionagi.tools.context.context import ContextTool
    from lionhub.providers.anthropic.messages import AnthropicMessagesEndpoint
    from tests.cli.test_agent_api_provider import _text
    from tests.cli.test_api_leg_bounded_results import _sent_results

    output = ("evidence é🦁 0123456789\n" * 6000)[:100045]
    branch, _requests, originals = await _clip_fixture([output], cap=1000000)
    branch.chat_model.tool_result_max_chars = 4000
    original = originals[0]
    original_before = original.to_dict()
    index = branch.msgs.progression.index(original.id)
    context = ContextTool().bind(branch)
    branch.register_tools(context)
    fold = install_context_fold(branch, cap=1000000)
    sent = []

    async def call(self, request, *args, **kwargs):
        # The range is still protected while the main request is in flight.
        assert branch.metadata["context_read_results"][str(original.id)] == [[1000, 5000]]
        actual = _sent_results(request, context.function)
        assert actual and any(item["output"]["text"] == output[1000:5000] for item in actual)
        sent.append(request)
        return _text("recovered page read")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", call)
    try:
        results = await branch.act(
            {
                "function": context.function,
                "arguments": {
                    "action": "read_result",
                    "index": index,
                    "offset": 1000,
                    "limit": 4000,
                },
            },
            strategy="sequential",
            suppress_errors=False,
        )
        assert len(results) == 1 and results[0].output["text"] == output[1000:5000]
        page = next(
            message
            for message in reversed(_active_results(branch))
            if message.content.function == context.function
        )
        page_before = page.to_dict()
        full, active = list(branch.msgs.progression), list(branch.progression)
        fold.cap = 1
        assert await fold._clip_once("before-page-delivery") is False
        assert list(branch.msgs.progression) == full and list(branch.progression) == active
        assert page.to_dict() == page_before and original.to_dict() == original_before
        assert branch.metadata["context_read_results"][str(original.id)] == [[1000, 5000]]

        fold.cap = 1000000  # The next request delivers, rather than forcing another fold.
        assert await branch.chat("read the recovered evidence") == "recovered page read"
        assert len(sent) == 1
        assert "context_read_results" not in branch.metadata
        assert str(page.id) in branch.metadata["context_delivered"]
        assert page.to_dict() == page_before

        fold.cap = 1
        assert await fold._clip_once("after-page-delivery")
        assert page.id not in branch.progression
        copies = [
            message
            for message in _active_results(branch)
            if message.metadata.get("clipped_from") == str(page.id)
        ]
        assert len(copies) == 1
        copied_text = copies[0].content.output["text"]
        assert len(copied_text) <= 4000
        assert copied_text.count('context(action="read_result"') == 1
        page_index = branch.msgs.progression.index(page.id)
        assert f'context(action="read_result", index={page_index}, ' in copied_text
        assert fold.records[-1]["estimate_after"] < fold.records[-1]["estimate_before"]
        assert page.to_dict() == page_before and original.to_dict() == original_before
    finally:
        await branch.mdls.shutdown()


@pytest.mark.asyncio
async def test_bounded_context_estimates_stay_advisory_beside_real_compiled_count():
    from lionagi.tools.context.context import ContextTool, TokenCalculator

    output = ("evidence é🦁 0123456789\n" * 6000)[:100045]
    branch, _requests, originals = await _clip_fixture([output], cap=1000000)
    original = originals[0]
    original_before = original.to_dict()
    context = ContextTool().bind(branch).func_callable
    fold = ContextFold(branch, cap=1000000)
    try:
        unbounded_status = await context(action="status")
        unbounded_count = fold.estimate()
        branch.chat_model.tool_result_max_chars = 4000
        bounded_status = await context(action="status")
        assert bounded_status == unbounded_status
        bounded_raw = estimate_next_request_tokens(branch)
        assert bounded_raw < unbounded_count
        branch.chat_model.input_token_scale = 2.0
        assert fold.estimate() == 2 * bounded_raw
        assert await context(action="status") == bounded_status
        assert bounded_status["estimated_active_tokens"] != fold.estimate()

        # This is the existing advisory message-content measure, not payload savings.
        content = original.content
        legacy_cost = TokenCalculator.tokenize(
            content if isinstance(content, str) else str(content)
        )
        compiled_before = fold.estimate()
        assert legacy_cost > compiled_before  # standing projection hides most stored text
        slot = list(branch.progression).index(original.id)
        compacted = await context(
            action="compact",
            start=slot,
            end=slot + 1,
            mode="all",
            summary="Concise evidence summary.",
        )
        assert compacted["success"] is True and compacted["compacted"] == 1
        assert compacted["tokens_freed_est"] == legacy_cost
        compiled_after = fold.estimate()
        assert compiled_after < compiled_before
        assert compacted["tokens_freed_est"] != compiled_before - compiled_after
        assert original.to_dict() == original_before
    finally:
        await branch.mdls.shutdown()
