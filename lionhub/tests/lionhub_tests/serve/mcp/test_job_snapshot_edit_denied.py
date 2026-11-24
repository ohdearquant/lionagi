# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The MCP server snapshot an ``agent.submit`` or ``flow.submit`` hands a leg is read again by a
resume, so a Claude Code child may not edit it."""

from __future__ import annotations

import os
from fnmatch import fnmatch


def test_a_claude_child_is_denied_edit_on_the_job_snapshot(monkeypatch, tmp_path):
    from lionhub.agent import child_sandbox
    from lionhub.serve.mcp import config, jobs

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setattr("lionagi._paths.LIONAGI_HOME", home / ".lionagi")
    monkeypatch.setattr(child_sandbox, "_global_settings", lambda: {"child_sandbox": {}})
    workspace = tmp_path / "work"
    workspace.mkdir()
    # Built from the server's own names, so a rename of either breaks this test.
    jobs_dir = home / ".lionagi" / config.JOBS_DIR.relative_to(config.LIONAGI_HOME)
    snapshot = jobs_dir / "20261123-job" / jobs._MCP_SNAPSHOT_FILENAME

    policy = child_sandbox.resolve_child_sandbox(None)
    settings = child_sandbox.claude_settings_for(policy, workspace=workspace)

    entry = f"{home}/.lionagi/mcp/jobs/*/mcp-servers.json"
    assert entry in child_sandbox.run_state_paths()
    assert f"Edit(/{entry})" in settings["permissions"]["deny"]
    assert fnmatch(str(snapshot), entry)
    # Nor is it under a root a Claude child's Bash may write.
    roots = [str(workspace), *(os.path.expanduser(root) for root in policy.allow_write)]
    assert not any(
        str(snapshot) == root.rstrip("/") or str(snapshot).startswith(root.rstrip("/") + "/")
        for root in roots
    )
