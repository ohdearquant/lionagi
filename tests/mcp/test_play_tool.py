# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the playbook submit tool and the bounded-wait tool.

``jobs.submit`` is stubbed so nothing spawns; these assert on the flags the tool
hands it, which is where a playbook run would silently become a different run.
"""

from __future__ import annotations

import pytest

# The tool surface is defined with fastmcp, which lives in the ``mcp`` extra.
pytest.importorskip("fastmcp", reason="requires the 'mcp' extra")

from lionagi.mcp import jobs, server  # noqa: E402 — must follow the extra guard


@pytest.fixture
def captured(monkeypatch):
    seen: dict = {}

    def fake_submit(kind, flags, **kwargs):
        seen["kind"] = kind
        seen["flags"] = flags
        seen.update(kwargs)
        return {"run_id": "rid", "status": "running", "terminal": False, "outcome": None}

    monkeypatch.setattr(server.jobs, "submit", fake_submit)
    return seen


def test_server_is_named_lion():
    assert server.mcp.name == "lion"


def test_play_runs_the_playbook_through_the_flow_command(captured):
    server.submit_play(name="review", prompt="check the diff", team_mode=True, timeout=900)

    assert captured["kind"] == "play"
    assert jobs._KIND_ARGV["play"] == ["orchestrate", "flow"]
    flags = captured["flags"]
    assert flags[:2] == ["-p", "review"]  # the playbook, not a bare prompt
    assert "--team-mode" in flags and flags[flags.index("--team-mode") + 1] != "review"
    assert ["--timeout", "900"] == flags[flags.index("--timeout") : flags.index("--timeout") + 2]
    assert captured["prompt"] == "check the diff"
    assert captured["label"] == "review"


def test_play_passes_playbook_declared_args_through(captured):
    """A playbook's own args are flags on the run; they ride the escape hatch."""
    server.submit_play(name="adr", extra_args=["--target", "docs/adr"])
    assert captured["flags"][-2:] == ["--target", "docs/adr"]


def test_play_team_mode_takes_a_name(captured):
    server.submit_play(name="review", team_mode="reviewers", team_max_rounds=4)
    flags = captured["flags"]
    assert flags[flags.index("--team-mode") + 1] == "reviewers"
    assert flags[flags.index("--team-max-rounds") + 1] == "4"


def test_play_resume_replays_without_a_playbook(captured):
    server.submit_play(resume="20260725T000000-abcdef")
    flags = captured["flags"]
    assert "-p" not in flags
    assert flags[:2] == ["--resume", "20260725T000000-abcdef"]


def test_play_refuses_a_name_and_a_resume_together(captured):
    with pytest.raises(ValueError, match="not both"):
        server.submit_play(name="review", resume="20260725T000000-abcdef")
    assert captured == {}  # rejected before anything was submitted


def test_play_needs_a_playbook_or_a_resume(captured):
    with pytest.raises(ValueError, match="playbook name"):
        server.submit_play(prompt="do something")
    assert captured == {}


def test_play_rejects_a_bad_prompt_file_before_submitting(captured):
    with pytest.raises(ValueError, match="absolute path"):
        server.submit_play(name="review", prompt_file="relative.txt")
    assert captured == {}


async def test_job_wait_tool_forwards_to_the_engine(monkeypatch):
    seen: dict = {}

    async def fake_wait(run_ids, max_wait, poll_interval):
        seen.update(run_ids=run_ids, max_wait=max_wait, poll_interval=poll_interval)
        return {"runs": [], "all_terminal": True, "timed_out": False, "pending": []}

    monkeypatch.setattr(server.jobs, "wait", fake_wait)
    out = await server.job_wait(["a", "b"], max_wait=5, poll_interval=0.5)

    assert seen == {"run_ids": ["a", "b"], "max_wait": 5, "poll_interval": 0.5}
    assert out["all_terminal"] is True
