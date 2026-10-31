# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The Anthropic messages endpoint sends an output limit when the request names none.

The messages API requires `max_tokens`. A model built with no limit used to fail every call in
validate_payload ("Invalid payload") before anything was sent, unless the code that built it
remembered to set one.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch
from lionhub.providers.anthropic.messages import (
    ANTHROPIC_DEFAULT_MAX_TOKENS,
    AnthropicMessagesEndpoint,
    CreateMessageRequest,
)

_MESSAGES = [{"role": "user", "content": "hi"}]


def _reply(text: str) -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


@pytest.fixture
def sent(monkeypatch) -> list[dict]:
    """The requests the endpoint would have sent, with the network call replaced."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    requests: list[dict] = []

    async def fake_call(self, request, *args, **kwargs):
        requests.append(request)
        return _reply("ok")

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", fake_call)
    return requests


@pytest.mark.asyncio
async def test_a_branch_on_an_anthropic_model_with_no_limit_sends_the_default(sent):
    branch = Branch(chat_model="anthropic/claude-haiku-5-5")

    assert await branch.chat("hi") == "ok"

    (request,) = sent
    assert request["max_tokens"] == ANTHROPIC_DEFAULT_MAX_TOKENS
    CreateMessageRequest.model_validate(request)


@pytest.mark.asyncio
async def test_a_limit_set_on_the_model_is_kept(sent):
    branch = Branch(
        chat_model=iModel(provider="anthropic", model="claude-haiku-5-5", max_tokens=1024)
    )

    await branch.chat("hi")

    assert sent[0]["max_tokens"] == 1024


@pytest.mark.asyncio
async def test_a_limit_set_on_the_call_is_kept(sent):
    branch = Branch(
        chat_model=iModel(provider="anthropic", model="claude-haiku-5-5", max_tokens=1024)
    )

    await branch.chat("hi", max_tokens=512)

    assert sent[0]["max_tokens"] == 512


@pytest.mark.parametrize(
    ("model_kwargs", "request_extra", "expected"),
    [
        ({}, {}, ANTHROPIC_DEFAULT_MAX_TOKENS),
        ({"max_tokens": 1024}, {}, 1024),
        ({}, {"max_tokens": 512}, 512),
        ({"max_tokens": 1024}, {"max_tokens": 512}, 512),
        # None names no limit, and the API refuses a request carrying it.
        ({}, {"max_tokens": None}, ANTHROPIC_DEFAULT_MAX_TOKENS),
        ({"max_tokens": 1024}, {"max_tokens": None}, 1024),
    ],
)
def test_the_payload_carries_the_named_limit_or_the_default(
    monkeypatch, model_kwargs, request_extra, expected
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    endpoint = iModel(provider="anthropic", model="claude-haiku-5-5", **model_kwargs).endpoint

    payload, _ = endpoint.create_payload({"messages": _MESSAGES, **request_extra})

    assert payload["max_tokens"] == expected


def test_a_keyword_limit_is_kept(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    endpoint = iModel(provider="anthropic", model="claude-haiku-5-5", max_tokens=1024).endpoint

    payload, _ = endpoint.create_payload({"messages": _MESSAGES}, max_tokens=256)

    assert payload["max_tokens"] == 256


def test_a_none_keyword_keeps_the_request_limit(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    endpoint = iModel(provider="anthropic", model="claude-haiku-5-5").endpoint

    payload, _ = endpoint.create_payload(
        {"messages": _MESSAGES, "max_tokens": 512}, max_tokens=None
    )

    assert payload["max_tokens"] == 512


class _Request(BaseModel):
    messages: list
    max_tokens: int | None = None


@pytest.mark.parametrize(
    ("model_kwargs", "expected"),
    [({}, ANTHROPIC_DEFAULT_MAX_TOKENS), ({"max_tokens": 1024}, 1024)],
)
def test_a_model_request_with_no_limit_keeps_the_model_limit_or_sends_the_default(
    monkeypatch, model_kwargs, expected
):
    """A request model's unset field dumps as None, which names no limit."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    endpoint = iModel(provider="anthropic", model="claude-haiku-5-5", **model_kwargs).endpoint

    payload, _ = endpoint.create_payload(_Request(messages=_MESSAGES))

    assert payload["max_tokens"] == expected


def test_the_default_is_not_written_into_the_model_config(monkeypatch):
    """A model saved or copied after a call carries only what its caller set."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    endpoint = iModel(provider="anthropic", model="claude-haiku-5-5").endpoint

    endpoint.create_payload({"messages": _MESSAGES})

    assert "max_tokens" not in endpoint.config.kwargs


class _Handed(Exception):
    """Stops the call once the endpoint has been handed its request."""


@pytest.mark.asyncio
async def test_a_cli_endpoint_is_handed_no_output_limit(monkeypatch):
    """The default lives in the messages endpoint: nothing between a Branch and a CLI
    endpoint adds one."""
    branch = Branch(chat_model="claude_code/haiku")
    endpoint = branch.chat_model.endpoint
    handed: list[dict] = []

    def record(self, request, extra_headers=None, **kwargs):
        request = request if isinstance(request, dict) else request.model_dump()
        handed.append({**request, **kwargs})
        raise _Handed

    monkeypatch.setattr(type(endpoint), "create_payload", record)

    # The chat path wraps the stop in its own error; what the endpoint was handed is the evidence.
    with pytest.raises(Exception):
        await branch.chat("hi")

    assert not isinstance(endpoint, AnthropicMessagesEndpoint)
    (request,) = handed
    assert "messages" in request
    assert "max_tokens" not in request
