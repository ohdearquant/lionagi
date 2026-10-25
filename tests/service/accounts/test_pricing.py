# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Ceilings and settlement prices for routed Anthropic requests."""

import hashlib
import json
from fractions import Fraction

import pytest

from lionagi.service.accounts._pricing import (
    CURRENT_REVISION,
    freeze_request,
    price_usage,
    quote_ceiling,
    revision_by_id,
)
from lionagi.service.accounts._types import AccountRoutingError, RouterErrorCode

REV = CURRENT_REVISION


def _payload(**overrides):
    payload = {
        "model": "claude-opus-5-5",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": "hello"}],
        "service_tier": "standard_only",
    }
    payload.update(overrides)
    return payload


def _quote(**overrides):
    return quote_ceiling(REV, freeze_request(_payload(**overrides)))


def _refused(body: bytes) -> None:
    with pytest.raises(AccountRoutingError) as caught:
        quote_ceiling(REV, body)
    assert caught.value.code is RouterErrorCode.UNSUPPORTED_PRICING


@pytest.mark.parametrize(
    ("model", "rates"),
    [
        ("claude-fable-5-1", (10_000, 12_500, 20_000, 250, 50_000)),
        ("claude-opus-5-5", (4_000, 5_000, 8_000, 200, 20_000)),
        ("claude-sonnet-5-5", (2_000, 2_500, 4_000, 100, 10_000)),
    ],
)
def test_flat_rates_are_the_published_prices_in_nanodollars_per_token(model, rates):
    (tier,) = REV.models[model].tiers
    assert (tier.input, tier.write_5m, tier.write_1h, tier.read, tier.output) == rates


def test_the_small_model_has_two_tiers_split_at_one_hundred_thousand_prompt_tokens():
    low, high = REV.models["claude-haiku-5-5"].tiers
    assert low.max_prompt_tokens == 100_000 and high.max_prompt_tokens is None
    assert (low.input, low.write_5m, low.write_1h, low.read, low.output) == (100, 125, 200, 10, 500)
    assert (high.input, high.write_5m, high.write_1h, high.read, high.output) == (
        500,
        625,
        1000,
        50,
        2500,
    )


def test_a_ceiling_prices_the_whole_input_window_and_the_requested_output_at_the_regional_maximum():
    quote = _quote()
    # (1,000,000 * 4,000 + 4,096 * 20,000) * 1.1
    assert quote.ceiling_nusd == 4_490_112_000
    assert quote.requested_model == "claude-opus-5-5"
    assert quote.price_revision == REV.id


def test_a_ceiling_takes_the_write_rate_of_each_requested_ttl():
    no_cache = _quote(model="claude-fable-5-1", max_tokens=1000).ceiling_nusd
    five = _quote(
        model="claude-fable-5-1",
        max_tokens=1000,
        system=[{"type": "text", "text": "s", "cache_control": {"type": "ephemeral"}}],
    ).ceiling_nusd
    hour = _quote(
        model="claude-fable-5-1",
        max_tokens=1000,
        cache_control={"type": "ephemeral", "ttl": "1h"},
    ).ceiling_nusd
    assert no_cache == (1_000_000 * 10_000 + 1000 * 50_000) * 11 // 10
    assert five == (1_000_000 * 12_500 + 1000 * 50_000) * 11 // 10
    assert hour == 22_055_000_000


def test_a_tiered_ceiling_uses_the_highest_tier():
    quote = _quote(
        model="claude-haiku-5-5",
        max_tokens=100,
        messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}],
            }
        ],
    )
    assert quote.ceiling_nusd == (1_000_000 * 625 + 100 * 2500) * 11 // 10


def test_the_digest_names_the_exact_bytes_quoted():
    body = freeze_request(_payload())
    assert quote_ceiling(REV, body).request_digest == hashlib.sha256(body).hexdigest()
    assert json.loads(body) == _payload()


def test_a_quote_accepts_text_images_client_tools_and_returned_thinking():
    quote = _quote(
        system="be brief",
        tools=[
            {
                "name": "lookup",
                "description": "d",
                "input_schema": {"type": "object"},
                "cache_control": {"type": "ephemeral", "ttl": "5m"},
            }
        ],
        tool_choice={"type": "auto"},
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        metadata={"user_id": "u-1"},
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
                    },
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "t", "signature": "sig"},
                    {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [{"type": "text", "text": "r"}],
                    }
                ],
            },
        ],
    )
    assert quote.ceiling_nusd > 0


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"model": "claude-opus-5"}, id="model-not-in-revision"),
        pytest.param({"model": "claude-opus-5-5-latest"}, id="model-by-prefix"),
        pytest.param({"max_tokens": 0}, id="max-tokens-zero"),
        pytest.param({"max_tokens": 128_001}, id="max-tokens-over-limit"),
        pytest.param({"max_tokens": True}, id="max-tokens-bool"),
        pytest.param({"service_tier": "auto"}, id="priority-capacity-allowed"),
        pytest.param({"speed": "fast"}, id="fast-mode"),
        pytest.param({"inference_geo": "us"}, id="explicit-region"),
        pytest.param({"container": "c"}, id="container"),
        pytest.param(
            {"tools": [{"type": "web_search_20260209", "name": "web_search"}]}, id="server-tool"
        ),
        pytest.param(
            {"tools": [{"name": "t", "input_schema": {}, "defer_loading": True}]},
            id="unknown-tool-key",
        ),
        pytest.param({"cache_control": {"type": "ephemeral", "ttl": "24h"}}, id="unknown-ttl"),
        pytest.param({"cache_control": {"type": "persistent"}}, id="unknown-cache-type"),
        pytest.param({"thinking": {"type": "enabled", "budget_tokens": 10}}, id="manual-thinking"),
        pytest.param({"output_config": {"format": {}}}, id="unknown-output-config"),
        pytest.param({"tool_choice": {"type": "required"}}, id="unknown-tool-choice"),
        pytest.param(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "document", "source": {"type": "file", "file_id": "f"}}
                        ],
                    }
                ]
            },
            id="document-block",
        ),
        pytest.param(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "url", "url": "https://x"}}
                        ],
                    }
                ]
            },
            id="image-by-url",
        ),
        pytest.param(
            {
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "t", "citations": []}]}
                ]
            },
            id="unknown-block-key",
        ),
        pytest.param(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "t",
                                "content": [
                                    {"type": "tool_use", "id": "x", "name": "n", "input": {}}
                                ],
                            }
                        ],
                    }
                ]
            },
            id="tool-result-carrying-a-tool-call",
        ),
        pytest.param({"messages": [{"role": "system", "content": "x"}]}, id="system-role-message"),
        pytest.param({"messages": []}, id="no-messages"),
    ],
)
def test_anything_the_revision_does_not_price_refuses(overrides):
    _refused(freeze_request(_payload(**overrides)))


def test_a_request_that_omits_the_tier_refuses():
    payload = _payload()
    del payload["service_tier"]
    _refused(freeze_request(payload))


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            b'{"model":"claude-opus-5-5","model":"claude-fable-5-1","max_tokens":1,'
            b'"messages":[{"role":"user","content":"x"}],"service_tier":"standard_only"}',
            id="duplicate-key",
        ),
        pytest.param(
            b'{"model":"claude-opus-5-5","max_tokens":1,"temperature":NaN,'
            b'"messages":[{"role":"user","content":"x"}],"service_tier":"standard_only"}',
            id="non-finite-number",
        ),
        pytest.param(b"[]", id="not-an-object"),
        pytest.param(b"\xff", id="not-utf8"),
    ],
)
def test_a_body_that_two_parsers_could_read_differently_refuses(body):
    _refused(body)


def test_a_payload_that_cannot_be_encoded_exactly_refuses():
    with pytest.raises(AccountRoutingError):
        freeze_request(_payload(temperature=float("nan")))


def test_an_unknown_revision_refuses():
    assert revision_by_id(REV.id) is REV
    with pytest.raises(AccountRoutingError) as caught:
        revision_by_id("anthropic-19990101")
    assert caught.value.code is RouterErrorCode.UNSUPPORTED_PRICING


# --- settlement ----------------------------------------------------------------------------

_NONE = frozenset()
_FIVE = frozenset({"5m"})
_HOUR = frozenset({"1h"})


def test_settlement_prices_each_category_at_the_served_models_rates():
    usage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read_input_tokens": 2000,
        "cache_creation_input_tokens": 300,
        "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200},
        "inference_geo": "global",
        "service_tier": "standard",
        "speed": "standard",
        "output_tokens_details": {"thinking_tokens": 400},
        "server_tool_use": {"web_search_requests": 0, "web_fetch_requests": 0},
    }
    priced = price_usage(REV, frozenset({"5m", "1h"}), "claude-sonnet-5-5", usage, "req_1")
    assert priced is not None
    assert priced.cost_nusd == 1000 * 2000 + 2000 * 100 + 100 * 2500 + 200 * 4000 + 500 * 10_000
    assert (priced.cache_write_5m_tokens, priced.cache_write_1h_tokens) == (100, 200)
    assert priced.request_id == "req_1"
    # Thinking tokens are already inside output_tokens.
    del usage["output_tokens_details"]
    again = price_usage(REV, frozenset({"5m", "1h"}), "claude-sonnet-5-5", usage, "req_1")
    assert again is not None and again.cost_nusd == priced.cost_nusd


def test_a_cheaper_served_model_is_charged_its_own_rates():
    usage = {"input_tokens": 10, "output_tokens": 10}
    opus = price_usage(REV, _NONE, "claude-opus-5-5", usage)
    sonnet = price_usage(REV, _NONE, "claude-sonnet-5-5", usage)
    assert opus is not None and sonnet is not None
    assert (opus.cost_nusd, sonnet.cost_nusd) == (240_000, 120_000)


def test_the_tier_counts_cache_reads_and_writes_in_the_prompt_length():
    def cost(fresh, read, written):
        usage = {
            "input_tokens": fresh,
            "output_tokens": 0,
            "cache_read_input_tokens": read,
            "cache_creation_input_tokens": written,
        }
        priced = price_usage(REV, _FIVE, "claude-haiku-5-5", usage)
        assert priced is not None
        return priced.cost_nusd

    assert cost(40_000, 40_000, 20_000) == 40_000 * 100 + 40_000 * 10 + 20_000 * 125
    assert cost(40_001, 40_000, 20_000) == 40_001 * 500 + 40_000 * 50 + 20_000 * 625


def test_a_regional_multiplier_rounds_the_total_up_once():
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 1,
    }
    priced = price_usage(REV, _FIVE, "claude-haiku-5-5", {**usage, "inference_geo": "us"})
    # 125 * 1.1 = 137.5 nanodollars
    assert priced is not None and priced.cost_nusd == 138
    assert REV.geo_multipliers["us"] == Fraction(11, 10)


def test_a_write_without_a_split_is_attributed_only_when_one_ttl_was_asked_for():
    usage = {
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 10,
    }
    five = price_usage(REV, _FIVE, "claude-opus-5-5", usage)
    hour = price_usage(REV, _HOUR, "claude-opus-5-5", usage)
    assert five is not None and five.cache_write_5m_tokens == 10
    assert hour is not None and hour.cache_write_1h_tokens == 10
    assert price_usage(REV, frozenset({"5m", "1h"}), "claude-opus-5-5", usage) is None
    assert price_usage(REV, _NONE, "claude-opus-5-5", usage) is None


def test_omitted_cache_counts_are_zero_only_when_the_request_could_not_cache():
    usage = {"input_tokens": 5, "output_tokens": 5}
    assert price_usage(REV, _NONE, "claude-opus-5-5", usage) is not None
    assert price_usage(REV, _FIVE, "claude-opus-5-5", usage) is None
    # Each omitted count on its own.
    no_writes = {**usage, "cache_read_input_tokens": 0}
    no_reads = {**usage, "cache_creation_input_tokens": 0}
    assert price_usage(REV, _NONE, "claude-opus-5-5", no_writes) is not None
    assert price_usage(REV, _FIVE, "claude-opus-5-5", no_writes) is None
    assert price_usage(REV, _FIVE, "claude-opus-5-5", no_reads) is None


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"input_tokens": None}, id="missing-input"),
        pytest.param({"output_tokens": -1}, id="negative-output"),
        pytest.param({"output_tokens": True}, id="bool-count"),
        pytest.param({"output_tokens": 1.0}, id="float-count"),
        pytest.param({"cache_creation_input_tokens": 3}, id="split-does-not-add-up"),
        pytest.param({"service_tier": "priority"}, id="priority-tier"),
        pytest.param({"service_tier": "batch"}, id="batch-tier"),
        pytest.param({"speed": "fast"}, id="fast-speed"),
        pytest.param({"inference_geo": "eu"}, id="unpriced-region"),
        pytest.param({"server_tool_use": {"web_search_requests": 1}}, id="server-tool-charge"),
        pytest.param({"cache_creation": {"ephemeral_24h_input_tokens": 0}}, id="unknown-split-key"),
        pytest.param({"cache_storage_hours": 1}, id="unknown-billing-field"),
    ],
)
def test_incomplete_or_unpriced_usage_leaves_the_cost_unknown(change):
    usage = {
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 2,
        "cache_creation": {"ephemeral_5m_input_tokens": 2, "ephemeral_1h_input_tokens": 0},
    }
    assert price_usage(REV, _FIVE, "claude-opus-5-5", usage) is not None
    usage.update(change)
    assert price_usage(REV, _FIVE, "claude-opus-5-5", usage) is None


@pytest.mark.parametrize("model", ["claude-opus-5", "claude-opus-5-5-20261001", None, 5])
def test_an_unpriced_served_model_leaves_the_cost_unknown(model):
    assert price_usage(REV, _NONE, model, {"input_tokens": 1, "output_tokens": 1}) is None
