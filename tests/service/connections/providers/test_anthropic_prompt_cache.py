# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The prompt-caching option on the Anthropic messages endpoint (ADR-0127 D3, D5, D7)."""

import copy
import json
import os
from contextlib import nullcontext
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from lionagi.providers.anthropic.messages import AnthropicMessagesEndpoint
from lionagi.service import cache_intent
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch

MODEL = "claude-opus-5-5"
SYSTEM = "You review pull requests."
ONE_HOUR = {"type": "ephemeral", "ttl": "1h"}
FIVE_MINUTES = {"type": "ephemeral"}


def _request(**extra):
    return {
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second question"},
        ],
        "model": MODEL,
        "max_tokens": 100,
        **extra,
    }


def _filtered(**config):
    """The default endpoint: its request-option filter is present."""
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-anthropic-key"}):
        return iModel(provider="anthropic", model=MODEL, **config).endpoint


def _unfiltered(**config):
    """An endpoint whose ``request_options`` is None, so nothing filters the payload."""
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-anthropic-key"}):
        return AnthropicMessagesEndpoint(request_options=None, **config)


def _marks(payload):
    """Every cache mark in the payload, in request order: system blocks, then messages."""
    blocks = list(payload.get("system") or [])
    for message in payload["messages"]:
        if isinstance(message["content"], list):
            blocks.extend(message["content"])
    return [block["cache_control"] for block in blocks if block.get("cache_control") is not None]


# --- option off: the request is the one this endpoint built before the option existed ---


def test_option_off_filtered_path_builds_todays_request():
    payload, _ = _filtered().create_payload(_request(cache_control=True))

    # The filter drops `cache_control` before the mark code, so no mark is placed.
    assert json.dumps(payload) == json.dumps(
        {
            "model": MODEL,
            "messages": [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "second question"},
            ],
            "max_tokens": 100,
            "system": [{"type": "text", "text": SYSTEM}],
        }
    )


def test_option_off_unfiltered_path_builds_todays_request():
    payload, _ = _unfiltered().create_payload(_request(cache_control=True))

    # Nothing filters the flag, so one five-minute mark lands on the last message.
    assert json.dumps(payload) == json.dumps(
        {
            "messages": [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "second question", "cache_control": FIVE_MINUTES}
                    ],
                },
            ],
            "model": MODEL,
            "max_tokens": 100,
            "system": [{"type": "text", "text": SYSTEM}],
        }
    )


def test_option_off_is_the_default():
    assert _filtered().config.prompt_caching is False
    assert _filtered().places_prompt_cache is False


def test_option_off_leaves_the_stored_model_as_before():
    """A model is stored with every branch that records it; with the option off its dump
    carries no key the option added."""
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-anthropic-key"}):
        model = iModel(provider="anthropic", model=MODEL)
    assert "prompt_caching" not in model.endpoint.config.model_dump()
    assert "prompt_caching" not in json.loads(model.endpoint.config.model_dump_json())
    assert "prompt_caching" not in model.to_dict()["endpoint"]["config"]


def test_option_on_survives_the_stored_model_round_trip():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-anthropic-key"}):
        model = iModel(provider="anthropic", model=MODEL, prompt_caching=True)
        stored = json.loads(json.dumps(model.to_dict()))
        restored = iModel.from_dict(stored)
    assert stored["endpoint"]["config"]["prompt_caching"] is True
    assert restored.endpoint.config.prompt_caching is True
    assert restored.endpoint.places_prompt_cache is True
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-anthropic-key"}):
        off = iModel.from_dict(iModel(provider="anthropic", model=MODEL).to_dict())
    assert off.endpoint.config.prompt_caching is False


# --- option on, declaration present ---


@pytest.mark.parametrize("endpoint", [_filtered, _unfiltered], ids=["filtered", "unfiltered"])
def test_option_on_places_the_stable_and_turn_marks(endpoint):
    payload, _ = endpoint(prompt_caching=True).create_payload(_request())

    assert payload["system"] == [{"type": "text", "text": SYSTEM, "cache_control": ONE_HOUR}]
    assert payload["messages"][-1] == {
        "role": "user",
        "content": [{"type": "text", "text": "second question", "cache_control": FIVE_MINUTES}],
    }
    assert payload["messages"][:-1] == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
    ]
    marks = _marks(payload)
    assert len(marks) <= 4
    assert marks == [ONE_HOUR, FIVE_MINUTES]


@pytest.mark.parametrize("endpoint", [_filtered, _unfiltered], ids=["filtered", "unfiltered"])
def test_option_on_keeps_the_option_out_of_the_body(endpoint):
    payload, _ = endpoint(prompt_caching=True).create_payload(
        _request(prompt_caching=True, system_blocks=["more stable text"])
    )

    assert "prompt_caching" not in payload
    assert "system_blocks" not in payload
    assert "prompt_caching" not in json.dumps(payload)


@pytest.mark.parametrize("endpoint", [_filtered, _unfiltered], ids=["filtered", "unfiltered"])
def test_option_on_replaces_the_cache_control_mark(endpoint):
    payload, _ = endpoint(prompt_caching=True).create_payload(_request(cache_control=True))

    assert "cache_control" not in payload
    assert _marks(payload) == [ONE_HOUR, FIVE_MINUTES]


@pytest.mark.parametrize("endpoint", [_filtered, _unfiltered], ids=["filtered", "unfiltered"])
def test_stable_blocks_follow_the_system_message_in_order(endpoint):
    payload, _ = endpoint(prompt_caching=True).create_payload(
        _request(system_blocks=["block one", "", "block two"])
    )

    assert payload["system"] == [
        {"type": "text", "text": SYSTEM},
        {"type": "text", "text": "block one"},
        {"type": "text", "text": "block two", "cache_control": ONE_HOUR},
    ]


def test_stable_blocks_without_a_system_message():
    request = _request(system_blocks=["only block"])
    request["messages"] = request["messages"][1:]

    payload, _ = _filtered(prompt_caching=True).create_payload(request)

    assert payload["system"] == [{"type": "text", "text": "only block", "cache_control": ONE_HOUR}]
    assert _marks(payload) == [ONE_HOUR, FIVE_MINUTES]


def test_no_stable_text_places_only_the_turn_mark():
    request = _request()
    request["messages"] = request["messages"][1:]

    payload, _ = _filtered(prompt_caching=True).create_payload(request)

    assert "system" not in payload
    assert _marks(payload) == [FIVE_MINUTES]


def test_a_block_list_turn_is_marked_on_its_last_block():
    request = _request()
    request["messages"][-1]["content"] = [
        {"type": "text", "text": "look at this"},
        {"type": "text", "text": "and this"},
    ]

    payload, _ = _filtered(prompt_caching=True).create_payload(request)

    assert payload["messages"][-1]["content"] == [
        {"type": "text", "text": "look at this"},
        {"type": "text", "text": "and this", "cache_control": FIVE_MINUTES},
    ]


def test_the_callers_messages_are_left_as_handed_in():
    request = _request()
    request["messages"][-1]["content"] = [{"type": "text", "text": "a block"}]
    before = copy.deepcopy(request["messages"][1:])

    _filtered(prompt_caching=True).create_payload(request)

    assert request["messages"][-1:] == before[-1:]
    assert "cache_control" not in request["messages"][-1]["content"][0]


@pytest.mark.parametrize("prompt_caching", [True, False], ids=["on", "off"])
def test_the_callers_request_is_left_as_handed_in(prompt_caching):
    """A request reused for a second payload, as a retry or a second call would, builds the same."""
    request = _request(system_blocks=["more stable text"])
    before = copy.deepcopy(request)
    endpoint = _filtered(prompt_caching=prompt_caching)

    first, _ = endpoint.create_payload(request)
    second, _ = endpoint.create_payload(request)

    assert request == before
    assert json.dumps(first) == json.dumps(second)


@pytest.mark.parametrize("prompt_caching", [True, False], ids=["on", "off"])
def test_a_marked_request_is_left_as_handed_in_on_the_unfiltered_path(prompt_caching):
    request = _request(cache_control=True)
    request["messages"][-1]["content"] = [{"type": "text", "text": "a block"}]
    before = copy.deepcopy(request)
    endpoint = _unfiltered(prompt_caching=prompt_caching)

    first, _ = endpoint.create_payload(request)
    second, _ = endpoint.create_payload(request)

    assert request == before
    assert json.dumps(first) == json.dumps(second)
    assert _marks(first)[-1] == FIVE_MINUTES


def test_a_blank_system_message_leaves_the_native_system_field_as_the_stable_text():
    request = _request(system="native system text", system_blocks=["more stable text"])
    request["messages"][0]["content"] = "   "

    payload, _ = _filtered(prompt_caching=True).create_payload(request)

    assert payload["system"] == [
        {"type": "text", "text": "native system text"},
        {"type": "text", "text": "more stable text", "cache_control": ONE_HOUR},
    ]


def test_a_blank_native_system_field_is_not_sent():
    request = _request(system="   ")
    request["messages"] = request["messages"][1:]

    payload, _ = _filtered(prompt_caching=True).create_payload(request)

    assert "system" not in payload
    assert _marks(payload) == [FIVE_MINUTES]


def test_a_blank_last_message_takes_no_turn_mark():
    request = _request()
    request["messages"][-1]["content"] = "   "

    payload, _ = _filtered(prompt_caching=True).create_payload(request)

    assert payload["messages"][-1]["content"] == "   "
    assert _marks(payload) == [ONE_HOUR]


def test_the_turn_mark_skips_a_blank_text_block():
    request = _request()
    request["messages"][-1]["content"] = [
        {"type": "text", "text": "visible"},
        {"type": "text", "text": "   "},
    ]

    payload, _ = _unfiltered(prompt_caching=True).create_payload(request)

    assert payload["messages"][-1]["content"] == [
        {"type": "text", "text": "visible", "cache_control": FIVE_MINUTES},
        {"type": "text", "text": "   "},
    ]


def test_no_one_hour_mark_follows_a_five_minute_mark_in_the_tools():
    tool = {"name": "lookup", "input_schema": {"type": "object"}, "cache_control": FIVE_MINUTES}

    payload, _ = _unfiltered(prompt_caching=True).create_payload(_request(tools=[tool]))

    assert "cache_control" not in payload["system"][-1]
    assert _marks(payload) == [FIVE_MINUTES]
    assert payload["tools"] == [tool]


def test_no_one_hour_mark_follows_a_five_minute_mark_in_the_stable_blocks():
    request = _request()
    request["messages"][0]["content"] = [
        {"type": "text", "text": "part one", "cache_control": FIVE_MINUTES},
        {"type": "text", "text": "part two"},
    ]

    payload, _ = _unfiltered(prompt_caching=True).create_payload(request)

    assert payload["system"] == [
        {"type": "text", "text": "part one", "cache_control": FIVE_MINUTES},
        {"type": "text", "text": "part two"},
    ]


def test_an_empty_cache_control_field_is_not_a_mark():
    request = _request()
    request["messages"] = [request["messages"][0]] + [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": [{"type": "text", "text": f"turn {i}", "cache_control": None}],
        }
        for i in range(5)
    ]

    payload, _ = _unfiltered(prompt_caching=True).create_payload(request)

    assert _marks(payload) == [ONE_HOUR, FIVE_MINUTES]


def test_marks_inside_a_tool_result_count_toward_the_four():
    request = _request()
    request["messages"][-1]["content"] = [
        {
            "type": "tool_result",
            "tool_use_id": "toolu_1",
            "content": [
                {"type": "text", "text": f"result {i}", "cache_control": FIVE_MINUTES}
                for i in range(3)
            ],
        },
        {"type": "text", "text": "and a question"},
    ]

    payload, _ = _unfiltered(prompt_caching=True).create_payload(request)

    marks = [b for b in _blocks_with_nested_content(payload) if b.get("cache_control") is not None]
    assert len(marks) == 4
    # The one free slot goes to the end of the turn, which outranks the stable segment.
    assert payload["messages"][-1]["content"][-1]["cache_control"] == FIVE_MINUTES
    assert "cache_control" not in payload["system"][-1]


def _blocks_with_nested_content(payload):
    blocks = list(payload.get("system") or [])
    for message in payload["messages"]:
        if isinstance(message["content"], list):
            for block in message["content"]:
                blocks.append(block)
                if isinstance(block.get("content"), list):
                    blocks.extend(block["content"])
    return blocks


def test_system_blocks_passed_beside_the_request_are_stable_blocks():
    payload, _ = _filtered(prompt_caching=True).create_payload(
        _request(), system_blocks=["more stable text"]
    )

    assert payload["system"] == [
        {"type": "text", "text": SYSTEM},
        {"type": "text", "text": "more stable text", "cache_control": ONE_HOUR},
    ]


def test_a_payload_built_again_from_itself_is_unchanged():
    """A streamed call builds its payload a second time from the first one."""
    endpoint = _filtered(prompt_caching=True)
    payload, _ = endpoint.create_payload(_request(system_blocks=["more stable text"]))

    again, _ = endpoint.create_payload(copy.deepcopy(payload))

    assert json.dumps(again) == json.dumps(payload)


def test_stable_text_with_nothing_visible_is_not_a_block():
    request = _request(system_blocks=["   ", "\n", "kept"])
    request["messages"][0]["content"] = "  "

    payload, _ = _filtered(prompt_caching=True).create_payload(request)

    assert payload["system"] == [{"type": "text", "text": "kept", "cache_control": ONE_HOUR}]


def test_a_single_string_of_system_blocks_is_one_block():
    payload, _ = _filtered(prompt_caching=True).create_payload(_request(system_blocks="abc"))

    assert payload["system"] == [
        {"type": "text", "text": SYSTEM},
        {"type": "text", "text": "abc", "cache_control": ONE_HOUR},
    ]


def test_a_block_list_system_message_keeps_its_blocks():
    request = _request()
    request["messages"][0]["content"] = [
        {"type": "text", "text": "part one"},
        {"type": "text", "text": "part two"},
    ]
    before = copy.deepcopy(request)

    payload, _ = _filtered(prompt_caching=True).create_payload(request)

    assert payload["system"] == [
        {"type": "text", "text": "part one"},
        {"type": "text", "text": "part two", "cache_control": ONE_HOUR},
    ]
    assert request == before


def test_a_native_system_field_is_the_stable_text():
    request = _request(system="native system text")
    request["messages"] = request["messages"][1:]

    payload, _ = _filtered(prompt_caching=True).create_payload(request)

    assert payload["system"] == [
        {"type": "text", "text": "native system text", "cache_control": ONE_HOUR}
    ]
    assert _marks(payload) == [ONE_HOUR, FIVE_MINUTES]


def test_the_turn_mark_skips_a_thinking_block():
    request = _request()
    request["messages"].append(
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "an answer"},
                {"type": "thinking", "thinking": "worked out", "signature": "sig"},
                {"type": "redacted_thinking", "data": "opaque"},
            ],
        }
    )

    payload, _ = _unfiltered(prompt_caching=True).create_payload(request)

    assert payload["messages"][-1]["content"] == [
        {"type": "text", "text": "an answer", "cache_control": FIVE_MINUTES},
        {"type": "thinking", "thinking": "worked out", "signature": "sig"},
        {"type": "redacted_thinking", "data": "opaque"},
    ]


def test_a_turn_of_thinking_alone_takes_no_mark():
    request = _request()
    request["messages"].append(
        {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "worked out", "signature": "sig"}],
        }
    )

    payload, _ = _unfiltered(prompt_caching=True).create_payload(request)

    assert _marks(payload) == [ONE_HOUR]


@pytest.mark.parametrize("caller_marks", [2, 3, 4])
def test_marks_the_caller_placed_count_toward_the_four(caller_marks):
    request = _request()
    request["messages"] = [request["messages"][0]] + [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": [{"type": "text", "text": f"turn {i}", "cache_control": FIVE_MINUTES}],
        }
        for i in range(caller_marks)
    ]
    request["messages"].append({"role": "user", "content": "last question"})

    payload, _ = _unfiltered(prompt_caching=True).create_payload(request)

    marks = _marks(payload)
    assert len(marks) == 4
    # The end of the turn takes the first free slot and the one-hour stable mark the next;
    # the stable mark sits ahead of every other mark in the request.
    assert ("cache_control" in payload["messages"][-1]["content"][-1]) is (caller_marks < 4)
    assert (marks[0] == ONE_HOUR) is (caller_marks < 3)


# --- option on, declaration absent ---


def test_a_provider_without_the_declaration_is_unchanged_by_the_option():
    with patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai-key"}):
        off = iModel(provider="openai", model="gpt-4.1-mini").endpoint
        on = iModel(provider="openai", model="gpt-4.1-mini", prompt_caching=True).endpoint
    request = {"messages": [{"role": "user", "content": "hello"}], "model": "gpt-4.1-mini"}

    assert on.places_prompt_cache is False
    assert json.dumps(on.create_payload(copy.deepcopy(request))[0]) == json.dumps(
        off.create_payload(copy.deepcopy(request))[0]
    )


# --- the chat path hands the endpoint the system text as its own block ---

RESPONSE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": MODEL,
    "content": [{"type": "text", "text": "an answer"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


async def _chat_payloads(first_intent=None, **config):
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-anthropic-key"}):
        model = iModel(provider="anthropic", model=MODEL, max_tokens=100, **config)
    branch = Branch(system=SYSTEM, chat_model=model)
    sent = []
    build = model.endpoint.create_payload

    def spy(*args, **kwargs):
        payload, headers = build(*args, **kwargs)
        sent.append(copy.deepcopy(payload))
        return payload, headers

    async def call(*_args, **_kwargs):
        return copy.deepcopy(RESPONSE)

    with (
        patch.object(model.endpoint, "create_payload", side_effect=spy),
        patch.object(model.endpoint, "call", side_effect=call),
    ):
        with cache_intent(first_intent) if first_intent else nullcontext():
            await branch.chat("first question")
        await branch.chat("second question")
    return sent


@pytest.mark.asyncio
async def test_a_chat_with_the_option_sends_the_system_text_as_its_own_block():
    sent = await _chat_payloads(prompt_caching=True)

    assert len(sent) == 2
    for payload in sent:
        assert SYSTEM in payload["system"][-1]["text"]
        assert payload["system"][-1]["cache_control"] == ONE_HOUR
        assert SYSTEM not in json.dumps(payload["messages"])
        assert _marks(payload) == [ONE_HOUR, FIVE_MINUTES]
    assert sent[0]["system"] == sent[1]["system"]


@pytest.mark.asyncio
async def test_a_chat_without_the_option_folds_the_system_text_as_before():
    sent = await _chat_payloads()

    assert len(sent) == 2
    for payload in sent:
        assert "system" not in payload
        assert SYSTEM in json.dumps(payload["messages"][0])
        assert _marks(payload) == []


@pytest.mark.asyncio
async def test_a_chat_its_caller_declares_final_writes_no_entry_at_the_end_of_its_turn():
    sent = await _chat_payloads(first_intent="final", prompt_caching=True)

    assert len(sent) == 2
    assert _marks(sent[0]) == [ONE_HOUR]
    assert sent[0]["system"][-1]["cache_control"] == ONE_HOUR
    # The declaration ends with its block: the next chat marks the end of its turn again.
    assert sent[1]["messages"][-1]["content"][-1]["cache_control"] == FIVE_MINUTES


# --- ADR-0127 D2: the tool set renders once, in the system block ---


def lookup_order(order_id: str) -> str:
    """Find an order by its id."""
    return order_id


class _Review(BaseModel):
    approved: bool


class _Summary(BaseModel):
    summary: str


def _texts(messages):
    """Each message as role and text, without the marks the adapter places on the last one."""
    out = []
    for message in messages:
        content = message["content"]
        if isinstance(content, list):
            content = "".join(block.get("text", "") for block in content)
        out.append((message["role"], content))
    return out


async def _operate_payloads(**config):
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-anthropic-key"}):
        model = iModel(provider="anthropic", model=MODEL, max_tokens=100, **config)
    branch = Branch(system=SYSTEM, chat_model=model, tools=[lookup_order])
    sent = []
    build = model.endpoint.create_payload

    def spy(*args, **kwargs):
        payload, headers = build(*args, **kwargs)
        sent.append(copy.deepcopy(payload))
        return payload, headers

    async def call(*_args, **_kwargs):
        return copy.deepcopy(RESPONSE)

    with (
        patch.object(model.endpoint, "create_payload", side_effect=spy),
        patch.object(model.endpoint, "call", side_effect=call),
    ):
        await branch.operate(
            instruction="review the order",
            response_format=_Review,
            actions=True,
            tools=True,
            skip_validation=True,
        )
        await branch.operate(
            instruction="review it again",
            response_format=_Review,
            actions=True,
            tools=True,
            skip_validation=True,
        )
        await branch.operate(
            instruction="summarize", response_format=_Summary, skip_validation=True
        )
    return sent


@pytest.mark.asyncio
async def test_the_tool_section_is_in_the_system_block_of_every_request():
    sent = await _operate_payloads(prompt_caching=True)

    assert len(sent) == 3
    for payload in sent:
        assert SYSTEM in payload["system"][0]["text"]
        assert "Find an order by its id." in payload["system"][-1]["text"]
        assert payload["system"][-1]["cache_control"] == ONE_HOUR
        assert json.dumps(payload).count("Find an order by its id.") == 1
    assert sent[0]["system"] == sent[1]["system"] == sent[2]["system"]


@pytest.mark.asyncio
async def test_each_request_is_a_prefix_of_the_next_when_the_last_call_changes_its_format():
    sent = await _operate_payloads(prompt_caching=True)

    for earlier, later in zip(sent, sent[1:], strict=False):
        assert _texts(later["messages"])[: len(earlier["messages"])] == _texts(earlier["messages"])
    assert "summary" in _texts(sent[2]["messages"])[-1][1]
    assert "approved" in _texts(sent[2]["messages"])[0][1]


@pytest.mark.asyncio
async def test_without_the_option_the_tools_render_in_the_turn_as_before():
    sent = await _operate_payloads()

    assert len(sent) == 3
    for payload in sent:
        assert "system" not in payload
    assert "Find an order by its id." in _texts(sent[0]["messages"])[-1][1]
    assert "Find an order by its id." not in _texts(sent[2]["messages"])[-1][1]
    assert "approved" not in _texts(sent[2]["messages"])[0][1]


@pytest.mark.parametrize("endpoint", [_filtered, _unfiltered], ids=["filtered", "unfiltered"])
@pytest.mark.parametrize("declares", [True, False], ids=["declaring", "not-declaring"])
def test_launch_settings_reach_endpoint_config_without_provider_policy_keys(
    endpoint, declares, monkeypatch
):
    from lionagi.cli.agent import _install_api_input_cap

    monkeypatch.setattr(AnthropicMessagesEndpoint, "caches_system_prefix", declares)
    branch = Branch(chat_model=iModel(endpoint=endpoint()))
    before = branch.chat_model.endpoint.create_payload(copy.deepcopy(_request()))[0]
    assert (
        _install_api_input_cap(branch, "none", tool_result_max_chars=4000, prompt_caching=True)
        is None
    )
    selected = branch.chat_model.endpoint
    assert selected.config.prompt_caching is True
    assert selected.places_prompt_cache is declares
    payload = selected.create_payload(copy.deepcopy(_request()))[0]
    assert not {
        "tool_result_max_chars",
        "context_fold_at",
        "context_fold_target",
        "context_fold_tail_margin",
        "prompt_caching",
    }.intersection(payload)
    # The request's own max_tokens is sent over the output limit the leg puts on the config.
    assert payload["max_tokens"] == 100
    if declares:
        assert _marks(payload) == [ONE_HOUR, FIVE_MINUTES]
    else:
        # Only the position of max_tokens in the payload moves; nothing else changes.
        assert payload == before
