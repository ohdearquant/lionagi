"""Submitted flow and play resumes refuse ambiguous old snapshots."""

import argparse
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from lionhub.orchestration import mcp_config
from lionhub.serve.cli import orchestrate
from lionhub.serve.cli.orchestrate import flow
from lionhub.serve.mcp import jobs
from tests.mcp.test_resume_mcp_policy import _request, submission


@pytest.mark.parametrize("op", ["flow.submit", "play.submit"])
def test_submit_resume_reaches_legacy_refusal(submission, monkeypatch, op):  # noqa: F811
    cwd, _, spawned = submission
    monkeypatch.setattr(jobs, "refuse_nested_agent", lambda: None)
    monkeypatch.setattr(jobs, "_process_create_time", lambda _: ("unavailable", None))
    recorded = cwd / "legacy.json"
    recorded.write_text(json.dumps({"mcpServers": {}, "lionagiSnapshotOf": str(cwd / ".mcp.json")}))
    checkpoint = {
        "prompt": "work",
        "config": {
            "model_spec": "claude_code/sonnet",
            "mcp_config": str(recorded),
            "mcp_launch_dir": str(cwd),
            "no_mcp_config": False,
        },
    }
    monkeypatch.setattr(
        flow, "resolve_checkpoint_target", AsyncMock(return_value=(None, checkpoint))
    )
    monkeypatch.setattr(flow, "stamp_worker_depth", lambda: None)
    errors = []
    monkeypatch.setattr(orchestrate, "log_error", errors.append)
    monkeypatch.setattr(
        mcp_config, "discover_mcp_config", lambda _: pytest.fail("ambient discovery")
    )
    reads = []
    real_read = mcp_config._read_config

    def tracked(path):
        reads.append(Path(path))
        return real_read(path)

    monkeypatch.setattr(mcp_config, "_read_config", tracked)
    args = {"resume": "old-run", "cwd": str(cwd)}
    if op == "flow.submit":
        args["prompt"] = "work"
    else:
        books = cwd / ".lionagi" / "playbooks"
        books.mkdir(parents=True)
        (books / "probe.playbook.yaml").write_text("name: probe\nprompt: work\n")
        args["playbook"] = "probe"
    reply = _request(op, args)
    assert reply["ok"], reply
    parser = argparse.ArgumentParser()
    orchestrate.add_orchestrate_subparser(parser.add_subparsers(dest="command", required=True))
    parsed = parser.parse_args(spawned[0][1:])
    assert orchestrate.run_orchestrate(parsed) != 0
    assert len(errors) == 1
    assert "recorded MCP" in errors[0] and "fresh run" in errors[0]
    assert reads == [recorded]
