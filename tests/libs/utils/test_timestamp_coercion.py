# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Timestamp coercion preserves epoch precedence and normalizes naive ISO values."""

from __future__ import annotations

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


def test_trailing_z_is_normalized_before_python_iso_parser(monkeypatch):
    class LegacyDatetime(datetime):
        @classmethod
        def fromisoformat(cls, value):
            # Python 3.10 does not accept the UTC designator itself.
            if value.endswith("Z"):
                raise ValueError("Invalid isoformat string")
            return super().fromisoformat(value)

    monkeypatch.setattr(_utils, "datetime", LegacyDatetime)
    assert _utils.coerce_created_at("2025-01-01T00:00:00Z") == datetime(
        2025, 1, 1, tzinfo=timezone.utc
    )


def test_compact_numeric_string_keeps_epoch_precedence():
    assert _utils.coerce_created_at("20250101") == datetime.fromtimestamp(20250101, tz=timezone.utc)
