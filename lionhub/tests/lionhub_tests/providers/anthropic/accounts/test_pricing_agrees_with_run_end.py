# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""One rule reads a usage report's cache-write split for every consumer.

The run-end usage the core reports and the price the account engine computes read the same
split: a report that the run end marks not valid is one ``price_usage`` refuses, and an integer
split that the run end reports is priced with the same two lifetimes.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from lionagi.session.signal import _collect_branch_usage
from lionhub.providers.anthropic.accounts._pricing import CURRENT_REVISION, price_usage

MODEL = "claude-opus-5-5"
BOTH = frozenset({"5m", "1h"})


def _branch_with_usage_dicts(*usages):
    messages = []
    for usage in usages:
        msg = MagicMock()
        msg.metadata = {"model_response": {"usage": usage}}
        messages.append(msg)
    branch = MagicMock()
    branch.msgs.messages = messages
    return branch


@pytest.mark.parametrize(
    ("report", "lifetimes"),
    [
        pytest.param(
            {"input_tokens": 1, "cache_creation": {"ephemeral_1h_input_tokens": 300}},
            BOTH,
            id="split-without-its-total",
        ),
        pytest.param(
            {"input_tokens": 1, "cache_creation": "not a mapping"},
            BOTH,
            id="split-that-is-not-a-mapping",
        ),
        pytest.param(
            {
                "input_tokens": 3,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 1234,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": 1000.0,
                    "ephemeral_1h_input_tokens": 234,
                },
            },
            BOTH,
            id="integral-float-part",
        ),
        pytest.param(
            {
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 200.0,
            },
            frozenset({"5m"}),
            id="integral-float-total",
        ),
        pytest.param(
            {
                "input_tokens": 1,
                "output_tokens": 1,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 200.0,
                "cache_creation": {"ephemeral_5m_input_tokens": 200},
            },
            frozenset({"5m"}),
            id="integral-float-total-with-a-split",
        ),
    ],
)
def test_a_report_the_run_end_marks_not_valid_is_not_priced(report, lifetimes):
    usage = _collect_branch_usage(_branch_with_usage_dicts(dict(report)))
    assert usage["usage_valid"] is False
    assert price_usage(CURRENT_REVISION, lifetimes, MODEL, report) is None


def test_an_integer_split_prices_with_the_lifetimes_the_run_end_reports():
    report = {
        "input_tokens": 3,
        "output_tokens": 5,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 1234,
        "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 234},
    }
    usage = _collect_branch_usage(_branch_with_usage_dicts(dict(report)))
    assert (
        usage["cache_write_5m_tokens"],
        usage["cache_write_1h_tokens"],
        usage["usage_valid"],
    ) == (1000, 234, True)
    priced = price_usage(CURRENT_REVISION, BOTH, MODEL, report)
    assert priced is not None
    assert (priced.cache_write_5m_tokens, priced.cache_write_1h_tokens) == (1000, 234)
