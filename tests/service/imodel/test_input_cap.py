# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""``max_input_tokens``: a request whose estimated input exceeds the cap is refused before it is
sent, and the estimate is held to the provider's own count once a response has reported it.
The endpoint is stubbed, so no request leaves the process.
"""

from __future__ import annotations

import json
import math

import pytest

from lionagi._errors import InputTokenCapError
from lionagi.providers.anthropic.messages import AnthropicMessagesEndpoint
from lionagi.service.connections.api_calling import estimate_payload_input_tokens
from lionagi.service.imodel import iModel, reported_input_tokens


def _haiku(cap: int | None) -> iModel:
    return iModel(
        provider="anthropic",
        model="claude-haiku-5-5",
        api_key="test-not-a-key",
        max_tokens=64,
        max_input_tokens=cap,
    )


def _message(text: str, input_tokens: int = 1) -> dict:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": input_tokens, "output_tokens": 1},
    }


# ── the cap, at the request ───────────────────────────────────────────────


def test_a_request_over_the_cap_is_refused_with_the_estimate_and_the_cap():
    model = _haiku(200)
    with pytest.raises(InputTokenCapError) as exc_info:
        model.create_api_calling(messages=[{"role": "user", "content": "word " * 1000}])
    msg = str(exc_info.value)
    assert "max_input_tokens=200" in msg
    assert exc_info.value.details["cap"] == 200
    assert exc_info.value.details["estimate"] > 200
    assert f"{exc_info.value.details['estimate']:,}" in msg
    assert model.input_cap_refusal is exc_info.value


def test_a_request_under_the_cap_is_built():
    call = _haiku(200).create_api_calling(messages=[{"role": "user", "content": "hello"}])
    assert call.payload["messages"]
    assert 0 < call.input_token_estimate <= 200
    assert call.counted_input_tokens == call.input_token_estimate


def test_the_system_prompt_counts_toward_the_cap():
    # The Messages API carries the system prompt outside `messages`; it is still input.
    with pytest.raises(InputTokenCapError):
        _haiku(200).create_api_calling(
            messages=[
                {"role": "system", "content": "rule " * 1000},
                {"role": "user", "content": "hi"},
            ]
        )


def test_a_response_format_counts_toward_the_estimate():
    # A structured-output schema goes out with the request and is read as input.
    messages = [{"role": "user", "content": "hi"}]
    schema = {"type": "json_schema", "json_schema": {"name": "r", "description": "field " * 500}}
    without = estimate_payload_input_tokens({"messages": messages})
    assert estimate_payload_input_tokens({"messages": messages, "response_format": schema}) > (
        without + 400
    )


def test_special_token_text_is_counted_not_estimated_as_zero():
    call = _haiku(None).create_api_calling(
        messages=[{"role": "user", "content": "<|endoftext|> " * 50}]
    )
    assert call.input_token_estimate > 50


def test_no_cap_builds_any_size_and_counts_nothing_for_it():
    call = _haiku(None).create_api_calling(messages=[{"role": "user", "content": "word " * 5000}])
    assert call.input_token_estimate > 5000
    assert call.counted_input_tokens is None


def test_the_first_refusal_is_the_one_kept():
    model = _haiku(100)
    with pytest.raises(InputTokenCapError) as first:
        model.create_api_calling(messages=[{"role": "user", "content": "a " * 500}])
    with pytest.raises(InputTokenCapError):
        model.create_api_calling(messages=[{"role": "user", "content": "b " * 900}])
    assert model.input_cap_refusal is first.value


def test_a_model_that_refused_refuses_every_later_request_until_cleared():
    model = _haiku(100)
    with pytest.raises(InputTokenCapError) as first:
        model.create_api_calling(messages=[{"role": "user", "content": "a " * 500}])
    with pytest.raises(InputTokenCapError) as later:
        model.create_api_calling(messages=[{"role": "user", "content": "hi"}])
    assert later.value is first.value
    model.input_cap_refusal = None
    assert model.create_api_calling(messages=[{"role": "user", "content": "hi"}]) is not None


@pytest.mark.asyncio
async def test_invoke_and_stream_raise_the_cap_refusal_itself():
    big = [{"role": "user", "content": "a " * 500}]
    with pytest.raises(InputTokenCapError):
        await _haiku(100).invoke(messages=big)
    with pytest.raises(InputTokenCapError):
        async for _ in _haiku(100).stream(messages=big):
            pass


@pytest.mark.asyncio
async def test_a_refused_parse_repair_raises_instead_of_returning_the_text():
    from pydantic import BaseModel

    from lionagi import Branch

    class Out(BaseModel):
        answer: str

    model = _haiku(10)
    branch = Branch(chat_model=model, parse_model=model)
    with pytest.raises(InputTokenCapError):
        await branch.parse(
            "prose, not the structured answer",
            response_format=Out,
            handle_validation="return_value",
        )


@pytest.mark.asyncio
async def test_branch_react_raises_a_refused_final_answer_turn(monkeypatch):
    # A direct Branch.ReAct caller gets the refusal itself, never an earlier round's text as
    # the answer.
    from lionagi import Branch
    from lionagi.service.connections.api_calling import APICalling

    replies = [_message(json.dumps({"analysis": "done", "extension_needed": False}))]
    sent: list[dict] = []

    async def fake_call(self, request, *a, **kw):
        sent.append(request)
        return replies.pop(0)

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", fake_call)
    built: list[int] = []

    def estimate(self):
        built.append(1)
        return 100 if len(built) == 1 else 10_000

    monkeypatch.setattr(APICalling, "input_token_estimate", property(estimate))
    branch = Branch(chat_model=_haiku(1000))
    with pytest.raises(InputTokenCapError):
        await branch.ReAct(instruction="t", max_extensions=0)
    assert len(sent) == 1
    assert len(built) == 2


def test_the_cap_survives_copy_and_a_serialization_round_trip():
    model = _haiku(1234)
    assert model.copy().max_input_tokens == 1234
    assert iModel.from_dict(model.to_dict()).max_input_tokens == 1234
    assert "max_input_tokens" not in _haiku(None).to_dict()


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "100"])
def test_a_cap_that_is_not_a_positive_integer_is_refused(bad):
    with pytest.raises(ValueError, match="max_input_tokens"):
        iModel(provider="anthropic", model="claude-haiku-5-5", api_key="x", max_input_tokens=bad)


# ── the cap, calibrated by the provider's own counts ──────────────────────


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"prompt_tokens": 120, "completion_tokens": 9}, 120),
        (
            {
                "input_tokens": 10,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 20,
                "output_tokens": 5,
            },
            130,
        ),
        ({"input_tokens": 50, "input_tokens_details": {"cached_tokens": 40}}, 50),
        ({"input_tokens": 10, "cache_read_input_tokens": True}, 10),
        ({"input_tokens": True}, None),
        ({"input_tokens": -1}, None),
        ({"prompt_tokens": "9"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_the_reported_input_is_read_from_each_usage_shape(usage, expected):
    assert reported_input_tokens(usage) == expected


def _counted(text: str) -> int:
    return (
        _haiku(None)
        .create_api_calling(messages=[{"role": "user", "content": text}])
        .input_token_estimate
    )


def _reporting(monkeypatch, ratio: float, sent: list[dict] | None = None) -> None:
    """Answer every request with an input count of ``ratio`` times the cap's own count of it."""

    async def fake_call(self, request, *a, **kw):
        if sent is not None:
            sent.append(request)
        return _message("ok", input_tokens=int(ratio * estimate_payload_input_tokens(request)))

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", fake_call)


def test_the_cap_compares_the_scaled_estimate_and_names_the_scale():
    text = "word " * 300
    counted = _counted(text)
    model = _haiku(counted + 10)
    call = model.create_api_calling(messages=[{"role": "user", "content": text}])
    assert call.counted_input_tokens == counted

    model.input_token_scale = 1.5
    with pytest.raises(InputTokenCapError) as exc_info:
        model.create_api_calling(messages=[{"role": "user", "content": text}])

    assert exc_info.value.details == {
        "estimate": math.ceil(counted * 1.5),
        "cap": counted + 10,
        "counted": counted,
        "scale": 1.5,
    }
    assert "times 1.500" in str(exc_info.value)


@pytest.mark.asyncio
async def test_a_request_the_cap_counted_low_is_refused_once_a_response_calibrates_it(
    monkeypatch,
):
    # The provider counts twice what the cap counts. The first request passes on the cap's own
    # count; once its response shows the provider's count, the same request is refused.
    sent: list[dict] = []
    _reporting(monkeypatch, 2.0, sent)
    text = "word " * 6000
    counted = _counted(text)
    assert counted >= 4_000
    model = _haiku(int(1.5 * counted))

    await model.invoke(messages=[{"role": "user", "content": text}])
    assert model.input_token_scale == pytest.approx(2.0, rel=0.01)
    with pytest.raises(InputTokenCapError) as exc_info:
        await model.invoke(messages=[{"role": "user", "content": text}])

    assert len(sent) == 1
    assert exc_info.value.details["counted"] == counted
    assert exc_info.value.details["scale"] == pytest.approx(2.0, rel=0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("words", "cap", "ratio"),
    [
        (6000, 100_000, 0.5),  # the provider counts fewer: the cap never admits more than its count
        (500, 100_000, 3.0),  # under the floor: a small request does not calibrate
        (6000, None, 3.0),  # no cap: nothing is counted, nothing calibrates
    ],
)
async def test_the_scale_rises_only_from_a_counted_request_over_the_floor(
    monkeypatch, words, cap, ratio
):
    _reporting(monkeypatch, ratio)
    model = _haiku(cap)
    await model.invoke(messages=[{"role": "user", "content": "word " * words}])
    assert model.input_token_scale == 1.0


@pytest.mark.asyncio
async def test_the_scale_keeps_the_highest_ratio_seen(monkeypatch):
    model = _haiku(100_000)
    messages = [{"role": "user", "content": "word " * 6000}]
    _reporting(monkeypatch, 1.6)
    await model.invoke(messages=messages)
    _reporting(monkeypatch, 1.2)
    await model.invoke(messages=messages)
    assert model.input_token_scale == pytest.approx(1.6, rel=0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape",
    [
        "messages",  # input on message_start, output only on message_delta
        "messages_zeroed",  # message_delta repeats the input keys at zero
        "messages_moved",  # message_delta moves input from one key to another
        "messages_grown",  # message_delta reports more input than message_start
        "messages_fresh_only",  # message_delta carries input_tokens alone, cache keys left out
        "messages_cache_only",  # message_delta carries a cache key alone
        "chat_completions",  # a closing usage chunk
        "responses",  # usage on response.completed
    ],
)
async def test_a_streamed_response_calibrates_the_scale_too(monkeypatch, shape):
    async def fake_stream(self, payload, headers, **kw):
        counted = estimate_payload_input_tokens(payload)
        start = {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": counted,
                    "cache_read_input_tokens": counted,
                    "output_tokens": 1,
                }
            },
        }
        text = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}}
        events = {
            "messages": [start, text, {"type": "message_delta", "usage": {"output_tokens": 2}}],
            "messages_zeroed": [
                start,
                text,
                {
                    "type": "message_delta",
                    "usage": {"input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 2},
                },
            ],
            "messages_moved": [
                {
                    "type": "message_start",
                    "message": {
                        "usage": {"input_tokens": counted, "cache_read_input_tokens": counted}
                    },
                },
                text,
                {
                    "type": "message_delta",
                    "usage": {"input_tokens": 0, "cache_read_input_tokens": 2 * counted},
                },
            ],
            "messages_grown": [
                {"type": "message_start", "message": {"usage": {"input_tokens": counted}}},
                text,
                {
                    "type": "message_delta",
                    "usage": {"input_tokens": 2 * counted, "output_tokens": 2},
                },
            ],
            "messages_fresh_only": [
                {
                    "type": "message_start",
                    "message": {"usage": {"input_tokens": 0, "cache_read_input_tokens": counted}},
                },
                text,
                {"type": "message_delta", "usage": {"input_tokens": counted, "output_tokens": 2}},
            ],
            "messages_cache_only": [
                {"type": "message_start", "message": {"usage": {"input_tokens": counted}}},
                text,
                {
                    "type": "message_delta",
                    "usage": {"cache_read_input_tokens": counted, "output_tokens": 2},
                },
            ],
            "chat_completions": [text, {"choices": [], "usage": {"prompt_tokens": 2 * counted}}],
            "responses": [
                text,
                {
                    "type": "response.completed",
                    "response": {"usage": {"input_tokens": 2 * counted}},
                },
            ],
        }[shape]
        for event in events:
            yield self._line_to_stream_chunk(json.dumps(event))

    monkeypatch.setattr(AnthropicMessagesEndpoint, "_stream_http", fake_stream)
    model = _haiku(100_000)
    async for _chunk in model.stream(messages=[{"role": "user", "content": "word " * 6000}]):
        pass

    assert model.input_token_scale == pytest.approx(2.0, rel=0.01)


def test_the_scale_is_carried_by_copy_and_serialization():
    model = _haiku(1000)
    assert "input_token_scale" not in model.to_dict()
    model.input_token_scale = 1.25

    assert model.copy().input_token_scale == 1.25
    data = model.to_dict()
    assert data["input_token_scale"] == 1.25
    assert iModel.from_dict(data).input_token_scale == 1.25
    for bad in ("2", True, float("nan"), float("inf"), 0.5, None):
        assert iModel.from_dict({**data, "input_token_scale": bad}).input_token_scale == 1.0


# The tool-result character bound is local presentation policy, independent of
# the input-token refusal and provider request settings.
def _tool_bound_model(bound, **kwargs):
    return iModel(
        provider="anthropic",
        model="claude-haiku-5-5",
        api_key="test-not-a-key",
        max_tokens=64,
        tool_result_max_chars=bound,
        **kwargs,
    )


@pytest.mark.parametrize("bound", [None, 1024, 4000, 20000])
def test_tool_result_bound_is_validated_local_model_state(bound):
    model = _tool_bound_model(bound)
    assert getattr(model, "tool_result_max_chars", "missing") == bound
    assert "tool_result_max_chars" not in model.endpoint.config.kwargs
    assert "tool_result_max_chars" not in model.endpoint.to_dict()["config"]


@pytest.mark.parametrize(
    "bad",
    [True, False, 0, 1, -1, 1023, "4000", "", 1024.0, float("nan"), float("inf")],
    ids=[
        "true",
        "false",
        "zero",
        "one",
        "negative",
        "below-floor",
        "string",
        "empty",
        "float",
        "nan",
        "infinity",
    ],
)
def test_tool_result_bound_rejects_non_integer_or_too_small_values(bad):
    with pytest.raises(ValueError, match="tool_result_max_chars"):
        _tool_bound_model(bad)


@pytest.mark.parametrize("unfiltered", [False, True], ids=["schema", "schema-less"])
def test_tool_result_bound_never_reaches_endpoint_or_provider_payload(unfiltered):
    model = _tool_bound_model(4000)
    if unfiltered:
        model.endpoint.request_options = None
    payload, headers = model.endpoint.create_payload(
        request={"messages": [{"role": "user", "content": "hello"}]}
    )
    assert "tool_result_max_chars" not in payload
    assert "tool_result_max_chars" not in headers
    assert "tool_result_max_chars" not in model.endpoint.config.kwargs
    assert "tool_result_max_chars" not in json.dumps(model.endpoint.to_dict())
    assert payload["messages"]


@pytest.mark.parametrize("bound", [None, 1024, 4000, 20000])
def test_tool_result_bound_round_trips_without_changing_cap_or_scale(bound):
    model = _tool_bound_model(bound, max_input_tokens=1234)
    model.input_token_scale = 1.75
    data = model.to_dict()
    if bound is None:
        assert "tool_result_max_chars" not in data
    else:
        assert data.get("tool_result_max_chars") == bound
    restored = iModel.from_dict(data, api_key="test-not-a-key")
    assert getattr(restored, "tool_result_max_chars", "missing") == bound
    assert restored.max_input_tokens == 1234
    assert restored.input_token_scale == 1.75
    assert restored.id == model.id
    assert "tool_result_max_chars" not in restored.endpoint.config.kwargs


def test_old_stored_model_without_tool_result_bound_loads_unset():
    data = _haiku(1234).to_dict()
    assert "tool_result_max_chars" not in data
    restored = iModel.from_dict(data, api_key="test-not-a-key")
    assert getattr(restored, "tool_result_max_chars", "missing") is None
    assert "tool_result_max_chars" not in restored.to_dict()
    assert restored.max_input_tokens == 1234


@pytest.mark.parametrize("bad", [True, 1023, "4000", 1024.0, float("nan"), {}, []])
def test_malformed_stored_tool_result_bound_is_rejected(bad):
    data = _haiku(1234).to_dict()
    data["tool_result_max_chars"] = bad
    with pytest.raises(ValueError, match="tool_result_max_chars"):
        iModel.from_dict(data, api_key="test-not-a-key")


@pytest.mark.asyncio
@pytest.mark.parametrize("share_executor", [False, True])
async def test_copy_keeps_tool_bound_cap_scale_and_actual_stream_hook(share_executor):
    from lionagi.service.hooks import HookRegistry

    seen = []

    async def hook(_event, _chunk_type, chunk, **_kw):
        seen.append(chunk["value"])
        return {"choices": [{"delta": {"content": "hooked"}}]}

    model = _tool_bound_model(
        4000,
        max_input_tokens=1234,
        hook_registry=HookRegistry(stream_handlers={"dict": hook}),
        exit_hook=True,
        limit_requests=3,
    )
    model.input_token_scale = 1.75
    copied = model.copy(share_executor=share_executor)
    assert getattr(copied, "tool_result_max_chars", "missing") == 4000
    assert copied.max_input_tokens == 1234
    assert copied.input_token_scale == 1.75
    assert copied.id != model.id
    assert copied.endpoint is not model.endpoint
    assert copied.hook_registry is model.hook_registry
    assert copied.exit_hook is True
    assert copied.executor.config == model.executor.config
    assert (copied.executor is model.executor) is share_executor
    assert await model.process_chunk({"value": "original"}) == {
        "choices": [{"delta": {"content": "hooked"}}]
    }
    assert await copied.process_chunk({"value": "copy"}) == {
        "choices": [{"delta": {"content": "hooked"}}]
    }
    assert seen == ["original", "copy"]
    assert "tool_result_max_chars" not in copied.endpoint.config.kwargs
