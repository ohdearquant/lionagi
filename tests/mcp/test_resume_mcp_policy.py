# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Resumed submissions must not promise changes their replay will ignore."""

from __future__ import annotations

import asyncio
import json

import pytest

from lionhub.serve.mcp import config, dispatch, jobs


class _Process:
    pid = 4242

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def submission(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "li_command", lambda: ["echo"])
    monkeypatch.setattr(config, "child_actor_suffix", lambda: None)
    monkeypatch.setattr(jobs, "_read_lifecycle", lambda _: None)
    monkeypatch.setattr("lionhub.orchestration.spawn._plugin_agent_profiles", lambda: {})
    cwd = tmp_path / "work"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    agents = cwd / ".lionagi" / "agents"
    agents.mkdir(parents=True)
    (agents / "ordinary.md").write_text("---\nmodel: codex/gpt-5\n---\nOrdinary.")
    (agents / "isolated.md").write_text("---\nmodel: codex/gpt-5\nmcp: none\n---\nIsolated.")
    source = cwd / ".mcp.json"
    source.write_text(json.dumps({"mcpServers": {"ambient": {"command": "echo"}}}))
    spawned = []

    def popen(argv, **kwargs):
        if argv[0] == "echo":
            spawned.append(argv)
        return _Process()

    monkeypatch.setattr(jobs.subprocess, "Popen", popen)
    return cwd, source, spawned


def _request(op, args):
    help_target = {"verb": op, "playbook": args["playbook"]} if op == "play.submit" else op
    fingerprint = asyncio.run(dispatch.request(help=help_target))["schema_fingerprint"]
    return asyncio.run(
        dispatch.request(ops=[{"op": op, "args": args, "schema_fingerprint": fingerprint}])
    )["ops"][0]


def _assert_replay_refusal(result, spawned):
    assert not result["ok"], result
    assert result["error"]["kind"] == "invalid_input"
    message = result["error"]["message"].lower()
    assert "replay" in message and "record" in message and "fresh" in message
    assert spawned == []
    assert not config.JOBS_DIR.exists() or not list(config.JOBS_DIR.iterdir())


@pytest.mark.parametrize(
    "extra",
    [
        {"exclude_mcp_servers": ["ambient"]},
        {"exclude_mcp_servers": []},
        {"mcp_config": "named.json"},
        {"no_mcp_config": True},
        {"agent": "ordinary"},
    ],
    ids=["exclude", "empty-exclude", "named-config", "no-config", "named-agent"],
)
def test_flow_resume_refuses_selection_it_cannot_apply(submission, extra):
    cwd, _, spawned = submission
    result = _request(
        "flow.submit", {"resume": "old-run", "prompt": "work", "cwd": str(cwd), **extra}
    )
    _assert_replay_refusal(result, spawned)


@pytest.mark.parametrize(
    "extra",
    [
        {"exclude_mcp_servers": ["ambient"]},
        {"exclude_mcp_servers": []},
        {"mcp_config": "named.json"},
        {"no_mcp_config": True},
        {"agent": "ordinary"},
    ],
    ids=["exclude", "empty-exclude", "named-config", "no-config", "named-agent"],
)
def test_play_resume_refuses_selection_it_cannot_apply(submission, extra):
    cwd, _, spawned = submission
    books = cwd / ".lionagi" / "playbooks"
    books.mkdir(parents=True)
    (books / "probe.playbook.yaml").write_text("name: probe\nprompt: work\n")
    result = _request(
        "play.submit", {"playbook": "probe", "resume": "old-run", "cwd": str(cwd), **extra}
    )
    _assert_replay_refusal(result, spawned)


def test_play_selectorless_resume_records_replay_without_snapshot(submission):
    cwd, source, spawned = submission
    books = cwd / ".lionagi" / "playbooks"
    books.mkdir(parents=True)
    (books / "probe.playbook.yaml").write_text("name: probe\nprompt: work\n")
    source.write_text("not JSON: resume must not inspect this")
    result = _request("play.submit", {"playbook": "probe", "resume": "old-run", "cwd": str(cwd)})
    assert result["ok"], result
    handle = result["result"]
    assert handle["mcp_config"] is None
    assert handle["mcp_config_source"] is None
    assert "replay" in handle["mcp_config_reason"].lower()
    assert handle["declared_mcp_servers"] is None
    assert handle["mcp_config_servers"] is None
    record = jobs._read_job(handle["run_id"])
    assert record["mcp_config_reason"] == handle["mcp_config_reason"]
    assert record["declared_mcp_servers"] is None
    assert not any(
        arg == flag or arg.startswith(flag + "=")
        for arg in spawned[0]
        for flag in ("--mcp-config", "--mcp-submit-snapshot", "--no-mcp-config")
    )
    assert not list(config.JOBS_DIR.rglob("mcp-servers.json"))


@pytest.mark.parametrize("continuation", [{"resume": "old-branch"}, {"continue_last": True}])
def test_agent_resume_and_continue_refuse_nonempty_exclusions(submission, continuation):
    cwd, _, spawned = submission
    result = _request(
        "agent.submit",
        {**continuation, "prompt": "work", "cwd": str(cwd), "exclude_mcp_servers": ["ambient"]},
    )
    _assert_replay_refusal(result, spawned)


@pytest.mark.parametrize(
    ("op", "continuation", "extra"),
    [
        ("flow.submit", {"resume": "old-run"}, {}),
        ("agent.submit", {"resume": "old-branch"}, {}),
        ("agent.submit", {"continue_last": True}, {}),
        ("agent.submit", {"resume": "old-branch"}, {"exclude_mcp_servers": []}),
        ("agent.submit", {"continue_last": True}, {"exclude_mcp_servers": []}),
    ],
)
def test_selectorless_resume_records_unknown_replay_without_snapshot(
    submission, op, continuation, extra
):
    cwd, source, spawned = submission
    source.write_text("not JSON: resume must not inspect this")
    result = _request(op, {**continuation, **extra, "prompt": "work", "cwd": str(cwd)})
    assert result["ok"], result
    handle = result["result"]
    assert handle["mcp_config"] is None
    assert handle["mcp_config_source"] is None
    assert "replay" in handle["mcp_config_reason"].lower()
    assert handle["declared_mcp_servers"] is None
    assert handle["mcp_config_servers"] is None
    record = jobs._read_job(handle["run_id"])
    assert record["mcp_config_reason"] == handle["mcp_config_reason"]
    assert record["declared_mcp_servers"] is None
    assert not any(
        arg == flag or arg.startswith(flag + "=")
        for arg in spawned[0]
        for flag in ("--mcp-config", "--mcp-submit-snapshot", "--no-mcp-config")
    )
    assert not list(config.JOBS_DIR.rglob("mcp-servers.json"))


@pytest.mark.parametrize("continuation", [{"resume": "old-branch"}, {"continue_last": True}])
@pytest.mark.parametrize("selection", ["named", "none", "profile", "contained-profile"])
def test_agent_resume_keeps_supported_explicit_choices(submission, continuation, selection):
    cwd, source, spawned = submission
    if selection == "profile":
        source.write_text("not JSON: an ordinary profile still replays the branch")
    extra = (
        {"mcp_config": str(source)}
        if selection == "named"
        else {"no_mcp_config": True}
        if selection == "none"
        else {"agent": "isolated" if selection == "contained-profile" else "ordinary"}
    )
    result = _request("agent.submit", {**continuation, **extra, "prompt": "work", "cwd": str(cwd)})
    assert result["ok"], result
    argv = spawned[0]
    if selection == "named":
        assert result["result"]["mcp_config"] == str(source)
        assert any(arg.startswith("--mcp-config=") for arg in argv)
    elif selection == "none":
        assert result["result"]["declared_mcp_servers"] == []
        assert "--no-mcp-config" in argv
    elif selection == "profile":
        assert any(arg == "--agent=ordinary" for arg in argv)
        assert "replay" in result["result"]["mcp_config_reason"].lower()
        assert result["result"]["declared_mcp_servers"] is None
        assert "--mcp-submit-snapshot" not in argv
    else:
        assert any(arg == "--agent=isolated" for arg in argv)
        assert result["result"]["declared_mcp_servers"] == []
        assert result["result"]["mcp_config_reason"] == "mcp_disabled_by_profile:isolated"
