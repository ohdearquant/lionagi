# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for li agent -r (resume) empty-stream detection (closes #1427)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Shared stub helpers
# ---------------------------------------------------------------------------


def _wire_agent_stubs(monkeypatch, tmp_path: Path, operate_return=None):
    """Monkeypatch all external I/O in _run_agent so tests run without real I/O."""
    import lionagi.cli.agent as agent_mod
    from lionagi import Branch
    from lionagi.service.manager import iModelManager

    async def fake_operate(self, instruction=None, **kw):
        return operate_return

    monkeypatch.setattr(Branch, "operate", fake_operate)
    monkeypatch.setattr(iModelManager, "shutdown", AsyncMock())

    monkeypatch.setattr(agent_mod, "build_chat_model", lambda *a, **kw: "codex/model")
    monkeypatch.setattr(agent_mod, "resolve_persisted_effort", lambda *a, **kw: None)

    async def fake_setup(*a, **kw):
        return None

    async def fake_teardown(ctx, *, status="completed", exception=None):
        return status

    monkeypatch.setattr(agent_mod, "setup_agent_persist", fake_setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", fake_teardown)
    monkeypatch.setattr(agent_mod, "save_last_branch_pointer", lambda *a, **kw: None)
    monkeypatch.setattr(
        agent_mod,
        "_provenance",
        SimpleNamespace(
            resolve_model_spec=lambda p, m: f"{p}/{m}",
            agent_definition_hash=lambda n: "abc",
        ),
    )
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: None)
    monkeypatch.setattr(
        agent_mod,
        "allocate_run",
        lambda: SimpleNamespace(
            run_id="r",
            artifact_root=tmp_path / "artifacts",
            stream_dir=tmp_path / "stream",
            branches_dir=tmp_path / "branches",
        ),
    )


def _make_branch_json(tmp_path: Path) -> tuple[str, Path]:
    """Persist a minimal serialised Branch and return (branch_id, path)."""
    from lionagi import Branch

    b = Branch()
    branch_id = str(b.id)
    p = tmp_path / f"{branch_id}.json"
    p.write_text(json.dumps(b.to_dict()))
    return branch_id, p


# ---------------------------------------------------------------------------
# Test: resume with empty stream → non-zero exit + actionable message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_empty_stream_returns_failed_status(monkeypatch, tmp_path):
    """Resume that produces no assistant output must return terminal_status='failed'."""
    branch_id, branch_path = _make_branch_json(tmp_path)

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(
        agent_mod,
        "find_branch",
        lambda bid: ("run-x", branch_path),
    )

    _wire_agent_stubs(monkeypatch, tmp_path, operate_return=None)

    from lionagi.cli.agent import _run_agent

    _result, _provider, _bid, terminal_status = await _run_agent(
        "codex/model",
        "follow up",
        resume=branch_id,
    )

    assert terminal_status == "failed", (
        f"Expected 'failed' on empty-stream resume, got {terminal_status!r}"
    )


@pytest.mark.asyncio
async def test_resume_empty_stream_logs_actionable_message(monkeypatch, tmp_path):
    """Resume empty-stream must emit an error message naming the UUID and the re-run hint."""
    branch_id, branch_path = _make_branch_json(tmp_path)

    import lionagi.cli._logging as logging_mod
    import lionagi.cli.agent as agent_mod

    errors_emitted: list[str] = []
    monkeypatch.setattr(logging_mod, "log_error", lambda msg: errors_emitted.append(msg))
    monkeypatch.setattr(agent_mod, "log_error", lambda msg: errors_emitted.append(msg))

    monkeypatch.setattr(
        agent_mod,
        "find_branch",
        lambda bid: ("run-x", branch_path),
    )

    _wire_agent_stubs(monkeypatch, tmp_path, operate_return=None)

    from lionagi.cli.agent import _run_agent

    await _run_agent("codex/model", "follow up", resume=branch_id)

    combined = " ".join(errors_emitted)
    assert "empty" in combined.lower() or "expired" in combined.lower(), (
        f"Error message should mention empty stream or expiry; got: {errors_emitted}"
    )
    assert "re-run" in combined.lower() or "without" in combined.lower(), (
        f"Error message should tell caller to re-run without -r; got: {errors_emitted}"
    )


@pytest.mark.asyncio
async def test_resume_empty_stream_exit_code_nonzero(monkeypatch, tmp_path):
    """run_agent() must return non-zero exit code when resume produces empty stream."""
    branch_id, branch_path = _make_branch_json(tmp_path)

    import lionagi.cli._logging as logging_mod
    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(logging_mod, "log_error", lambda msg: None)
    monkeypatch.setattr(agent_mod, "log_error", lambda msg: None)
    monkeypatch.setattr(logging_mod, "hint", lambda msg: None)
    monkeypatch.setattr(agent_mod, "hint", lambda msg: None)

    monkeypatch.setattr(
        agent_mod,
        "find_branch",
        lambda bid: ("run-x", branch_path),
    )

    _wire_agent_stubs(monkeypatch, tmp_path, operate_return=None)

    # Stub run_async to call _run_agent synchronously
    from lionagi.ln.concurrency import run_async as _real_run_async

    monkeypatch.setattr(agent_mod, "run_async", _real_run_async)

    args = SimpleNamespace(
        model="codex/model",
        prompt="follow up",
        yolo=False,
        verbose=False,
        theme=None,
        resume=branch_id,
        continue_last=False,
        effort=None,
        agent=None,
        cwd=None,
        timeout=None,
        fast=False,
        invocation=None,
        project=None,
        bypass=False,
        preset=None,
        form=None,
    )

    from lionagi.cli.agent import run_agent

    rc = run_agent(args)
    assert rc != 0, f"Expected non-zero exit code on empty-stream resume, got {rc}"


# ---------------------------------------------------------------------------
# Test: resume with non-empty stream → exit 0 (no regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_nonempty_stream_returns_completed_status(monkeypatch, tmp_path):
    """Resume that produces output must keep terminal_status='completed' (no regression)."""
    branch_id, branch_path = _make_branch_json(tmp_path)

    import lionagi.cli.agent as agent_mod

    monkeypatch.setattr(
        agent_mod,
        "find_branch",
        lambda bid: ("run-x", branch_path),
    )

    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="here is the verdict")

    from lionagi.cli.agent import _run_agent

    result, _provider, _bid, terminal_status = await _run_agent(
        "codex/model",
        "follow up",
        resume=branch_id,
    )

    assert terminal_status == "completed", (
        f"Non-empty resume must stay 'completed', got {terminal_status!r}"
    )
    assert result == "here is the verdict"


@pytest.mark.asyncio
async def test_fresh_run_empty_result_still_exits_zero(monkeypatch, tmp_path):
    """A fresh (non-resume) run that returns empty output must NOT be affected by the guard."""
    import lionagi.cli.agent as agent_mod

    _wire_agent_stubs(monkeypatch, tmp_path, operate_return=None)

    from lionagi.cli.agent import _run_agent

    _result, _provider, _bid, terminal_status = await _run_agent(
        "codex/model",
        "say hi",
        resume=None,
    )

    # Fresh run empty output is allowed to succeed (no detection on non-resume path).
    assert terminal_status == "completed", (
        f"Fresh run with empty output should still be 'completed', got {terminal_status!r}"
    )
