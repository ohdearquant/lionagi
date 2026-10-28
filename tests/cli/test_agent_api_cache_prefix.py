# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""What a cached `li agent` leg on an Anthropic model sends from one request to the next.

A provider's prompt cache is read only where an earlier request wrote an entry for the same
bytes, so every request a leg sends is serialized here as the provider receives it, marks
included, and compared with the request before it. The endpoint's call is scripted, so no
request leaves the process.
"""

from __future__ import annotations

import copy
import json

import pytest

from lionagi.providers.anthropic.messages import AnthropicMessagesEndpoint
from tests.cli import test_agent_api_provider as _provider_tests
from tests.cli.test_agent_api_context_fold import _SUMMARY_PROMPT
from tests.cli.test_agent_api_provider import (
    _answer,
    _last_round,
    _last_user_text,
    _profile,
    _read_round,
)

leg = _provider_tests.leg  # the scripted-endpoint fixture the provider tests run on

_RECALLED = "RECALLED: the notes file was last edited by the release script"
# Keywords Endpoint.call consumes itself; the rest reach create_payload.
_CALL_ONLY = {"cache_control", "skip_payload_creation", "_routing_scope", "extra_headers"}


class _FirstTurnRecall:
    """A provider that answers only before the conversation has a response, as recall does."""

    name = "first-turn-recall"

    async def provide(self, branch, instruction):
        return _RECALLED if branch.msgs.last_response is None else None


def _capture(monkeypatch) -> list[dict]:
    """Record every conversation request as the provider receives it; summary requests apart."""
    scripted = AnthropicMessagesEndpoint.call
    sent: list[dict] = []

    async def call(self, request, *a, **kw):
        payload, _ = self.create_payload(
            request, **{k: v for k, v in kw.items() if k not in _CALL_ONLY}
        )
        if not _last_user_text(payload).startswith(_SUMMARY_PROMPT):
            sent.append(copy.deepcopy(payload))
        return await scripted(self, request, *a, **kw)

    monkeypatch.setattr(AnthropicMessagesEndpoint, "call", call)
    return sent


def _register_recall(monkeypatch) -> None:
    import lionagi.agent.factory as factory_mod

    built = factory_mod.create_agent

    async def create_agent(*args, **kwargs):
        branch = await built(*args, **kwargs)
        branch.providers.register(_FirstTurnRecall())
        return branch

    monkeypatch.setattr(factory_mod, "create_agent", create_agent)


def _blocks(message: dict) -> list:
    content = message.get("content")
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content)


def _unmarked(value):
    """The value without its cache marks, a message's string content read as its one text block
    (the provider reads the two forms as the same prompt)."""
    if isinstance(value, dict):
        if "role" in value and "content" in value:
            value = {**value, "content": _blocks(value)}
        return {k: _unmarked(v) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_unmarked(v) for v in value]
    return value


@pytest.mark.asyncio
async def test_a_first_turn_context_block_leaves_every_request_a_prefix_of_the_next(
    leg, monkeypatch
):
    sent = _capture(monkeypatch)
    _register_recall(monkeypatch)
    leg.replies = [_read_round("notes.txt"), _last_round(), _answer("the notes say hello")]

    result, _p, _b, status, _s = await leg.run(_profile(api_tools="read", prompt_caching=True))

    assert (result, status) == ("the notes say hello", "completed")
    assert len(sent) == 3
    for before, after in zip(sent, sent[1:]):
        assert _unmarked(after.get("system")) == _unmarked(before.get("system"))
        assert _unmarked(after.get("tools")) == _unmarked(before.get("tools"))
        kept = _unmarked(after["messages"][: len(before["messages"])])
        assert kept == _unmarked(before["messages"]), "a later request rewrote an earlier message"
    # The block is the conversation's: every request carries it, not only the first.
    assert all(_RECALLED in json.dumps(payload, ensure_ascii=False) for payload in sent)
