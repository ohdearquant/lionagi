# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Timestamp coercion preserves epoch precedence and normalizes naive ISO values."""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from lionagi.ln import _utils


@pytest.mark.parametrize("value", [True, False])
def test_boolean_is_not_an_epoch(value):
    with pytest.raises(ValueError, match="bool"):
        _utils.coerce_created_at(value)


@pytest.mark.parametrize("value", ["2025-01-01", "2025-01-01T00:00:00"])
def test_naive_iso_string_is_utc(value):
    assert _utils.coerce_created_at(value) == datetime(2025, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("suffix", ["Z", "z"])
@pytest.mark.parametrize("fraction", ["", ".1", ".12", ".123", ".1234", ".123456", ".1234567"])
def test_trailing_z_is_normalized_before_python_iso_parser(monkeypatch, suffix, fraction):
    class LegacyDatetime(datetime):
        @classmethod
        def fromisoformat(cls, value):
            # Python 3.10 does not accept the UTC designator itself.
            if value.endswith(("Z", "z")):
                raise ValueError("Invalid isoformat string")
            fractional = re.search(r"\.(\d+)", value)
            if fractional and len(fractional[1]) not in (3, 6):
                raise ValueError("Invalid isoformat string")
            return super().fromisoformat(value)

    monkeypatch.setattr(_utils, "datetime", LegacyDatetime)
    assert _utils.coerce_created_at(f"2025-01-01T00:00:00{fraction}{suffix}") == datetime(
        2025, 1, 1, microsecond=int((fraction[1:] + "000000")[:6]), tzinfo=timezone.utc
    )


def test_compact_numeric_string_keeps_epoch_precedence():
    assert _utils.coerce_created_at("20250101") == datetime.fromtimestamp(20250101, tz=timezone.utc)


@pytest.mark.parametrize("value", ["1e300", "inf", "-inf", "nan", "-1e300"])
def test_out_of_range_numeric_string_raises_value_error(value):
    with pytest.raises(ValueError):
        _utils.coerce_created_at(value)


@pytest.mark.parametrize("exception", [OverflowError, OSError])
def test_platform_range_error_is_normalized(monkeypatch, exception):
    class RangeLimitedDatetime(datetime):
        @classmethod
        def fromtimestamp(cls, value, tz=None):
            raise exception("outside platform range")

    monkeypatch.setattr(_utils, "datetime", RangeLimitedDatetime)
    with pytest.raises(ValueError):
        _utils.coerce_created_at("1e300")
