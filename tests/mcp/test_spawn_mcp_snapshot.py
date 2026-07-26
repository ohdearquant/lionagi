# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A detached leg's MCP server set is fixed by the submission, not re-read later.

Popen is doubled so no real `li` process is spawned; the tests read the argv the
engine built and resolve it the way the child would.
"""

from __future__ import annotations

import json

import pytest

from lionagi.cli._mcp_resolve import McpConfigError, resolve_spawn_mcp_servers
from lionagi.mcp import config, jobs


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "li_command", lambda: ["echo"])
    monkeypatch.setattr(jobs, "_read_lifecycle", lambda run_id: None)
    return tmp_path


class _FakeProc:
    def __init__(self, pid: int = 4242) -> None:
        self.pid = pid


@pytest.fixture
def submit_dir(monkeypatch, tmp_path):
    """A submitting directory holding its own .mcp.json, with no config above it.

    The search walks to the filesystem root, so an ancestor's real .mcp.json
    would otherwise decide these tests. A HOME-shaped tmp dir is used as the
    launch dir and the walk stops at the file planted here.
    """
    d = tmp_path / "submit"
    d.mkdir()
    monkeypatch.chdir(d)
    return d


def _config_arg(argv: list[str]) -> str:
    return argv[argv.index("--mcp-config") + 1]


def test_spawned_leg_keeps_the_server_set_the_submission_resolved(sandbox, submit_dir, monkeypatch):
    """The source config is replaced after submit; the leg still gets S1.

    This is the whole guarantee: the tool surface a leg starts with is a
    property of the submission, and an edit to the file afterwards belongs to
    the next submission, not to a run already under way.
    """
    source = submit_dir / ".mcp.json"
    source.write_text(json.dumps({"mcpServers": {"s1": {"command": "one"}}}))

    captured: dict = {}

    def fake_popen(argv, **kw):
        captured["argv"] = argv
        return _FakeProc()

    monkeypatch.setattr(jobs.subprocess, "Popen", fake_popen)
    handle = jobs.submit("agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir))

    # The submission is assembled and the child is spawned. Now the file moves.
    source.write_text(json.dumps({"mcpServers": {"s2": {"command": "two"}}}))

    seen = resolve_spawn_mcp_servers(_config_arg(captured["argv"]), launch_dir=submit_dir)
    assert set(seen.servers) == {"s1"}
    assert handle["mcp_config_reason"] is None


def test_spawned_leg_survives_the_source_config_being_removed(sandbox, submit_dir, monkeypatch):
    """Deletion is the same failure with a louder symptom: the leg would start
    with no servers at all, or fail resolving a path that no longer exists."""
    source = submit_dir / ".mcp.json"
    source.write_text(json.dumps({"mcpServers": {"s1": {"command": "one"}}}))

    captured: dict = {}
    monkeypatch.setattr(
        jobs.subprocess,
        "Popen",
        lambda argv, **kw: (captured.update(argv=argv), _FakeProc())[1],
    )
    jobs.submit("agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir))

    source.unlink()

    seen = resolve_spawn_mcp_servers(_config_arg(captured["argv"]), launch_dir=submit_dir)
    assert set(seen.servers) == {"s1"}


def test_submission_fails_when_the_discovered_config_cannot_be_used(
    sandbox, submit_dir, monkeypatch
):
    """An unusable config is refused at submit rather than handed to the child.

    A child that discovers the problem reports it in its own log, minutes later
    and only to whoever reads that log; the submitter is told the run started.
    """
    (submit_dir / ".mcp.json").write_text("{not json")

    spawned: list = []
    monkeypatch.setattr(
        jobs.subprocess, "Popen", lambda argv, **kw: (spawned.append(argv), _FakeProc())[1]
    )

    with pytest.raises(McpConfigError) as exc:
        jobs.submit("agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir))

    assert "mcp" in str(exc.value).lower()
    assert spawned == []
    # Rejected before anything was written: no half-made job record is left to
    # read back as a run that never finishes.
    assert not (config.JOBS_DIR).exists() or list(config.JOBS_DIR.iterdir()) == []


def test_no_config_anywhere_is_reported_not_raised(sandbox, submit_dir, monkeypatch):
    """Nothing configured is a choice, not a failure — the run still starts and
    the handle says why it has no servers."""
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda argv, **kw: _FakeProc())
    monkeypatch.setattr("lionagi.cli._mcp_resolve.discover_mcp_config", lambda start: None)

    handle = jobs.submit("agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir))
    assert handle["mcp_config"] is None
    assert handle["mcp_config_reason"]
