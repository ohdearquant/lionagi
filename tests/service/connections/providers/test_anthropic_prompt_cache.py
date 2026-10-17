# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The prompt-caching option on the Anthropic messages endpoint (ADR-0127 D3, D5, D7)."""

import copy
import json
import os
from unittest.mock import patch

import pytest

from lionagi.providers.anthropic.messages import AnthropicMessagesEndpoint
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
    return [block["cache_control"] for block in blocks if "cache_control" in block]


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


async def _chat_payloads(**config):
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
