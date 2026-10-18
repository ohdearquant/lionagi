# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Timestamp conversion at the run and team import boundaries."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from lionagi.cli import state as state_mod
from lionagi.state import db as db_mod
from lionagi.state.db import StateDB

UTC_EPOCH = 1704067200.0
TIMESTAMPS = [
    pytest.param("2024-01-01T00:00:00", UTC_EPOCH, id="naive-utc"),
    pytest.param("1704067200", UTC_EPOCH, id="epoch-string"),
    pytest.param("20250101", 20250101.0, id="numeric-before-iso"),
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


@pytest.mark.parametrize(("raw", "expected"), TIMESTAMPS)
def test_run_import_timestamps_use_utc_and_accept_epoch_strings(
    raw: str, expected: float, tmp_path: Path, non_utc_local_timezone: None
) -> None:
    assert state_mod._derive_timestamps({"started_at": raw, "ended_at": raw}, tmp_path) == (
        expected,
        expected,
        False,
    )


@pytest.mark.parametrize("field", ["started_at", "ended_at"])
@pytest.mark.parametrize("error_type", [OverflowError, OSError])
def test_run_import_timestamp_range_errors_keep_filesystem_fallback(
    field: str,
    error_type: type[Exception],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Establish that this caller accepts epoch strings before checking its error path.
    valid = {"started_at": "1704067200", "ended_at": "1704067200"}
    assert state_mod._derive_timestamps(valid, tmp_path) == (UTC_EPOCH, UTC_EPOCH, False)
    original = state_mod.coerce_created_at

    def outside_range(value):
        if value == "1e300":
            raise error_type("timestamp outside platform range")
        return original(value)

    monkeypatch.setattr(state_mod, "coerce_created_at", outside_range)
    stat = tmp_path.stat()
    expected_start = getattr(stat, "st_birthtime", stat.st_ctime)
    result = state_mod._derive_timestamps({**valid, field: "1e300"}, tmp_path)
    if field == "started_at":
        assert result == (expected_start, UTC_EPOCH, False)
    else:
        assert result == (UTC_EPOCH, stat.st_mtime, True)


async def _import_team_message(
    root: Path, raw: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[float, float]:
    teams = root / "teams"
    teams.mkdir(parents=True)
    document = teams / "team.json"
    document.write_text(
        json.dumps(
            {
                "id": "team",
                "members": [],
                "messages": [
                    {"id": "message", "from": "sender", "content": "hello", "timestamp": raw}
                ],
            }
        )
    )
    file_time = document.stat().st_mtime
    database = root / "state.db"
    monkeypatch.setattr(state_mod, "RUNS_ROOT", root / "runs")
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", database)
    counts = await state_mod._import_teams()
    assert counts == {"teams": 1, "messages": 1, "skipped_teams": 0, "errors": 0}
    async with StateDB(database) as db:
        row = await db.fetch_one("SELECT created_at FROM team_messages WHERE id = ?", ("message",))
    assert row is not None
    return row["created_at"], file_time


@pytest.mark.parametrize(("raw", "expected"), TIMESTAMPS)
async def test_team_import_timestamps_use_utc_and_accept_epoch_strings(
    raw: str,
    expected: float,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    non_utc_local_timezone: None,
) -> None:
    created_at, _ = await _import_team_message(tmp_path / "valid", raw, monkeypatch)
    assert created_at == expected
    created_at, file_time = await _import_team_message(tmp_path / "overflow", "1e300", monkeypatch)
    assert created_at == file_time


@pytest.mark.parametrize("error_type", [OverflowError, OSError])
async def test_team_import_timestamp_range_errors_keep_file_time(
    error_type: type[Exception], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created_at, _ = await _import_team_message(tmp_path / "valid", "1704067200", monkeypatch)
    assert created_at == UTC_EPOCH

    def outside_range(value):
        raise error_type("timestamp outside platform range")

    monkeypatch.setattr(state_mod, "coerce_created_at", outside_range)
    created_at, file_time = await _import_team_message(tmp_path / "overflow", "1e300", monkeypatch)
    assert created_at == file_time
