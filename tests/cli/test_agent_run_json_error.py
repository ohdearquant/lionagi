# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A failed `li agent` run's run.json names its error, so the run directory alone tells a
provider's refusal from a crash."""

from __future__ import annotations

import json
import sys

import pytest

from lionagi.cli._runs import RunDir, resolve_run_reason
from lionagi.state.reasons import RunReasons
from tests.cli.test_agent_context_from import _wire_agent_stubs
from tests.providers.test_claude_code_refusal import REFUSAL


def _run(monkeypatch, tmp_path) -> RunDir:
    import lionagi.cli.agent as agent_mod

    run = RunDir(
        run_id="run-x", state_root=tmp_path / "state", artifact_root=tmp_path / "artifacts"
    )
    run.ensure_state_dirs()
    monkeypatch.setattr(agent_mod, "allocate_run", lambda: run)
    return run


@pytest.mark.asyncio
async def test_a_claude_cli_refusal_reaches_run_json_and_the_reason_as_a_quota_error(
    monkeypatch, tmp_path
):
    """The whole path from the CLI's exit through the model's stream to the run directory: only
    the CLI is a stand-in, printing the refusal and exiting 1 as the real one does."""
    from lionagi import Branch
    from lionagi.providers.anthropic import claude_code as cc

    operate = Branch.operate
    _wire_agent_stubs(monkeypatch, tmp_path)
    monkeypatch.setattr(Branch, "operate", operate)
    cli = tmp_path / "claude"
    cli.write_text(
        f"#!{sys.executable}\nimport sys\nprint({json.dumps(json.dumps(REFUSAL))}, flush=True)\n"
        "sys.exit(1)\n"
    )
    cli.chmod(0o755)
    monkeypatch.setattr(cc, "resolve_claude_cli", lambda: str(cli))
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    with pytest.raises(ValueError) as info:
        await _run_agent("claude/sonnet", "go")

    code, summary, _ = resolve_run_reason(status="failed", exception=info.value)
    assert code == RunReasons.FAILED_PROVIDER_RETRYABLE
    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["status"] == "failed"
    assert manifest["error"] == summary
    assert manifest["error"].startswith(
        "ProviderQuotaError: Claude Code said: You've hit your session"
    )
    assert "(api_error_status 429)" in manifest["error"]


@pytest.mark.asyncio
async def test_a_resume_that_comes_back_empty_says_why_it_failed(monkeypatch, tmp_path):
    """This run turns failed after teardown has written run.json; the error is written with it."""
    import lionagi.cli.agent as agent_mod
    from tests.cli.test_agent_resume_empty_stream import _make_branch_json

    branch_id, branch_path = _make_branch_json(tmp_path)
    monkeypatch.setattr(agent_mod, "find_branch", lambda bid: ("run-x", branch_path))
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return=None)
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    *_, terminal_status, _session_id = await _run_agent("claude/sonnet", "go on", resume=branch_id)

    manifest = json.loads(run.manifest_path.read_text())
    assert terminal_status == manifest["status"] == "failed"
    assert manifest["error"] == (
        "Resume produced an empty stream; the session may have expired "
        f"(resume target: {branch_id})."
    )


@pytest.mark.asyncio
async def test_a_run_failed_by_a_missing_artifact_says_why(monkeypatch, tmp_path):
    """Teardown decides this failure and nothing raises; run.json carries the row's own reason."""
    import lionagi.cli.agent as agent_mod
    from lionagi.state.db import StateDB

    real_setup, real_teardown = agent_mod.setup_agent_persist, agent_mod.teardown_agent_persist
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="done, without the report")
    monkeypatch.setattr(agent_mod, "setup_agent_persist", real_setup)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", real_teardown)
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    contract = {"expected": [{"id": "report", "path": "report.md", "required": True}]}
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: contract)
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    *_, terminal_status, session_id = await _run_agent("claude/sonnet", "go")

    async with StateDB() as db:
        row = await db.get_session(session_id)
    assert row["status_reason_code"] == RunReasons.FAILED_MISSING_ARTIFACT
    manifest = json.loads(run.manifest_path.read_text())
    assert terminal_status == manifest["status"] == "failed"
    assert manifest["error"] == row["status_reason_summary"]
    assert manifest["error"] == "Missing required artifact: report (report.md)."


def _with_a_real_row(monkeypatch, tmp_path, operate_return=None):
    import lionagi.cli.agent as agent_mod

    real_setup, real_teardown = agent_mod.setup_agent_persist, agent_mod.teardown_agent_persist
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return=operate_return)
    monkeypatch.setattr(agent_mod, "setup_agent_persist", real_setup)
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", tmp_path / "state.db")
    return agent_mod, real_teardown


@pytest.mark.asyncio
async def test_a_run_on_a_row_still_holding_an_earlier_runs_reason_names_its_own(
    monkeypatch, tmp_path
):
    """A resume reuses the session row, and one whose reopen missed leaves it terminal with the
    earlier run's reason. The row is put in that state just before this run's teardown: the row
    keeps the earlier reason, and run.json names this run's own failure instead."""
    from lionagi import Branch
    from lionagi.state.db import StateDB

    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path)
    earlier = "Missing required artifact: report (report.md)."
    seen = {}

    async def operate(self, instruction=None, **kw):
        raise ValueError("this run's own provider failure")

    async def teardown(ctx, **kw):
        seen["session_id"] = ctx["session_id"]
        async with StateDB() as db:  # the row as a missed reopen leaves it
            await db.update_status(
                "session",
                ctx["session_id"],
                new_status="failed",
                reason_code=RunReasons.FAILED_MISSING_ARTIFACT,
                reason_summary=earlier,
                source="executor",
                actor=ctx["session_id"],
            )
        return await real_teardown(ctx, **kw)

    monkeypatch.setattr(Branch, "operate", operate)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", teardown)
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    with pytest.raises(ValueError) as info:
        await _run_agent("claude/sonnet", "go")

    async with StateDB() as db:
        row = await db.get_session(seen["session_id"])
    assert row["status_reason_summary"] == earlier  # the row kept the earlier run's reason
    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["status"] == "failed"
    assert manifest["error"] == resolve_run_reason(status="failed", exception=info.value)[1]
    assert "this run's own provider failure" in manifest["error"]


@pytest.mark.asyncio
async def test_a_run_failed_by_a_missing_artifact_says_why_with_its_row_unreadable(
    monkeypatch, tmp_path
):
    """Nothing raised and the row cannot be read once teardown is done: the reason teardown
    decided still reaches run.json."""
    import sqlite3

    from lionagi.state.db import StateDB

    agent_mod, real_teardown = _with_a_real_row(
        monkeypatch, tmp_path, operate_return="done, without the report"
    )
    contract = {"expected": [{"id": "report", "path": "report.md", "required": True}]}
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: contract)
    real_get = StateDB.get_session

    async def teardown(ctx, **kw):
        status = await real_teardown(ctx, **kw)
        reads = iter([sqlite3.OperationalError("database is locked")])

        async def get_session(self, session_id):
            failure = next(reads, None)
            if failure is not None:
                raise failure
            return await real_get(self, session_id)

        monkeypatch.setattr(StateDB, "get_session", get_session)
        return status

    monkeypatch.setattr(agent_mod, "teardown_agent_persist", teardown)
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    *_, terminal_status, _session_id = await _run_agent("claude/sonnet", "go")

    manifest = json.loads(run.manifest_path.read_text())
    assert terminal_status == manifest["status"] == "failed"
    assert manifest["error"] == "Missing required artifact: report (report.md)."


_SWEEP_REASON = "doctor sweep: running longer than 6h"


async def _sweep_as_stale(db, session_id):
    """End the row the way the state doctor ends a stale session."""
    import time

    from lionagi.state.db import StateDB
    from lionagi.state.reasons import SessionReasons

    swept = await StateDB.update_status(
        db,
        "session",
        session_id,
        new_status="failed",
        reason_code=SessionReasons.HEALTH_STALE_NO_HEARTBEAT,
        reason_summary=_SWEEP_REASON,
        source="admin",
        actor="doctor",
        expected_statuses={"running"},
        extra_fields={"ended_at": time.time()},
    )
    assert swept


@pytest.mark.asyncio
async def test_a_failed_status_another_writer_won_the_row_with_names_this_runs_own_outcome(
    monkeypatch, tmp_path
):
    """Teardown decided completed, and the state doctor ended the row failed between teardown's
    read of it and its write, so teardown's compare-and-set missed: run.json keeps the row's
    status, and its error says this run did not write it and what this run decided, never the
    other writer's reason."""
    from lionagi.state.db import SESSION_TERMINAL_STATUSES, StateDB

    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path, operate_return="done")
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", real_teardown)
    real_update = StateDB.update_status
    missed = []

    async def update_status(self, entity_type, entity_id, **kw):
        terminal_write = (
            entity_type == "session"
            and kw.get("source") == "executor"
            and kw.get("new_status") in SESSION_TERMINAL_STATUSES
        )
        if terminal_write and not missed:
            await _sweep_as_stale(self, entity_id)
        written = await real_update(self, entity_type, entity_id, **kw)
        if terminal_write and not missed:
            missed.append((kw["new_status"], written))
        return written

    monkeypatch.setattr(StateDB, "update_status", update_status)
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    *_, terminal_status, _session_id = await _run_agent("claude/sonnet", "go")

    assert missed == [("completed", False)]
    manifest = json.loads(run.manifest_path.read_text())
    assert terminal_status == manifest["status"] == "failed"
    assert manifest["error"] == (
        "The session row reads failed, which this run's teardown did not write; "
        "this run's own outcome was completed."
    )


@pytest.mark.asyncio
async def test_a_row_ended_before_teardown_and_a_teardown_error_names_this_runs_own_outcome(
    monkeypatch, tmp_path
):
    """The state doctor ended the row failed before teardown began, so teardown skipped its
    write; a later step of teardown then raised, and teardown handed back the row's status: the
    same error, since this run wrote none of it."""
    import lionagi.hooks

    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path, operate_return="done")

    def unroute(*_a, **_kw):
        raise RuntimeError("unroute failed")

    async def teardown(ctx, **kw):
        await _sweep_as_stale(ctx["db"], ctx["session_id"])
        monkeypatch.setattr(lionagi.hooks, "unroute_message_persistence", unroute)
        return await real_teardown(ctx, **kw)

    monkeypatch.setattr(agent_mod, "teardown_agent_persist", teardown)
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    *_, terminal_status, _session_id = await _run_agent("claude/sonnet", "go")

    manifest = json.loads(run.manifest_path.read_text())
    assert terminal_status == manifest["status"] == "failed"
    assert manifest["error"] == (
        "The session row reads failed, which this run's teardown did not write; "
        "this run's own outcome was completed."
    )
    assert _SWEEP_REASON not in manifest["error"]


@pytest.mark.asyncio
async def test_a_failed_run_with_no_decided_reason_and_no_exception_says_none_was_recorded(
    monkeypatch, tmp_path
):
    """Teardown hands back no outcome and nothing raised: run.json says no reason was recorded
    rather than carry no error."""
    agent_mod, _real_teardown = _with_a_real_row(monkeypatch, tmp_path, operate_return="done")

    async def teardown(ctx, **kw):
        return "failed"

    monkeypatch.setattr(agent_mod, "teardown_agent_persist", teardown)
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude/sonnet", "go")

    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["status"] == "failed"
    assert manifest["error"] == "The run failed; no reason was recorded for it."


@pytest.mark.asyncio
async def test_a_completed_run_writes_no_error(monkeypatch, tmp_path):
    _wire_agent_stubs(monkeypatch, tmp_path, operate_return="done")
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude/sonnet", "go")

    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["status"] == "completed"
    assert "error" not in manifest
