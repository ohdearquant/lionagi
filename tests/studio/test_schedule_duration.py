# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Schedule duration limits and representation survive shared duration parsing."""

from __future__ import annotations

import pytest

from lionagi.studio.services.schedule_declaration import Trigger, _resolve_trigger


@pytest.mark.parametrize(
    ("window", "seconds"),
    [
        ("30s", 30),
        ("15m", 900),
        ("6h", 21600),
        ("2d", 172800),
        (" 30d ", 2592000),
        ("2592000s", 2592000),
    ],
    ids=["seconds", "minutes", "hours", "days", "max-days", "max-seconds"],
)
def test_schedule_every_resolves_bounded_integer_seconds(window: str, seconds: int):
    resolved = _resolve_trigger(Trigger(every=window))
    assert resolved == {"kind": "every", "interval_sec": seconds, "raw": window}
    assert type(resolved["interval_sec"]) is int


@pytest.mark.parametrize(
    "window",
    ["+2h", "1_000s", "1.5h", "1H", "3600"],
    ids=["plus", "underscore", "float", "uppercase", "bare"],
)
def test_schedule_every_keeps_strict_digits_and_unit(window: str):
    assert _resolve_trigger(Trigger(every="2h"))["interval_sec"] == 7200
    with pytest.raises(ValueError, match="trigger.every"):
        _resolve_trigger(Trigger(every=window))


@pytest.mark.parametrize("window", ["31d", "2592001s"], ids=["days", "one-second-over"])
def test_schedule_every_rejects_above_maximum(window: str):
    assert _resolve_trigger(Trigger(every="30d"))["interval_sec"] == 2592000
    with pytest.raises(ValueError, match="30d"):
        _resolve_trigger(Trigger(every=window))


def test_schedule_every_rejects_zero():
    assert _resolve_trigger(Trigger(every="1s"))["interval_sec"] == 1
    with pytest.raises(ValueError, match="positive"):
        _resolve_trigger(Trigger(every="0s"))
