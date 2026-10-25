# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A detached leg's MCP server set is fixed by the submission, not re-read later.

Popen is doubled so no real `li` process is spawned; the tests read the argv the
engine built and resolve it the way the child would.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from lionagi.cli._mcp_resolve import McpConfigError, discover_mcp_config, resolve_spawn_mcp_servers
from lionhub.serve.mcp import config, dispatch, jobs


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


def _child_config(argv: list[str]) -> str | None:
    """The config the child's parser would settle on, in either spelling.

    argparse keeps the last occurrence, so a line carrying two of these does not
    fail — it quietly picks one. Reading it the same way is what lets a test say
    which file the child actually opens rather than which one appears first.
    """
    seen: list[str] = []
    for i, tok in enumerate(argv):
        if tok == "--mcp-config" and i + 1 < len(argv):
            seen.append(argv[i + 1])
        elif tok.startswith("--mcp-config="):
            seen.append(tok.split("=", 1)[1])
    return seen[-1] if seen else None


def _spawn_agent(args: dict) -> dict:
    """Drive the real spawn verb, fingerprint fetched the way a caller must."""
    return _spawn_verb("agent.submit", args)


def _spawn_verb(op: str, args: dict, *, expect_refusal: bool = False) -> dict:
    """Drive a real spawn verb, fingerprint fetched the way a caller must."""
    fingerprint = asyncio.run(dispatch.request(help=op))["schema_fingerprint"]
    answer = asyncio.run(
        dispatch.request(ops=[{"op": op, "args": args, "schema_fingerprint": fingerprint}])
    )
    op = answer["ops"][0]
    if expect_refusal:
        return op
    assert op["ok"], op
    return op["result"]


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


@pytest.mark.parametrize("op", ("agent.submit", "flow.submit", "fanout.submit"))
def test_every_spawn_verb_accepts_a_server_exclusion(sandbox, submit_dir, monkeypatch, op):
    """The parameter is part of each spawn verb's surface, not only the agent's."""
    (submit_dir / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"lion": {"command": "li"}, "khive": {"command": "kk"}}})
    )
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *a, **kw: _FakeProc())

    result = _spawn_verb(
        op,
        {
            "query": ["a-model"],
            "prompt": "hi",
            "cwd": str(submit_dir),
            "exclude_mcp_servers": ["lion"],
        },
    )

    assert result["declared_mcp_servers"] == ["khive"]
    assert result["excluded_mcp_servers"] == ["lion"]


@pytest.mark.parametrize("op", ("agent.submit", "flow.submit", "fanout.submit", "play.submit"))
def test_every_spawn_verb_declares_the_server_exclusion_parameter(op):
    """The parameter belongs to all four spawn surfaces, including the play one."""
    schema = asyncio.run(dispatch.request(help=op))["schema"]
    assert "exclude_mcp_servers" in schema["properties"]


def test_dispatch_reports_an_unknown_server_exclusion_as_invalid_input(
    sandbox, submit_dir, monkeypatch
):
    (submit_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {"khive": {"command": "kk"}}}))
    spawned: list = []
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *a, **kw: spawned.append(a))

    answer = _spawn_verb(
        "agent.submit",
        {
            "query": ["a-model"],
            "prompt": "hi",
            "cwd": str(submit_dir),
            "exclude_mcp_servers": ["missing"],
        },
        expect_refusal=True,
    )

    assert answer["ok"] is False
    assert answer["error"]["kind"] == "invalid_input"
    assert "not declared" in answer["error"]["message"] and "missing" in answer["error"]["message"]
    assert spawned == []
    assert not config.JOBS_DIR.exists() or list(config.JOBS_DIR.iterdir()) == []


@pytest.mark.parametrize(
    ("declared", "reason"),
    [(None, "no_mcp_config_found"), ({}, "mcp_config_declares_no_servers")],
)
def test_dispatch_refuses_an_exclusion_when_no_server_set_resolves(
    sandbox, submit_dir, monkeypatch, declared, reason
):
    """Nothing resolved means nothing to remove, so the exclusion cannot be honoured."""
    if declared is None:
        monkeypatch.setattr("lionagi.cli._mcp_resolve.discover_mcp_config", lambda start: None)
    else:
        (submit_dir / ".mcp.json").write_text(json.dumps({"mcpServers": declared}))
    spawned: list = []
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda *a, **kw: spawned.append(a))

    answer = _spawn_verb(
        "agent.submit",
        {
            "query": ["a-model"],
            "prompt": "hi",
            "cwd": str(submit_dir),
            "exclude_mcp_servers": ["khive"],
        },
        expect_refusal=True,
    )

    assert answer["ok"] is False, answer
    assert answer["error"]["kind"] == "invalid_input", answer
    assert "not declared" in answer["error"]["message"]
    assert reason in answer["error"]["message"]
    assert spawned == []
    assert not config.JOBS_DIR.exists() or list(config.JOBS_DIR.iterdir()) == []


def test_no_config_anywhere_is_reported_not_raised(sandbox, submit_dir, monkeypatch):
    """Nothing configured is a choice, not a failure — the run still starts and
    the handle says why it has no servers."""
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda argv, **kw: _FakeProc())
    monkeypatch.setattr("lionagi.cli._mcp_resolve.discover_mcp_config", lambda start: None)

    handle = jobs.submit("agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir))
    assert handle["mcp_config"] is None
    assert handle["mcp_config_reason"]


@pytest.mark.parametrize(("asked", "recorded"), [(None, None), ([], [])])
def test_the_exclusion_is_recorded_as_asked(sandbox, submit_dir, monkeypatch, asked, recorded):
    """null says no exclusion was asked for; an empty list says one was asked for and named none."""
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda argv, **kw: _FakeProc())
    monkeypatch.setattr("lionagi.cli._mcp_resolve.discover_mcp_config", lambda start: None)

    handle = jobs.submit(
        "agent", ["-m", "x"], prompt="hi", cwd=str(submit_dir), exclude_mcp_servers=asked
    )

    assert handle["excluded_mcp_servers"] == recorded
    assert jobs._read_job(handle["run_id"])["excluded_mcp_servers"] == recorded
    assert jobs.status(handle["run_id"])["excluded_mcp_servers"] == recorded


def test_a_caller_who_names_a_config_gets_that_config_and_a_handle_that_says_so(
    sandbox, submit_dir, monkeypatch
):
    """The named file is what the child opens, and what the handle reports.

    Both halves matter. A snapshot generated beside the caller's own choice puts
    two configs on the line, and the one the parser drops is the one the handle
    was naming — so the surface would be describing a file the run never reads.
    """
    (submit_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {"ambient": {"command": "a"}}}))
    chosen = submit_dir / "chosen.json"
    chosen.write_text(json.dumps({"mcpServers": {"mine": {"command": "m"}}}))

    captured: dict = {}
    monkeypatch.setattr(
        jobs.subprocess,
        "Popen",
        lambda argv, **kw: (captured.update(argv=argv), _FakeProc())[1],
    )

    handle = _spawn_agent(
        {"query": ["a-model"], "prompt": "hi", "mcp_config": str(chosen), "cwd": str(submit_dir)}
    )

    child_sees = _child_config(captured["argv"])
    assert child_sees == str(chosen)
    seen = resolve_spawn_mcp_servers(child_sees, launch_dir=submit_dir)
    assert set(seen.servers) == {"mine"}
    # The handle names the file the child opens, and does not claim a snapshot
    # it never took.
    assert handle["mcp_config"] == str(chosen)
    assert handle["mcp_config_source"] == str(chosen)
    assert not (config.job_dir(handle["run_id"]) / "mcp-servers.json").exists()


def test_the_generated_snapshot_still_wins_when_the_caller_says_nothing(
    sandbox, submit_dir, monkeypatch
):
    """Saying nothing is still the ambient config, snapshotted into the run."""
    (submit_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {"ambient": {"command": "a"}}}))

    captured: dict = {}
    monkeypatch.setattr(
        jobs.subprocess,
        "Popen",
        lambda argv, **kw: (captured.update(argv=argv), _FakeProc())[1],
    )

    handle = _spawn_agent({"query": ["a-model"], "prompt": "hi", "cwd": str(submit_dir)})

    snapshot = config.job_dir(handle["run_id"]) / "mcp-servers.json"
    assert _child_config(captured["argv"]) == str(snapshot)
    assert handle["mcp_config"] == str(snapshot)
    assert handle["mcp_config_source"] == str(submit_dir / ".mcp.json")
    # Read as the child reads it, from a directory of its own: the snapshot still names
    # where the submission searched, so the pair rediscovers the same file.
    seen = resolve_spawn_mcp_servers(str(snapshot), launch_dir=snapshot.parent)
    assert set(seen.servers) == {"ambient"}
    assert seen.searched_from == submit_dir.resolve()
    assert discover_mcp_config(seen.searched_from) == seen.source


def test_a_caller_who_asks_for_no_servers_is_not_handed_a_snapshot(
    sandbox, submit_dir, monkeypatch
):
    """Asking for none is an answer. Resolving one anyway would put a config on
    the line beside the switch that turns configs off, and name it on a handle
    for a run whose child was told to ignore it."""
    (submit_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {"ambient": {"command": "a"}}}))

    captured: dict = {}
    monkeypatch.setattr(
        jobs.subprocess,
        "Popen",
        lambda argv, **kw: (captured.update(argv=argv), _FakeProc())[1],
    )

    handle = _spawn_agent(
        {"query": ["a-model"], "prompt": "hi", "no_mcp_config": True, "cwd": str(submit_dir)}
    )

    assert _child_config(captured["argv"]) is None
    assert handle["mcp_config"] is None
    assert handle["mcp_config_source"] is None
    assert not (config.job_dir(handle["run_id"]) / "mcp-servers.json").exists()


def test_a_relative_jobs_root_still_hands_the_child_an_absolute_snapshot_path(
    sandbox, submit_dir, monkeypatch, tmp_path
):
    """The child reads ``--mcp-config`` from its own directory, not the server's.

    A jobs root that is relative (or empty, resolving to the process's directory)
    made the snapshot path relative too, so a run pointed at another checkout was
    told to read a file that exists only under the server's own directory. The
    path handed over has to be absolute whatever the root was written as.
    """
    from pathlib import Path

    (submit_dir / ".mcp.json").write_text(json.dumps({"mcpServers": {"s1": {"command": "one"}}}))
    monkeypatch.setattr(config, "JOBS_DIR", Path("relative-jobs"))
    other = tmp_path / "other"
    other.mkdir()

    captured: dict = {}
    monkeypatch.setattr(
        jobs.subprocess,
        "Popen",
        lambda argv, **kw: (captured.update(argv=argv), _FakeProc())[1],
    )

    handle = jobs.submit("agent", ["-m", "x"], prompt="hi", cwd=str(other))

    handed = _child_config(captured["argv"])
    assert handed is not None
    assert Path(handed).is_absolute(), handed
    assert (other / handed).is_file(), "the child's own directory must be able to read it"
    assert handle["mcp_config"] == handed
