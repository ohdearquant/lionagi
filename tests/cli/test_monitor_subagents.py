# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`li monitor` shows how many machine-wide agent slots each pool holds against its cap,
in the table and in `--machine`, and shows nothing new on a machine with no caps."""

from __future__ import annotations

import pytest

from lionagi.cli.monitor import _format_subagent_line, machine_result
from lionagi.providers._subagent_admission import admit_subagent


@pytest.fixture()
def home(tmp_path, monkeypatch):
    from lionagi import _paths

    monkeypatch.setattr(_paths, "LIONAGI_HOME", tmp_path / "home")
    return tmp_path / "home"


def test_no_caps_and_no_slots_add_no_line(home):
    from lionagi.providers._subagent_admission import subagent_admission_status

    assert _format_subagent_line(subagent_admission_status({})) is None


@pytest.mark.asyncio
async def test_the_line_counts_each_capped_pool(home):
    from lionagi.providers._subagent_admission import subagent_admission_status

    env = {
        "LIONAGI_MAX_SUBAGENTS": "30",
        "LIONAGI_MAX_SUBAGENTS_HEAVY": "6",
        "LIONAGI_MAX_SUBAGENTS_CODEX": "4",
    }
    lease = await admit_subagent(engine="codex", workload="heavy", environ=env)
    try:
        line = _format_subagent_line(subagent_admission_status(env))
    finally:
        lease.release()
    assert line == "subagents: all 1/30, codex 1/4, heavy 1/6"


def test_an_unreadable_status_is_said_rather_than_hidden():
    assert "unreadable (PermissionError)" in _format_subagent_line({"error": "PermissionError"})


@pytest.mark.asyncio
async def test_the_machine_result_carries_the_pools(home, monkeypatch):
    monkeypatch.setenv("LIONAGI_MAX_SUBAGENTS", "2")
    lease = await admit_subagent(engine="claude", workload="light")
    try:
        result = machine_result([])
    finally:
        lease.release()
    assert result["subagents"]["caps"]["LIONAGI_MAX_SUBAGENTS"] == 2
    assert result["subagents"]["pools"]["all"]["held"] == 1
    assert result["subagents"]["pools"]["all"]["holders"][0]["workload"] == "light"
