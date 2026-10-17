# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Timestamp coercion at both transcript mirror boundaries."""

from __future__ import annotations

import time
from collections.abc import Iterator
from types import ModuleType

import pytest

from lionagi.state import claude_mirror, codex_mirror

UTC_EPOCH = 1704067200.0
MIRRORS = [
    pytest.param(claude_mirror, id="claude"),
    pytest.param(codex_mirror, id="codex"),
]


@pytest.fixture()
def non_utc_local_timezone(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    if not hasattr(time, "tzset"):
        pytest.skip("The non-UTC local-time regression requires tzset")
    try:
        with monkeypatch.context() as patch:
            patch.setenv("TZ", "Etc/GMT+7")
            time.tzset()
            yield
    finally:
        time.tzset()


@pytest.mark.parametrize("mirror", MIRRORS)
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("2024-01-01T00:00:00", UTC_EPOCH, id="naive-utc"),
        pytest.param("1704067200", UTC_EPOCH, id="epoch-string"),
        pytest.param("20250101", 20250101.0, id="numeric-before-iso"),
        pytest.param(1704067200, UTC_EPOCH, id="epoch-int"),
    ],
)
def test_mirror_timestamps_use_utc_and_accept_epoch_values(
    mirror: ModuleType, raw: str | int, expected: float, non_utc_local_timezone: None
) -> None:
    assert mirror._ts(raw) == expected
    assert mirror._ts("1e300") is None


@pytest.mark.parametrize("mirror", MIRRORS)
@pytest.mark.parametrize("error_type", [OverflowError, OSError])
def test_mirror_timestamp_range_errors_keep_none(
    mirror: ModuleType, error_type: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert mirror._ts("1704067200") == UTC_EPOCH

    def outside_range(value):
        raise error_type("timestamp outside platform range")

    monkeypatch.setattr(mirror, "coerce_created_at", outside_range)
    assert mirror._ts("1e300") is None
