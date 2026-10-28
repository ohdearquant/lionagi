# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The review verdict is the only request its synthesis branch makes, so it places no
end-of-turn cache mark (ADR-0127 Amendment 3, ``final``). A reviewer keeps its mark: a
repair replays the conversation onto the same branch and reads it. The Anthropic endpoint is
patched, so no request leaves the process."""

import copy
import json
import os
from unittest.mock import patch

import pytest

from lionagi.providers.anthropic.messages import AnthropicMessagesEndpoint
from lionhub.orchestration.engines import ReviewEngine

pytestmark = pytest.mark.asyncio

FIVE_MINUTES = {"type": "ephemeral"}
ONE_HOUR = {"type": "ephemeral", "ttl": "1h"}
REVIEW = "for **correctness** only"
VERDICT = "Issue a single ProposedVerdict"
ISSUE = {
    "issue_found": {
        "dimension": "correctness",
        "description": "an empty list raises IndexError",
        "severity": "minor",
        "location": "first.py:2",
        "confidence": 0.6,
    }
}


def _response(text: str) -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _blocks(message: dict) -> list[dict]:
    content = message["content"]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _text(payload: dict) -> str:
    message = payload["messages"][-1]
    if isinstance(message["content"], str):
        return message["content"]
    return "".join(block.get("text", "") for block in _blocks(message))


def _message_marks(payload: dict) -> list[dict]:
    return [
        block["cache_control"]
        for message in payload["messages"]
        for block in _blocks(message)
        if "cache_control" in block
    ]


async def test_the_verdict_request_places_no_end_of_turn_mark():
    sent: list[dict] = []
    build = AnthropicMessagesEndpoint.create_payload

    def spy(self, *args, **kwargs):
        payload, headers = build(self, *args, **kwargs)
        sent.append(copy.deepcopy(payload))
        return payload, headers

    async def call(self, *_args, **_kwargs):
        prompt = _text(sent[-1])
        if REVIEW in prompt:
            return _response(f"Done.\n```json\n{json.dumps(ISSUE)}\n```\n")
        assert VERDICT in prompt
        return _response("APPROVE-WITH-FIXES: guard the empty list.")

    engine = ReviewEngine(
        model="anthropic/claude-haiku-5-5",
        prompt_caching=True,
        khive_injection=False,
        dimensions=("correctness",),
        verify_clean=False,
    )
    with (
        patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-anthropic-key"}),
        patch.object(AnthropicMessagesEndpoint, "create_payload", spy),
        patch.object(AnthropicMessagesEndpoint, "call", call),
    ):
        run = engine.new_run()
        out = await engine._run(run, "def first(xs):\n    return xs[0]\n")

    assert "APPROVE-WITH-FIXES" in out
    review = [payload for payload in sent if REVIEW in _text(payload)]
    verdict = [payload for payload in sent if VERDICT in _text(payload)]
    assert len(review) == 1
    assert len(verdict) == 1
    # Control: the reviewer's request marks the end of its turn.
    assert FIVE_MINUTES in _message_marks(review[0])
    assert FIVE_MINUTES not in _message_marks(verdict[0])
    assert verdict[0]["system"][-1]["cache_control"] == ONE_HOUR
