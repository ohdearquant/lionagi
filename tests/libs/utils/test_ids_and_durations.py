# Copyright (c) 2025 - 2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Public short identifiers and compact positive duration parsing."""

from __future__ import annotations

import re
from uuid import UUID

import pytest

from lionagi import ln
from lionagi.ln import _utils


def test_short_id_default_and_public_export(monkeypatch):
    value = UUID("12345678-1234-4abc-8def-123456789abc")
    monkeypatch.setattr(_utils.uuid, "uuid4", lambda: value)
    assert ln.short_id() == "123456781234"
    assert "short_id" in ln.__all__


@pytest.mark.parametrize("length", [1, 6, 8, 10, 12, 16, 32])
def test_short_id_lengths(length):
    value = ln.short_id(length)
    assert len(value) == length
    assert re.fullmatch("[0-9a-f]+", value)
    if length == 32:
        assert UUID(hex=value).version == 4


@pytest.mark.parametrize("length", [0, -1, 33, True, False, 1.0, "12", None])
def test_short_id_rejects_invalid_length(length):
    with pytest.raises(ValueError, match="integer.*1.*32"):
        ln.short_id(length)


@pytest.mark.parametrize(
    ("spec", "seconds"),
    [("1s", 1.0), ("2m", 120.0), ("3h", 10800.0), ("4d", 345600.0), (" 5s ", 5.0)],
)
def test_duration_units_and_public_export(spec, seconds):
    result = ln.parse_duration(spec)
    assert result == seconds
    assert isinstance(result, float)
    assert "parse_duration" in ln.__all__


@pytest.mark.parametrize(
    "spec", ["", "bad", "1w", "1M", "1H", "1D", "1S", "1", "1.5m", "1e2s", None, 3]
)
def test_duration_rejects_malformed_or_disabled_forms(spec):
    with pytest.raises(ValueError):
        ln.parse_duration(spec)


@pytest.mark.parametrize(
    "spec",
    ["0s", "-1s", "0m", "-2h", "0d", "0.0s", "nan", "inf", "-inf", "NaNm", "1e309s", "1e308d"],
)
def test_duration_rejects_non_positive_and_non_finite(spec):
    with pytest.raises(ValueError):
        ln.parse_duration(spec, allow_bare_seconds=True, allow_float=True)


@pytest.mark.parametrize(
    ("spec", "options", "seconds"),
    [
        ("12", {"allow_bare_seconds": True}, 12.0),
        ("1.5m", {"allow_float": True}, 90.0),
        (".5h", {"allow_float": True}, 1800.0),
        ("1e2s", {"allow_float": True}, 100.0),
        ("1.5", {"allow_float": True, "allow_bare_seconds": True}, 1.5),
    ],
)
def test_duration_options(spec, options, seconds):
    assert ln.parse_duration(spec, **options) == seconds


@pytest.mark.parametrize(
    ("spec", "options"),
    [("1.5", {"allow_bare_seconds": True}), ("1.5", {"allow_float": True})],
)
def test_duration_options_are_independent(spec, options):
    with pytest.raises(ValueError):
        ln.parse_duration(spec, **options)


def test_duration_maximum_includes_boundary():
    assert ln.parse_duration("30d", max_seconds=2592000) == 2592000.0
    with pytest.raises(ValueError, match="maximum"):
        ln.parse_duration("31d", max_seconds=2592000)


@pytest.mark.parametrize("maximum", [float("nan"), float("inf"), -1, 0])
def test_duration_rejects_invalid_maximum(maximum):
    with pytest.raises(ValueError, match="max_seconds"):
        ln.parse_duration("1s", max_seconds=maximum)


def test_duration_rejects_unrepresentable_integer():
    with pytest.raises(ValueError):
        ln.parse_duration("9" * 400 + "d")
