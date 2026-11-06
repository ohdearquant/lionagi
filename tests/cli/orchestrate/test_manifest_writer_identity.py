# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Orchestration entry points keep each run manifest tied to its directory."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lionagi import Branch
from lionagi.cli import _runs, _util
from lionagi.cli.orchestrate import _orchestration as orch
from lionagi.cli.orchestrate import fanout, flow
from lionagi.state import db as db_mod
from lionagi.state.lifecycle import callbacks, notify_settings
from lionhub.orchestration.mcp_config import McpResolution


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["flow", "play", "fanout"])
@pytest.mark.parametrize("handoff", [False, True], ids=["generated", "inherited"])
async def test_orchestration_entrypoints_keep_manifest_directory_identity(
    tmp_path, monkeypatch, kind, handoff
):
    root = tmp_path / "runs"
    monkeypatch.setattr(_runs, "RUNS_ROOT", root)
    monkeypatch.setattr(_runs, "_ALLOCATED_RUN_ID", None)
    monkeypatch.setattr(_util, "_run_allocated", False)
    monkeypatch.setattr(db_mod, "DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(
        db_mod, "settings", db_mod.settings.model_copy(update={"LIONAGI_STATE_DB_URL": None})
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("LIONAGI_AGENT_DEPTH", "0")
    monkeypatch.delenv("LIONAGI_SESSION_ID", raising=False)
    monkeypatch.delenv("LIONAGI_STATE_DB_URL", raising=False)
    if handoff:
        monkeypatch.setenv("LIONAGI_RUN_ID", "handoff-run")
    else:
        monkeypatch.delenv("LIONAGI_RUN_ID", raising=False)

    def no_external(*_args, **_kwargs):
        raise AssertionError("process, signal or network access is outside this test")

    monkeypatch.setattr(subprocess, "Popen", no_external)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_external)
    monkeypatch.setattr(os, "kill", no_external)
    if hasattr(os, "killpg"):
        monkeypatch.setattr(os, "killpg", no_external)
    monkeypatch.setattr(socket.socket, "connect", no_external)
    monkeypatch.setattr("lionagi._paths._find_git_root", lambda _cwd: None)
    monkeypatch.setattr("lionagi.ln._proc.current_pid_markers", lambda: {})
    monkeypatch.setattr(orch, "_resolve_project", lambda _project: ("fixture/project", "explicit"))
    monkeypatch.setattr(
        "lionhub.orchestration.mcp_config.resolve_spawn_mcp_servers",
        lambda *_args, **_kwargs: McpResolution(
            servers=None, reason="no_mcp_config_found", source=None, searched_from=tmp_path
        ),
    )
    model = SimpleNamespace(
        endpoint=SimpleNamespace(config=SimpleNamespace(provider="codex", kwargs={}))
    )
    monkeypatch.setattr(orch, "build_imodel_from_spec", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(orch, "resolve_persisted_effort", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(orch, "create_agent", AsyncMock(return_value=Branch(name="orchestrator")))
    monkeypatch.setattr(
        notify_settings, "register_run_notify_outcome_scope", lambda *_a, **_k: None
    )
    monkeypatch.setattr(callbacks.DEFAULT_TERMINAL_CALLBACKS, "_registrations", {})

    snapshots = []
    original_write = _runs.RunDir.write_manifest

    def record_manifest(run, data):
        original_write(run, data)
        snapshots.append((run.state_root, json.loads(run.manifest_path.read_text())))

    monkeypatch.setattr(_runs.RunDir, "write_manifest", record_manifest)

    async def fake_inner(_model, _prompt, *, env, **_kwargs):
        # The real entry point has already run start_live_persist here.
        assert env._live_persist is not None
        started = env.run.read_manifest()
        assert started["status"] == "running"
        assert started["invocation_kind"] == kind
        assert started["run_id"] == env.run.state_root.name
        return "offline result"

    if kind == "fanout":
        monkeypatch.setattr(fanout, "_run_fanout_inner", fake_inner)
        result, status = await fanout._run_fanout(
            "codex/model", "offline", project="fixture/project"
        )
    else:
        monkeypatch.setattr(flow, "_run_flow_inner", fake_inner)
        result, status = await flow._run_flow(
            "codex/model",
            "offline",
            project="fixture/project",
            playbook_name="offline-play" if kind == "play" else None,
        )

    assert result == "offline result"
    assert status in {"completed", "completed_empty"}
    # Allocation, start, and stop all wrote through RunDir's real writer.
    assert len(snapshots) >= 3
    paths = {path for path, _ in snapshots}
    assert len(paths) == 1
    path = next(iter(paths))
    assert path.parent == root
    assert all(snapshot["run_id"] == path.name for _, snapshot in snapshots)
    assert snapshots[-1][1]["status"] == status
    if handoff:
        assert path.name == "handoff-run"
    assert "LIONAGI_RUN_ID" not in os.environ
