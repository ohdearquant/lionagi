# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A request the host fold expects to fold after places no end-of-turn cache mark (ADR-0127
Amendment 4): the fold's decision at API_PRE_CALL, and what a chat then sends. The endpoint is
scripted, so no request leaves the process."""

import copy
import math
from unittest.mock import AsyncMock, patch

import pytest

from lionagi.agent.context_fold import ContextFold, install_context_fold
from lionagi.providers.anthropic import _prompt_cache as planner
from lionagi.service.imodel import iModel
from lionagi.session.branch import Branch

_CAP = 100_000
FIVE_MINUTES = {"type": "ephemeral"}
RESPONSE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-haiku-5-5",
    "content": [{"type": "text", "text": "an answer"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


def _haiku(*, places=True):
    return iModel(
        provider="anthropic",
        model="claude-haiku-5-5",
        api_key="test-not-a-key",
        max_tokens=64,
        max_input_tokens=_CAP,
        prompt_caching=places,
    )


def _fold(monkeypatch, estimates, **settings):
    """A fold at 0.9 of the cap whose estimates are scripted, one per call."""
    fold = ContextFold(Branch(chat_model=_haiku()), cap=_CAP, fold_at=0.9, target=0.6, **settings)
    values = iter(estimates)
    monkeypatch.setattr(fold, "estimate", lambda: next(values))
    return fold


@pytest.mark.parametrize(
    "estimate,skipped",
    [(85_499, False), (85_500, True), (89_999, True)],
)
@pytest.mark.asyncio
async def test_a_request_within_the_margin_below_the_fold_line_skips_its_turn_mark(
    monkeypatch, estimate, skipped
):
    fold = _fold(monkeypatch, [estimate])
    fold_call = AsyncMock(return_value=False)
    monkeypatch.setattr(fold, "fold", fold_call)

    await fold.on_api_pre_call()

    assert fold_call.await_count == 0
    expected = {"estimate": estimate, "fold_line": 90_000, "margin": 4_500} if skipped else None
    assert fold.branch._prompt_cache_tail_skip() == expected


@pytest.mark.parametrize(
    "estimates,landed,expected",
    [
        # A fold that brings the request well under the line: its turn is marked.
        ([91_000, 50_000], True, None),
        # A fold that leaves it near the line: the request as sent is the one judged.
        ([95_000, 88_000], True, 88_000),
        # A fold that lands, but not enough: still past the line, so the mark stays.
        ([95_000, 91_000], True, None),
    ],
)
@pytest.mark.asyncio
async def test_a_folded_request_is_judged_by_its_estimate_after_the_fold(
    monkeypatch, estimates, landed, expected
):
    fold = _fold(monkeypatch, estimates)
    fold_call = AsyncMock(return_value=landed)
    monkeypatch.setattr(fold, "fold", fold_call)

    await fold.on_api_pre_call()

    assert fold_call.await_count == 1
    decision = fold.branch._prompt_cache_tail_skip()
    assert (decision or {}).get("estimate") == expected


@pytest.mark.parametrize("estimate", [90_000, 92_000, 120_000])
@pytest.mark.asyncio
async def test_a_request_whose_fold_lands_nothing_keeps_its_turn_mark(monkeypatch, estimate):
    # At or past the line with nothing folded, the next request is no more likely to fold.
    fold = _fold(monkeypatch, [estimate])
    fold_call = AsyncMock(return_value=False)
    monkeypatch.setattr(fold, "fold", fold_call)

    await fold.on_api_pre_call()

    assert fold_call.await_count == 1
    assert fold.branch._prompt_cache_tail_skip() is None


@pytest.mark.parametrize("estimate", [89_999, 92_000])
@pytest.mark.asyncio
async def test_a_zero_margin_never_skips_a_turn_mark(monkeypatch, estimate):
    # 92,000 is past the line with a fold that lands nothing.
    fold = _fold(monkeypatch, [estimate], tail_margin=0)
    monkeypatch.setattr(fold, "fold", AsyncMock(return_value=False))

    await fold.on_api_pre_call()

    assert fold.branch._prompt_cache_tail_skip() is None


@pytest.mark.asyncio
async def test_the_fold_summary_request_clears_the_previous_decision(monkeypatch):
    from lionagi.tools.context.context import _automatic_summary

    fold = _fold(monkeypatch, [89_000])
    await fold.on_api_pre_call()
    assert fold.branch._prompt_cache_tail_skip() is not None

    token = _automatic_summary.set(True)
    try:
        await fold.on_api_pre_call()
    finally:
        _automatic_summary.reset(token)

    assert fold.branch._prompt_cache_tail_skip() is None


@pytest.mark.parametrize("margin", [-0.01, 0.9, 1.5, math.nan, math.inf, True, "0.045"])
def test_a_margin_outside_zero_to_the_fold_line_is_refused(margin):
    with pytest.raises(ValueError, match="tail_margin"):
        ContextFold(
            Branch(chat_model=_haiku()), cap=_CAP, fold_at=0.9, target=0.6, tail_margin=margin
        )


async def _chat_twice(monkeypatch, estimates, *, places=True):
    model = _haiku(places=places)
    branch = Branch(system="You review pull requests.", chat_model=model)
    fold = install_context_fold(branch, cap=_CAP, fold_at=0.9, target=0.6)
    values = iter(estimates)
    monkeypatch.setattr(fold, "estimate", lambda: next(values))
    sent, records = [], []
    build = model.endpoint.create_payload
    place = planner.place

    def spy(*args, **kwargs):
        payload, headers = build(*args, **kwargs)
        sent.append(copy.deepcopy(payload))
        return payload, headers

    def recording_place(payload, call):
        record = place(payload, call)
        records.append(record)
        return record

    async def call(*_args, **_kwargs):
        return copy.deepcopy(RESPONSE)

    with (
        patch.object(model.endpoint, "create_payload", side_effect=spy),
        patch.object(model.endpoint, "call", side_effect=call),
        patch.object(planner, "place", side_effect=recording_place),
    ):
        await branch.chat("first question")
        await branch.chat("second question")
    return sent, records


def _turn_marks(payload):
    content = payload["messages"][-1]["content"]
    blocks = content if isinstance(content, list) else []
    return [block.get("cache_control") for block in blocks if isinstance(block, dict)]


@pytest.mark.asyncio
async def test_a_chat_the_fold_expects_to_fold_after_sends_no_turn_mark(monkeypatch):
    sent, records = await _chat_twice(monkeypatch, [20_000, 89_000])

    assert len(sent) == 2
    assert FIVE_MINUTES in _turn_marks(sent[0])
    assert "tail_skip" not in records[0]
    assert all(mark is None for mark in _turn_marks(sent[1]))
    assert records[1]["tail_skip"] == {"estimate": 89_000, "fold_line": 90_000, "margin": 4_500}
    assert sent[1]["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert not any("tail" in mark["roles"] for mark in records[1]["marks"])


@pytest.mark.asyncio
async def test_a_chat_far_from_the_fold_line_keeps_its_turn_mark(monkeypatch):
    sent, records = await _chat_twice(monkeypatch, [20_000, 40_000])

    assert FIVE_MINUTES in _turn_marks(sent[1])
    assert "tail_skip" not in records[1]
