# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""CLI readers retain the identities and paths of active scheduler claims."""

from __future__ import annotations

import argparse

import pytest

from lionagi.cli import machine_schedule
from lionagi.studio import cli as schedule_cli

_HOLDERS = [
    {"schedule_id": "poll-project", "path": "github_poll", "acquired_at": 1700000000.0},
    {"schedule_id": "nightly", "path": "scheduled", "acquired_at": 1700000001.0},
]


def test_human_limits_displays_each_holder_and_acquisition_time(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        schedule_cli,
        "_api",
        lambda path: {
            "max_scheduled_concurrent": 4,
            "current_inflight": 2,
            "current_holders": _HOLDERS,
        },
    )

    assert schedule_cli._cmd_limits(argparse.Namespace()) == 0

    output = capsys.readouterr().out
    for holder in _HOLDERS:
        assert holder["schedule_id"] in output
        assert holder["path"] in output
        assert schedule_cli._fmt_rfc3339(holder["acquired_at"]) in output


@pytest.mark.parametrize("holders", (_HOLDERS, []))
def test_machine_limits_retains_current_holders(monkeypatch, holders) -> None:
    monkeypatch.setattr(
        machine_schedule,
        "_studio",
        lambda path: {
            "max_scheduled_concurrent": 4,
            "current_inflight": len(holders),
            "current_holders": holders,
        },
    )

    result = machine_schedule.dispatch_schedule(["limits"])

    assert result["current_holders"] == holders
    assert result["current_inflight"] == len(holders)
    assert result["max_scheduled_concurrent"] == 4


def test_limits_defaults_to_empty_holders_for_older_server(monkeypatch, capsys) -> None:
    monkeypatch.setattr(machine_schedule, "_studio", lambda path: {"current_inflight": 0})
    monkeypatch.setattr(schedule_cli, "_api", lambda path: {"current_inflight": 0})

    assert machine_schedule.dispatch_schedule(["limits"])["current_holders"] == []
    assert schedule_cli._cmd_limits(argparse.Namespace()) == 0
    assert "Current holders:          0" in capsys.readouterr().out
