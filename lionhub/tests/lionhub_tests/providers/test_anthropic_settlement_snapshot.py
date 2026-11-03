# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Settlement retains provider usage when its source event is later masked."""

from __future__ import annotations

import pytest

from lionhub.providers.anthropic.messages import _settlement_reading


@pytest.mark.parametrize("event_type", ("message_start", "message_delta"))
@pytest.mark.parametrize("usage_kind", ("missing", "null", "nested"))
def test_settlement_keeps_an_independent_usage_snapshot(event_type, usage_kind):
    usage = (
        {"input_tokens": 10, "cache_creation": {"ephemeral_5m_input_tokens": 4}}
        if usage_kind == "nested"
        else None
    )
    holder = {"model": "model-1", "id": "request-1"} if event_type == "message_start" else {}
    if usage_kind != "missing":
        holder["usage"] = usage
    event = (
        {"type": event_type, "message": holder}
        if event_type == "message_start"
        else {"type": event_type, **holder}
    )

    reading = _settlement_reading(event)

    if usage is not None:
        usage["input_tokens"] = "masked"
        usage["cache_creation"]["ephemeral_5m_input_tokens"] = "masked"
        usage["added_later"] = True
    expected_usage = (
        {"input_tokens": 10, "cache_creation": {"ephemeral_5m_input_tokens": 4}}
        if usage_kind == "nested"
        else None
    )
    expected = {"usage": expected_usage}
    if event_type == "message_start":
        expected = {"model": "model-1", "id": "request-1", **expected}
    assert reading == expected
    if usage is not None:
        assert reading["usage"] is not usage
        assert reading["usage"]["cache_creation"] is not usage["cache_creation"]
