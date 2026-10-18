# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Duration grammar and validation at the mirror and monitor CLI boundaries."""

from __future__ import annotations

import argparse

import pytest

from lionagi.cli.mirror import _parse_window, _since_window
from lionagi.cli.monitor import _since_timestamp


@pytest.mark.parametrize(
    ("window", "seconds"),
    [
        ("120", 120.0),
        ("2.5", 2.5),
        ("1e2", 100.0),
        ("2.5m", 150.0),
        ("1e2m", 6000.0),
        (" 2.5h ", 9000.0),
    ],
    ids=["bare-int", "bare-float", "bare-exponent", "float-unit", "exponent-unit", "trimmed"],
)
def test_mirror_since_preserves_numeric_windows(window: str, seconds: float):
    assert _parse_window(window) == seconds
    assert _since_window(window) == seconds


@pytest.mark.parametrize(("window", "seconds"), [("30s", 30.0), ("1.5s", 1.5)])
def test_mirror_since_accepts_seconds_unit(window: str, seconds: float):
    assert _since_window(window) == seconds


@pytest.mark.parametrize(
    "window",
    ["0", "-1m", "nan", "inf", "1e999d"],
    ids=["zero", "negative", "nan", "infinity", "overflow"],
)
def test_mirror_since_rejects_non_positive_or_non_finite(window: str):
    assert _since_window("1m") == 60.0
    with pytest.raises(argparse.ArgumentTypeError, match="positive finite"):
        _since_window(window)


@pytest.mark.parametrize("window", ["1H", "2D", "3M"])
def test_mirror_since_rejects_uppercase_units(window: str):
    assert _since_window("1h") == 3600.0
    with pytest.raises(argparse.ArgumentTypeError, match="--since"):
        _since_window(window)


@pytest.mark.parametrize("window", ["", " "])
def test_mirror_empty_window_keeps_unbounded_sentinel(window: str):
    assert _parse_window(window) is None
    with pytest.raises(argparse.ArgumentTypeError, match="non-empty"):
        _since_window(window)


@pytest.mark.parametrize(
    ("window", "seconds"),
    [
        ("30s", 30),
        ("2m", 120),
        ("2h", 7200),
        ("2d", 172800),
        ("+2h", 7200),
        ("1_000s", 1000),
    ],
    ids=["seconds", "minutes", "hours", "days", "plus", "underscore"],
)
def test_monitor_since_preserves_integer_windows(monkeypatch, window: str, seconds: int):
    monkeypatch.setattr("lionagi.cli.monitor.time.time", lambda: 1_000_000.0)
    assert _since_timestamp(window) == 1_000_000.0 - seconds


@pytest.mark.parametrize("window", [" 2h ", "2h\n"], ids=["spaces", "newline"])
def test_monitor_since_trims_surrounding_whitespace(monkeypatch, window: str):
    monkeypatch.setattr("lionagi.cli.monitor.time.time", lambda: 100_000.0)
    assert _since_timestamp(window) == 92_800.0


@pytest.mark.parametrize("window", ["0s", "-1h"], ids=["zero", "negative"])
def test_monitor_since_rejects_non_positive_windows(window: str):
    with pytest.raises(ValueError, match="must be positive"):
        _since_timestamp(window)


@pytest.mark.parametrize("window", ["1H", "2M"])
def test_monitor_since_rejects_uppercase_units(window: str):
    with pytest.raises(ValueError, match="lowercase unit"):
        _since_timestamp(window)


@pytest.mark.parametrize("window", ["", " \t"], ids=["empty", "blank"])
def test_monitor_since_empty_window_has_value_error(window: str):
    with pytest.raises(ValueError, match="--since"):
        _since_timestamp(window)


@pytest.mark.parametrize("window", ["1.5h", "1e2h", "3600"], ids=["float", "exponent", "bare"])
def test_monitor_since_keeps_integer_unit_grammar(window: str):
    with pytest.raises(ValueError, match="--since"):
        _since_timestamp(window)


@pytest.mark.parametrize("window", ["10 s", "10\tm", "10\nh"])
def test_cli_duration_callers_reject_internal_whitespace(window):
    with pytest.raises(ValueError):
        _parse_window(window)
    with pytest.raises(ValueError):
        _since_timestamp(window)
