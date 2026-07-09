"""Tests for cost.py's four-dimension token accounting.

Cache writes bill at a premium over uncached input (1.25x by default), never
at the cache-read discount. These tests pin the normalization split (Anthropic
cache_creation vs cache_read, OpenAI cached/write subsets of the input total),
the pricing rule (derived 1.25x default, explicit fourth price element,
three-element env-override back-compat), and the per-model aggregation slots.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness.cost import (  # noqa: E402
    CACHE_WRITE_MULT,
    Usage,
    _norm_tokens,
    _prices,
    cost_of,
)

_MODEL = "claude-code/sonnet"  # (3.00, 15.00, 0.30) per 1M


def test_norm_tokens_anthropic_splits_cache_write_from_read():
    u = {
        "input_tokens": 100,
        "cache_read_input_tokens": 400,
        "cache_creation_input_tokens": 300,
        "output_tokens": 50,
    }
    assert _norm_tokens(u) == (100, 400, 50, 300, False)


def test_norm_tokens_openai_write_is_subset_of_input_total():
    u = {
        "input_tokens": 1000,
        "cached_input_tokens": 600,
        "cache_creation_input_tokens": 100,
        "output_tokens": 20,
    }
    uncached, cached, out, write, is_codex = _norm_tokens(u)
    assert (uncached, cached, out, write) == (300, 600, 20, 100)
    assert is_codex


def test_norm_tokens_openai_without_write_key_unchanged():
    u = {"input_tokens": 1000, "cached_input_tokens": 600, "output_tokens": 20}
    assert _norm_tokens(u) == (400, 600, 20, 0, True)


def test_default_cache_write_price_is_1_25x_input():
    pin, _pout, _pcached, pwrite = _prices()[_MODEL]
    assert pwrite == pin * CACHE_WRITE_MULT


def test_cost_of_bills_cache_writes_at_premium():
    base = cost_of(0, 0, 0, _MODEL)
    with_writes = cost_of(0, 0, 0, _MODEL, cache_write_tokens=1_000_000)
    assert with_writes - base == 3.00 * CACHE_WRITE_MULT
    # and the premium exceeds what the same tokens would cost as plain input
    assert with_writes > cost_of(1_000_000, 0, 0, _MODEL)


def test_price_override_three_elements_derives_write_price(monkeypatch):
    monkeypatch.setenv("LIONAGI_BENCH_PRICES", json.dumps({"m": [2.0, 8.0, 0.2]}))
    assert _prices()["m"] == (2.0, 8.0, 0.2, 2.0 * CACHE_WRITE_MULT)


def test_price_override_fourth_element_is_explicit_write_price(monkeypatch):
    monkeypatch.setenv("LIONAGI_BENCH_PRICES", json.dumps({"m": [2.0, 8.0, 0.2, 4.0]}))
    assert _prices()["m"] == (2.0, 8.0, 0.2, 4.0)
    assert cost_of(0, 0, 0, "m", cache_write_tokens=1_000_000) == 4.0


def test_usage_add_and_cost_usd_include_cache_writes():
    usage = Usage()
    usage._add(_MODEL, 100, 200, 50, 300)
    assert usage.cache_write_tokens == 300
    assert usage.per_model[_MODEL] == [100, 200, 50, 300]
    assert usage.total_tokens == 650
    expected = cost_of(100, 200, 50, _MODEL, cache_write_tokens=300)
    assert usage.cost_usd(_MODEL) == expected


def test_usage_cost_usd_tolerates_legacy_three_slot_per_model():
    usage = Usage(per_model={_MODEL: [100, 200, 50]})
    assert usage.cost_usd(_MODEL) == cost_of(100, 200, 50, _MODEL)
