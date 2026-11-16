# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for li agent -r (resume) empty-stream detection."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# Shared stub helpers


def _wire_agent_stubs(monkeypatch, tmp_path: Path, operate_return=None):
    """Monkeypatch all external I/O in _run_agent so tests run without real I/O."""
    import lionhub.serve.cli.agent as agent_mod
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

    async def fake_teardown(
        ctx,
        *,
        status="completed",
        exception=None,
        cwd=None,
        engine_session_uid=None,
        defer_terminal=False,
    ):
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


@pytest.fixture
def resume_snapshots(tmp_path, monkeypatch):
    from lionagi import Branch, iModel
    from lionhub.orchestration import runs

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runs, "RUNS_ROOT", tmp_path / "runs")
    monkeypatch.setattr(runs, "_LEGACY_AGENTS_ROOT", tmp_path / "legacy")
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="resumed")
    branch = Branch(
        id="aaaaaaaa-0000-4000-8000-000000000001",
        chat_model=iModel(
            provider="codex", endpoint="query_cli", model="synthetic", api_key="dummy"
        ),
    )

    def write(version, *, legacy=False, branch_id=None):
        data = branch.to_dict()
        data["metadata"]["snapshot_version"] = version
        if branch_id is not None:
            data["id"] = branch_id
        if legacy:
            path = runs._LEGACY_AGENTS_ROOT / "codex" / str(branch.id)
            data = {"old_identity": {"id": data.pop("id")}, **data}
        else:
            run = runs.RUNS_ROOT / version
            path = run / "branches" / f"{branch.id}.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        if not legacy:
            timestamp = 1_000 if version == "older" else 2_000
            os.utime(run, (timestamp, timestamp))
        return path

    return str(branch.id), write


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_snapshot", ["wrong-id", "broken-json"])
async def test_canonical_resume_refuses_a_bad_newest_snapshot(
    monkeypatch, resume_snapshots, bad_snapshot
):
    from lionagi._errors import ConfigurationError
    from lionagi.service.manager import iModelManager
    from lionhub.serve.cli import agent as agent_mod

    def unexpected_allocation():
        pytest.fail("the rejected snapshot reached run allocation")

    monkeypatch.setattr(agent_mod, "allocate_run", unexpected_allocation)

    branch_id, write = resume_snapshots
    write("older")
    newest = write("newest", branch_id="bbbbbbbb-0000-4000-8000-000000000002")
    if bad_snapshot == "broken-json":
        newest.write_text("[", encoding="utf-8")
        expected = pytest.raises(json.JSONDecodeError)
    else:
        expected = pytest.raises(
            ConfigurationError, match="saved branch ID does not match the requested branch ID"
        )

    with expected:
        await agent_mod._run_agent(None, "follow up", resume=branch_id, no_mcp_config=True)

    if bad_snapshot == "wrong-id":
        iModelManager.shutdown.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["canonical", "prefix", "legacy"])
async def test_resume_uses_the_matching_latest_snapshot_or_legacy_branch(
    monkeypatch, resume_snapshots, reference
):
    from lionagi import Branch
    from lionhub.serve.cli import agent as agent_mod

    branch_id, write = resume_snapshots
    if reference == "legacy":
        write("legacy", legacy=True)
        expected_version = "legacy"
    else:
        if reference == "canonical":
            write("older")
        write("newest")
        expected_version = "newest"
    operated = []

    async def operate(self, *args, **kwargs):
        operated.append((str(self.id), self.metadata["snapshot_version"]))
        return "resumed"

    monkeypatch.setattr(Branch, "operate", operate)
    selector = branch_id[:8] if reference == "prefix" else branch_id
    reply, _, resumed_id, terminal, _ = await agent_mod._run_agent(
        None, "follow up", resume=selector, no_mcp_config=True
    )

    assert (reply, resumed_id, terminal) == ("resumed", branch_id, "completed")
    assert operated == [(branch_id, expected_version)]


# Test: resume with empty stream → non-zero exit + actionable message


@pytest.mark.asyncio
async def test_resume_empty_stream_returns_failed_status(monkeypatch, tmp_path):
    """Resume that produces no assistant output must return terminal_status='failed'."""
    branch_id, branch_path = _make_branch_json(tmp_path)

    import lionhub.serve.cli.agent as agent_mod

    monkeypatch.setattr(
        agent_mod,
        "find_branch",
        lambda bid: ("run-x", branch_path),
    )

    _wire_agent_stubs(monkeypatch, tmp_path, operate_return=None)

    from lionhub.serve.cli.agent import _run_agent

    _result, _provider, _bid, terminal_status, _sid = await _run_agent(
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

    import lionhub.orchestration.console as logging_mod
    import lionhub.serve.cli.agent as agent_mod

    errors_emitted: list[str] = []
    monkeypatch.setattr(logging_mod, "log_error", lambda msg: errors_emitted.append(msg))
    monkeypatch.setattr(agent_mod, "log_error", lambda msg: errors_emitted.append(msg))

    monkeypatch.setattr(
        agent_mod,
        "find_branch",
        lambda bid: ("run-x", branch_path),
    )

    _wire_agent_stubs(monkeypatch, tmp_path, operate_return=None)

    from lionhub.serve.cli.agent import _run_agent

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

    import lionhub.orchestration.console as logging_mod
    import lionhub.serve.cli.agent as agent_mod

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
        query=["codex/model", "follow up"],
        prompt_flag=None,
        prompt_file=None,
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

    from lionhub.serve.cli.agent import run_agent

    rc = run_agent(args)
    assert rc != 0, f"Expected non-zero exit code on empty-stream resume, got {rc}"


# Test: resume with non-empty stream → exit 0 (no regression)


@pytest.mark.asyncio
async def test_resume_nonempty_stream_returns_completed_status(monkeypatch, tmp_path):
    """Resume that produces output must keep terminal_status='completed' (no regression)."""
    branch_id, branch_path = _make_branch_json(tmp_path)

    import lionhub.serve.cli.agent as agent_mod

    monkeypatch.setattr(
        agent_mod,
        "find_branch",
        lambda bid: ("run-x", branch_path),
    )

    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="here is the verdict")

    from lionhub.serve.cli.agent import _run_agent

    result, _provider, _bid, terminal_status, _sid = await _run_agent(
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
    import lionhub.serve.cli.agent as agent_mod

    _wire_agent_stubs(monkeypatch, tmp_path, operate_return=None)

    from lionhub.serve.cli.agent import _run_agent

    _result, _provider, _bid, terminal_status, _sid = await _run_agent(
        "codex/model",
        "say hi",
        resume=None,
    )

    # Fresh run empty output is allowed to succeed (no detection on non-resume path).
    assert terminal_status == "completed", (
        f"Fresh run with empty output should still be 'completed', got {terminal_status!r}"
    )


# Test: a restored CLI session id is not this run's engine session at teardown


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream_reports_session", "teardown_gets_session", "final_status"),
    [(False, False, "failed"), (True, True, "completed")],
    ids=["before-any-session-event", "after-a-session-event"],
)
async def test_a_resumed_turn_failing_before_the_cli_reports_a_session_stays_failed(
    monkeypatch, tmp_path, stream_reports_session, teardown_gets_session, final_status
):
    """The id restored from the saved branch is the session to resume, not one this run's CLI
    has reported. A provider error before any session event must not be settled by the status
    of the previous turn's session, which is already completed in the store; once the stream
    has reported the session, the run is settled by that session as before."""
    import lionhub.orchestration.runs as runs_mod
    import lionhub.serve.cli.agent as agent_mod
    from lionagi import Branch, iModel
    from lionagi._provider_errors import ProviderError
    from lionagi.state.claude_mirror import mirror_session
    from lionagi.state.db import StateDB

    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    previous_turn_session = "7d1c2e90-5b3a-4f6e-9a8d-0c1b2a3d4e5f"

    saved = Branch(
        chat_model=iModel(provider="claude_code", model="claude-sonnet-5-5", api_key="test-key")
    )
    saved.chat_model.endpoint.session_id = previous_turn_session
    branch_path = tmp_path / f"{saved.id}.json"
    branch_path.write_text(json.dumps(saved.to_dict()))

    async with StateDB() as db:
        await mirror_session(
            db,
            session_uid=previous_turn_session,
            events=[
                {
                    "type": "assistant",
                    "uuid": "e1",
                    "timestamp": "2026-07-05T00:00:00.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "done"}],
                    },
                }
            ],
            tool_names={},
            status="completed",
        )

    _wire_agent_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", branch_path))

    async def operate_fails(self, instruction=None, **kw):
        if stream_reports_session:
            # What the run's stream does on a session event.
            self.chat_model.endpoint.session_id = previous_turn_session
        raise ProviderError("the CLI ended with an error")

    monkeypatch.setattr(Branch, "operate", operate_fails)

    teardown_calls: list[dict] = []
    contexts: list[dict] = []

    async def setup(branch, **kw):
        ctx = await runs_mod.setup_agent_persist(branch, **kw)
        contexts.append(ctx)
        return ctx

    async def teardown(ctx, *, status="completed", **kw):
        final = await runs_mod.teardown_agent_persist(ctx, status=status, **kw)
        teardown_calls.append({"engine_session_uid": kw.get("engine_session_uid"), "final": final})
        return final

    monkeypatch.setattr(agent_mod, "setup_agent_persist", setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", teardown)

    with pytest.raises(ProviderError):
        await agent_mod._run_agent(None, "follow up", resume=str(saved.id), no_mcp_config=True)

    assert len(contexts) == 1 and contexts[0] is not None
    assert [call["engine_session_uid"] for call in teardown_calls] == [
        previous_turn_session if teardown_gets_session else None
    ]
    assert [call["final"] for call in teardown_calls] == [final_status]
    async with StateDB() as db:
        session = await db.get_session(contexts[0]["session_id"])
    assert session["status"] == final_status
