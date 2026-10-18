# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Imported play timestamps use UTC and retain nullable fallbacks."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from lionagi.state import db as db_mod
from lionagi.state.db import StateDB
from lionagi.studio.services import shows as shows_mod

UTC_EPOCH = 1704067200.0
FIELDS = ("started_at", "ended_at", "merged_at")


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


async def _import_play(
    root: Path, timestamps: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> dict:
    shows = root / "shows"
    play = shows / "topic" / "play-001"
    play.mkdir(parents=True)
    (play / "_meta.json").write_text(json.dumps({"status": "pending", **timestamps}))
    database = root / "state.db"
    monkeypatch.setattr(shows_mod, "SHOWS_ROOT", shows)
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", database)
    counts = await shows_mod.import_shows()
    assert counts == {"shows_imported": 1, "plays_imported": 1}
    async with StateDB(database) as db:
        row = await db.fetch_one("SELECT started_at, ended_at, merged_at FROM plays")
    assert row is not None
    return row


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("2024-01-01T00:00:00", UTC_EPOCH, id="naive-utc"),
        pytest.param("1704067200", UTC_EPOCH, id="epoch-string"),
        pytest.param("20250101", 20250101.0, id="numeric-before-iso"),
    ],
)
async def test_show_import_timestamps_use_utc_and_accept_epoch_strings(
    raw: str,
    expected: float,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    non_utc_local_timezone: None,
) -> None:
    row = await _import_play(tmp_path / "valid", dict.fromkeys(FIELDS, raw), monkeypatch)
    assert row == dict.fromkeys(FIELDS, expected)
    row = await _import_play(tmp_path / "overflow", dict.fromkeys(FIELDS, "1e300"), monkeypatch)
    assert row == dict.fromkeys(FIELDS)


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("error_type", [OverflowError, OSError])
async def test_show_import_timestamp_range_errors_keep_null(
    field: str,
    error_type: type[Exception],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = dict.fromkeys(FIELDS, "1704067200")
    row = await _import_play(tmp_path / "valid", valid, monkeypatch)
    assert row == dict.fromkeys(FIELDS, UTC_EPOCH)
    original = shows_mod.coerce_created_at

    def outside_range(value):
        if value == "1e300":
            raise error_type("timestamp outside platform range")
        return original(value)

    monkeypatch.setattr(shows_mod, "coerce_created_at", outside_range)
    row = await _import_play(tmp_path / "overflow", {**valid, field: "1e300"}, monkeypatch)
    assert row == {**dict.fromkeys(FIELDS, UTC_EPOCH), field: None}
