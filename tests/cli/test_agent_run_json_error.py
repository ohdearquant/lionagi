# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""A failed `li agent` run's run.json names its error, so the run directory alone tells a
provider's refusal from a crash."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from lionagi.cli._runs import (
    TERMINAL_WRITE_LOST_FIELD,
    RunDir,
    lost_terminal_write,
    resolve_run_reason,
)
from lionagi.state.reasons import RunReasons
from tests.cli.test_agent_context_from import _wire_agent_stubs
from tests.providers.test_claude_code_refusal import REFUSAL

# The store error teardown keeps when a store read or write fails it.
_LOCKED = sqlite3.OperationalError("database is locked")


def test_durable_operations_scopes_manifest_error_to_agent_runs():
    doc = Path(__file__).parents[2] / "docs/guides/durable-operations.md"
    text = doc.read_text()
    state_section = text.split("## Understand local state and artifacts", 1)[1]
    state_section = state_section.split("## ", 1)[0]
    state_section = " ".join(state_section.split())

    assert "A failed run launched with `li agent` also records `error`" in state_section
    assert (
        "Operator runs record their failure reason on the Operator turn instead." in state_section
    )


def _run(monkeypatch, tmp_path) -> RunDir:
    import lionagi.cli.agent as agent_mod

    run = RunDir(
        run_id="run-x", state_root=tmp_path / "state", artifact_root=tmp_path / "artifacts"
    )
    run.ensure_state_dirs()
    monkeypatch.setattr(agent_mod, "allocate_run", lambda: run)
    process_markers = {"pid": 1, "pid_create_time": 1.0}
    monkeypatch.setattr("lionagi.ln._proc.current_pid_markers", lambda: dict(process_markers))
    return run


@pytest.mark.asyncio
async def test_a_claude_cli_refusal_reaches_run_json_and_the_reason_as_a_quota_error(
    monkeypatch, tmp_path
):
    """The whole path from the CLI's exit through the model's stream to the run directory: only
    the CLI is a stand-in, printing the refusal and exiting 1 as the real one does."""
    from lionagi import Branch
    from lionhub.providers.anthropic import claude_code as cc

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
    # another writer's terminal record is not a write this run's store refused
    assert TERMINAL_WRITE_LOST_FIELD not in manifest


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
    assert TERMINAL_WRITE_LOST_FIELD not in manifest


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


# A run whose store did not take its terminal write records what it decided.


def _two_runs(monkeypatch, tmp_path) -> tuple[RunDir, RunDir]:
    """The run a job allocates and the one its auto-resumed leg allocates after it: a second
    allocation in one process takes a run id of its own."""
    import lionagi.cli.agent as agent_mod

    runs = []
    for rid in ("run-job", "run-resumed"):
        run = RunDir(run_id=rid, state_root=tmp_path / rid, artifact_root=tmp_path / rid / "art")
        run.ensure_state_dirs()
        runs.append(run)
    allocated = iter(runs)
    monkeypatch.setattr(agent_mod, "allocate_run", lambda: next(allocated))
    # the resumed leg finds its branch where the job's leg snapshotted it
    monkeypatch.setattr(
        agent_mod, "find_branch", lambda bid: (runs[0].run_id, runs[0].branches_dir / f"{bid}.json")
    )
    return runs[0], runs[1]


def _snapshot_before_operate(monkeypatch) -> None:
    """Wrap the operate a test installed so it first writes the branch snapshot the real one
    writes, which an auto-resumed leg loads its branch from."""
    from lionagi import Branch

    installed = Branch.operate

    async def operate(self, instruction=None, **kw):
        snapshots = Path(kw["snapshot_dir"])
        snapshots.mkdir(parents=True, exist_ok=True)
        (snapshots / f"{self.id}.json").write_text(json.dumps(self.to_dict()))
        return await installed(self, instruction, **kw)

    monkeypatch.setattr(Branch, "operate", operate)


def _refuse_terminal_writes(monkeypatch, *, readback_fails: bool = False) -> list[str]:
    """The store refuses every terminal write a session's teardown makes. With *readback_fails* it
    cannot read the row afterwards either, so teardown has no status to hand back."""
    import sqlite3

    from lionagi.state.db import SESSION_TERMINAL_STATUSES, StateDB

    real_update, real_get = StateDB.update_status, StateDB.get_session
    refused: list[str] = []

    async def update_status(self, entity_type, entity_id, **kw):
        if (
            entity_type == "session"
            and kw.get("source") == "executor"
            and kw.get("new_status") in SESSION_TERMINAL_STATUSES
        ):
            refused.append(kw["new_status"])
            raise sqlite3.OperationalError("database is locked")
        return await real_update(self, entity_type, entity_id, **kw)

    async def get_session(self, session_id):
        if readback_fails and refused:
            raise sqlite3.OperationalError("database is locked")
        return await real_get(self, session_id)

    monkeypatch.setattr(StateDB, "update_status", update_status)
    monkeypatch.setattr(StateDB, "get_session", get_session)
    return refused


@pytest.mark.asyncio
async def test_a_refused_terminal_write_is_recorded_beside_the_status_that_is_durable(
    monkeypatch, tmp_path
):
    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path, operate_return="done")
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", real_teardown)
    refused = _refuse_terminal_writes(monkeypatch)
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    before = time.time()
    *_, terminal_status, _session_id = await _run_agent("claude/sonnet", "go")

    assert refused == ["completed"]
    manifest = json.loads(run.manifest_path.read_text())
    assert terminal_status == manifest["status"] == "running"  # what the store holds
    lost = manifest[TERMINAL_WRITE_LOST_FIELD]  # what the run decided
    assert lost == {"status": "completed", "reason_code": RunReasons.COMPLETED_OK, "at": lost["at"]}
    assert before <= lost["at"] <= time.time()
    assert manifest.get("ended_at") is None


@pytest.mark.asyncio
async def test_a_refused_terminal_write_with_the_row_unreadable_is_recorded_beside_unknown(
    monkeypatch, tmp_path
):
    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path, operate_return="done")
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", real_teardown)
    refused = _refuse_terminal_writes(monkeypatch, readback_fails=True)
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    *_, terminal_status, _session_id = await _run_agent("claude/sonnet", "go")

    assert refused == ["completed"]
    manifest = json.loads(run.manifest_path.read_text())
    assert terminal_status == manifest["status"] == "unknown"
    lost = manifest[TERMINAL_WRITE_LOST_FIELD]
    assert (lost["status"], lost["reason_code"]) == ("completed", RunReasons.COMPLETED_OK)


@pytest.mark.asyncio
async def test_a_refused_write_records_the_status_and_code_teardown_decided_not_the_one_asked(
    monkeypatch, tmp_path
):
    """The run asked for `completed` and teardown decided `failed` for a missing artifact: the
    record carries the decision and its code, not the request."""
    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path, operate_return="done")
    contract = {"expected": [{"id": "report", "path": "report.md", "required": True}]}
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: contract)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", real_teardown)
    refused = _refuse_terminal_writes(monkeypatch)
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude/sonnet", "go")

    assert refused == ["failed"]
    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["status"] == "running"
    lost = manifest[TERMINAL_WRITE_LOST_FIELD]
    assert (lost["status"], lost["reason_code"]) == ("failed", RunReasons.FAILED_MISSING_ARTIFACT)


@pytest.mark.asyncio
async def test_a_refused_verification_write_records_the_missing_artifact_failure_already_decided(
    monkeypatch, tmp_path
):
    """The store refuses the verification write of a completed request whose required report is
    missing. The verification already decided `failed`, so the record carries it and its code,
    not the request."""
    from lionagi.state.db import StateDB

    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path, operate_return="done")
    contract = {"expected": [{"id": "report", "path": "report.md", "required": True}]}
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: contract)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", real_teardown)
    refused: list[str] = []

    async def update_artifact_verification(self, session_id, verification):
        refused.append(verification["status"])
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(StateDB, "update_artifact_verification", update_artifact_verification)
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude/sonnet", "go")

    assert refused == ["failed"]
    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["status"] == "running"
    lost = manifest[TERMINAL_WRITE_LOST_FIELD]
    assert (lost["status"], lost["reason_code"]) == ("failed", RunReasons.FAILED_MISSING_ARTIFACT)


@pytest.mark.asyncio
@pytest.mark.parametrize("store_failed", [True, False], ids=["store-error", "other-error"])
async def test_a_teardown_that_failed_before_deciding_records_the_status_it_was_asked_to_write(
    monkeypatch, tmp_path, store_failed
):
    """No decision was recorded, so the status is the one asked and the code is the one the run's
    exception resolves to (a retryable provider refusal, not the generic failure code). Only a
    failure the store raised leaves the record: any other one left the store able to take a
    write that teardown never reached."""
    from lionagi import Branch
    from lionagi._provider_errors import ProviderQuotaError
    from lionagi.cli import _runs

    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", real_teardown)
    seen = {}

    async def operate(self, instruction=None, **kw):
        raise ProviderQuotaError("quota reached")

    async def flush_fails(ctx):
        seen["decision"] = ctx.get("terminal_reason")
        seen["resolved"] = ctx.get("requested_reason_code")
        if store_failed:
            raise sqlite3.OperationalError("database is locked")
        raise RuntimeError("flush failed")

    monkeypatch.setattr(Branch, "operate", operate)
    monkeypatch.setattr(_runs, "_flush_pending_message_events", flush_fails)
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    with pytest.raises(ProviderQuotaError):
        await _run_agent("claude/sonnet", "go")

    assert seen == {"decision": None, "resolved": None}
    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["status"] == "running"
    if not store_failed:
        assert TERMINAL_WRITE_LOST_FIELD not in manifest
        return
    lost = manifest[TERMINAL_WRITE_LOST_FIELD]
    assert (lost["status"], lost["reason_code"]) == ("failed", RunReasons.FAILED_PROVIDER_RETRYABLE)


@pytest.mark.asyncio
async def test_an_artifact_removed_while_teardown_verified_it_records_no_lost_write(
    monkeypatch, tmp_path
):
    """The required report is removed after the verifier saw it and before it read its size, so
    teardown fails before it decides or writes a terminal status. The store refused nothing:
    the row stays `running` and run.json says so, with no lost write beside it."""
    from lionagi.state import artifact_verifier
    from lionagi.state.db import StateDB

    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path, operate_return="done")
    contract = {"expected": [{"id": "report", "path": "report.md", "required": True}]}
    monkeypatch.setattr(agent_mod, "resolve_artifact_contract", lambda **_: contract)
    run = _run(monkeypatch, tmp_path)
    report = run.artifact_root / "report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("the report")
    real_getsize = artifact_verifier.os.path.getsize
    real_update = StateDB.update_status
    seen = {"terminal_writes": [], "write_errors": []}

    def getsize(path):
        if Path(path) == report and report.exists():
            report.unlink()
        return real_getsize(path)

    async def update_status(self, entity_type, entity_id, **kw):
        if entity_type == "session" and kw.get("new_status") != "running":
            seen["terminal_writes"].append(kw.get("new_status"))
        try:
            return await real_update(self, entity_type, entity_id, **kw)
        except BaseException as exc:
            seen["write_errors"].append(repr(exc))
            raise

    async def teardown(ctx, **kw):
        monkeypatch.setattr(artifact_verifier.os.path, "getsize", getsize)
        status = await real_teardown(ctx, **kw)
        seen["decision"] = ctx.get("terminal_reason")
        seen["store_error"] = ctx.get("teardown_store_error")
        return status

    monkeypatch.setattr(StateDB, "update_status", update_status)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", teardown)

    from lionagi.cli.agent import _run_agent

    *_, terminal_status, session_id = await _run_agent("claude/sonnet", "go")

    assert seen == {
        "terminal_writes": [],
        "write_errors": [],
        "decision": None,
        "store_error": None,
    }
    async with StateDB() as db:
        row = await db.get_session(session_id)
    manifest = json.loads(run.manifest_path.read_text())
    assert terminal_status == row["status"] == manifest["status"] == "running"
    assert TERMINAL_WRITE_LOST_FIELD not in manifest


@pytest.mark.asyncio
async def test_a_run_whose_row_is_gone_at_its_terminal_write_records_the_decided_end(
    monkeypatch, tmp_path
):
    """The session row is deleted before teardown writes the run's end, so the store finds no
    row for the write. The run decided `completed` and the store never took it: run.json keeps
    what is durable (`unknown`, the row cannot be read back) and records the decided end."""
    from lionagi.state.db import StateDB
    from lionagi.state.lifecycle import LifecycleNotFoundError

    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path, operate_return="done")
    run = _run(monkeypatch, tmp_path)
    real_update = StateDB.update_status
    seen = {"terminal_writes": [], "write_errors": []}

    async def update_status(self, entity_type, entity_id, **kw):
        if entity_type == "session" and kw.get("new_status") != "running":
            seen["terminal_writes"].append(kw.get("new_status"))
        try:
            return await real_update(self, entity_type, entity_id, **kw)
        except BaseException as exc:
            seen["write_errors"].append(type(exc.__cause__))
            raise

    async def teardown(ctx, **kw):
        await ctx["db"].execute("DELETE FROM sessions WHERE id = ?", (ctx["session_id"],))
        status = await real_teardown(ctx, **kw)
        seen["decision"] = ctx.get("terminal_reason")
        seen["store_error"] = type(ctx.get("teardown_store_error"))
        return status

    monkeypatch.setattr(StateDB, "update_status", update_status)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", teardown)

    from lionagi.cli.agent import _run_agent

    *_, terminal_status, _ = await _run_agent("claude/sonnet", "go")

    assert seen["terminal_writes"] == ["completed"]
    assert seen["write_errors"] == [LifecycleNotFoundError]
    assert seen["decision"][0] == "completed"
    assert seen["store_error"] is LifecycleNotFoundError
    manifest = json.loads(run.manifest_path.read_text())
    assert terminal_status == manifest["status"] == "unknown"
    lost = manifest[TERMINAL_WRITE_LOST_FIELD]
    assert (lost["status"], lost["reason_code"]) == ("completed", RunReasons.COMPLETED_OK)


@pytest.mark.asyncio
async def test_a_leg_that_defers_its_end_to_an_auto_resume_records_no_lost_write(
    monkeypatch, tmp_path
):
    """The leg timed out and would resume: its teardown deferred the terminal write, so there was
    none for the store to refuse, even when the store then failed teardown and left the row
    running."""
    from lionagi import Branch
    from lionagi.cli import _runs

    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", real_teardown)

    async def operate(self, instruction=None, **kw):
        raise TimeoutError("deadline")

    async def flush_fails(ctx):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Branch, "operate", operate)
    monkeypatch.setattr(_runs, "_flush_pending_message_events", flush_fails)
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude/sonnet", "go", resume_on_timeout=True)

    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["status"] == "running"
    assert TERMINAL_WRITE_LOST_FIELD not in manifest


@pytest.mark.asyncio
async def test_a_teardown_that_decided_running_records_no_lost_write(monkeypatch, tmp_path):
    """An engine session linked to the run still runs, so teardown decides `running`, not an end."""
    from lionagi import Branch
    from lionagi._provider_errors import ProviderError
    from lionagi.state.claude_mirror import session_db_id

    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path)
    engine_uid = "aaaaaaaa-bbbb-cccc-dddd-222222222222"
    seen = {}

    async def operate(self, instruction=None, **kw):
        raise ProviderError("the wrapper failed while the engine session lives")

    async def teardown(ctx, **kw):
        await ctx["db"].create_progression("engine-prog")
        await ctx["db"].create_session(
            {
                "id": session_db_id(engine_uid),
                "created_at": 1.0,
                "progression_id": "engine-prog",
                "name": "engine",
                "status": "running",
                "invocation_kind": "agent",
            }
        )
        status = await real_teardown(ctx, **{**kw, "engine_session_uid": engine_uid})
        seen["decision"] = ctx.get("terminal_reason")
        return status

    monkeypatch.setattr(Branch, "operate", operate)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", teardown)
    run = _run(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    with pytest.raises(ProviderError):
        await _run_agent("claude/sonnet", "go")

    assert seen["decision"][0] == "running"
    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["status"] == "running"
    assert TERMINAL_WRITE_LOST_FIELD not in manifest


@pytest.mark.parametrize(
    ("live", "requested", "returned", "expected"),
    [
        (None, "completed", "running", None),
        (
            {"session_id": "s", "teardown_store_error": _LOCKED},
            "completed",
            "running",
            ("completed", RunReasons.COMPLETED_OK),
        ),
        (
            {
                "session_id": "s",
                "requested_reason_code": RunReasons.FAILED_PROVIDER_RETRYABLE,
                "teardown_store_error": _LOCKED,
            },
            "failed",
            "running",
            ("failed", RunReasons.FAILED_PROVIDER_RETRYABLE),
        ),
        (
            {
                "terminal_reason": ("failed", "s", RunReasons.FAILED_MISSING_ARTIFACT),
                "teardown_store_error": _LOCKED,
            },
            "completed",
            "running",
            ("failed", RunReasons.FAILED_MISSING_ARTIFACT),
        ),
        (
            {
                "terminal_reason": ("failed", "the store refused a write"),
                "teardown_store_error": _LOCKED,
            },
            "completed",
            "running",
            ("failed", RunReasons.FAILED_EXCEPTION),
        ),
        (
            {
                "terminal_reason": ("completed", "s", RunReasons.COMPLETED_OK),
                "teardown_store_error": _LOCKED,
            },
            "completed",
            "unknown",
            ("completed", RunReasons.COMPLETED_OK),
        ),
        ({"session_id": "s"}, "completed", "running", None),
        (
            {"terminal_reason": ("completed", "s", RunReasons.COMPLETED_OK)},
            "completed",
            "running",
            None,
        ),
        (
            {
                "terminal_reason": ("running", "s", RunReasons.STARTED_OK),
                "teardown_store_error": _LOCKED,
            },
            "failed",
            "running",
            None,
        ),
        (
            {"terminal_reason": ("completed", "s", RunReasons.COMPLETED_OK)},
            "completed",
            "completed",
            None,
        ),
        (
            {"terminal_reason": ("completed", "s", RunReasons.COMPLETED_OK)},
            "completed",
            "failed",
            None,
        ),
        (
            {"terminal_reason": ("failed", "s", RunReasons.FAILED_EXCEPTION)},
            "failed",
            "cancelled",
            None,
        ),
        ({"session_id": "s", "teardown_store_error": _LOCKED}, "running", "running", None),
    ],
    ids=[
        "no-persistence",
        "no-decision-falls-back-to-the-request",
        "no-decision-takes-the-code-teardown-resolved",
        "decision-wins-over-the-request",
        "two-field-decision-resolves-its-code",
        "unknown-readback",
        "no-store-error-before-deciding",
        "no-store-error-after-deciding",
        "decided-running",
        "clean-write",
        "another-writers-terminal-status",
        "another-terminal-status-after-a-failure",
        "nothing-terminal-was-asked",
    ],
)
def test_the_lost_write_predicate_fires_only_for_a_terminal_decision_and_a_non_terminal_return(
    live, requested, returned, expected
):
    before = time.time()
    record = lost_terminal_write(live, requested=requested, exception=None, returned=returned)

    if expected is None:
        assert record is None
        return
    assert (record["status"], record["reason_code"]) == expected
    assert set(record) == {"status", "reason_code", "at"}
    assert before <= record["at"] <= time.time()


def _store_error_cases():
    import sqlite3

    from sqlalchemy.exc import OperationalError

    from lionagi.ln.concurrency._compat import BaseExceptionGroup
    from lionagi.state.lifecycle import LifecycleNotFoundError

    locked = sqlite3.OperationalError("database is locked")
    wrapped = OperationalError("UPDATE sessions", {}, sqlite3.OperationalError("disk I/O error"))
    raised_from = RuntimeError("teardown write failed")
    raised_from.__cause__ = locked
    while_handling = FileNotFoundError("report.md")
    while_handling.__context__ = locked
    nested = BaseExceptionGroup(
        "teardown", [ValueError("other"), BaseExceptionGroup("inner", [locked])]
    )
    # StateDB.update_status raises LookupError from the lifecycle service's refusal.
    no_row = LifecycleNotFoundError("session 's' not found")
    row_gone = LookupError("session 's' not found (table=sessions)")
    row_gone.__cause__ = no_row
    return [
        (locked, locked),
        (wrapped, wrapped),
        (raised_from, locked),
        (nested, locked),
        (FileNotFoundError("report.md"), None),
        (while_handling, None),
        (BaseExceptionGroup("teardown", [ValueError("a"), RuntimeError("b")]), None),
        (row_gone, no_row),
        (KeyError("report"), None),
    ]


@pytest.mark.parametrize(
    ("error", "expected"),
    _store_error_cases(),
    ids=[
        "sqlite-error",
        "sqlalchemy-error",
        "raised-from-a-store-error",
        "inside-nested-groups",
        "filesystem-error",
        "raised-while-handling-a-store-error",
        "group-without-a-store-error",
        "no-row-for-the-write",
        "a-lookup-error-from-elsewhere",
    ],
)
def test_a_store_error_is_found_where_teardown_can_carry_one(error, expected):
    from lionagi.cli._runs import _store_error_in

    assert _store_error_in(error) is expected


class _CauseCycle(RuntimeError):
    """An error whose cause is its partner. Reading the cause ten times means the search is
    walking the cycle again, so the read raises instead of letting the search loop."""

    def __init__(self, name):
        super().__init__(name)
        self.partner = None
        self.reads = 0

    @property
    def __cause__(self):
        self.reads += 1
        if self.reads >= 10:
            raise AssertionError(f"cause of {self} read {self.reads} times")
        return self.partner


def test_a_cause_cycle_without_a_store_error_ends():
    """Two errors that name each other as their cause: the search visits each once and finds
    nothing."""
    from lionagi.cli._runs import _store_error_in

    first, second = _CauseCycle("a"), _CauseCycle("b")
    first.partner, second.partner = second, first

    assert _store_error_in(first) is None


@pytest.mark.asyncio
async def test_a_resumed_legs_lost_write_reaches_the_run_json_the_job_reads(monkeypatch, tmp_path):
    """The run timed out and resumed itself. The resumed leg, in a run directory of its own,
    completed and the store refused its terminal write: its run.json records it beside the
    status that is durable, and so does the run.json of the run it resumed for, the one the job
    reads, which keeps the status that run wrote."""
    from lionagi import Branch

    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", real_teardown)
    legs = []

    async def operate(self, instruction=None, **kw):
        legs.append(instruction)
        if len(legs) == 1:
            raise TimeoutError("deadline")
        return "done"

    monkeypatch.setattr(Branch, "operate", operate)
    _snapshot_before_operate(monkeypatch)
    refused = _refuse_terminal_writes(monkeypatch)
    job, resumed = _two_runs(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude/sonnet", "go", resume_on_timeout=True)

    assert len(legs) == 2 and refused == ["completed"]
    resumed_manifest = json.loads(resumed.manifest_path.read_text())
    job_manifest = json.loads(job.manifest_path.read_text())
    assert resumed_manifest["status"] == "running"
    assert job_manifest["status"] == "timed_out"
    for manifest in (resumed_manifest, job_manifest):
        lost = manifest[TERMINAL_WRITE_LOST_FIELD]
        assert (lost["status"], lost["reason_code"]) == ("completed", RunReasons.COMPLETED_OK)
    assert job_manifest[TERMINAL_WRITE_LOST_FIELD] == resumed_manifest[TERMINAL_WRITE_LOST_FIELD]


@pytest.mark.asyncio
async def test_the_copied_lost_write_leaves_what_another_invocation_wrote(monkeypatch, tmp_path):
    """While the resumed leg ran, another invocation sharing the first run's id wrote that run's
    run.json. The copy adds the lost write and leaves the rest of the file as that writer left it."""
    from lionagi import Branch

    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", real_teardown)
    legs = []
    runs = {}

    async def operate(self, instruction=None, **kw):
        legs.append(instruction)
        if len(legs) == 1:
            raise TimeoutError("deadline")
        path = runs["job"].manifest_path
        manifest = json.loads(path.read_text())
        manifest.update({"status": "cancelled", "written_by": "another invocation"})
        path.write_text(json.dumps(manifest))
        return "done"

    monkeypatch.setattr(Branch, "operate", operate)
    _snapshot_before_operate(monkeypatch)
    refused = _refuse_terminal_writes(monkeypatch)
    runs["job"], _resumed = _two_runs(monkeypatch, tmp_path)

    from lionagi.cli.agent import _run_agent

    await _run_agent("claude/sonnet", "go", resume_on_timeout=True)

    assert len(legs) == 2 and refused == ["completed"]
    job_manifest = json.loads(runs["job"].manifest_path.read_text())
    assert (job_manifest["status"], job_manifest["written_by"]) == (
        "cancelled",
        "another invocation",
    )
    lost = job_manifest[TERMINAL_WRITE_LOST_FIELD]
    assert (lost["status"], lost["reason_code"]) == ("completed", RunReasons.COMPLETED_OK)


def test_a_lost_write_with_no_code_from_teardown_reads_the_sigterm_latch_once(monkeypatch):
    """A cancelled run's teardown failed before resolving a code: the record names the external
    SIGTERM and consumes the latch, so a later cancellation in the process is not read as one."""
    import threading

    from lionagi.ln.concurrency import utils as cu

    monkeypatch.setattr(cu, "_SIGTERM_RECEIVED", threading.Event())
    cu._SIGTERM_RECEIVED.set()

    lost = lost_terminal_write(
        {"session_id": "s", "teardown_store_error": _LOCKED},
        requested="cancelled",
        exception=None,
        returned="running",
    )

    assert lost["reason_code"] == RunReasons.CANCELLED_SIGTERM
    assert not cu.sigterm_received()


def test_a_lost_write_takes_the_code_teardown_resolved_and_leaves_the_latch_alone(monkeypatch):
    """Teardown resolved the code before a write failed, consuming the latch then: the record
    takes that code and does not read the latch again."""
    import threading

    from lionagi.ln.concurrency import utils as cu

    monkeypatch.setattr(cu, "_SIGTERM_RECEIVED", threading.Event())

    lost = lost_terminal_write(
        {
            "session_id": "s",
            "requested_reason_code": RunReasons.CANCELLED_SIGTERM,
            "teardown_store_error": _LOCKED,
        },
        requested="cancelled",
        exception=None,
        returned="running",
    )

    assert lost["reason_code"] == RunReasons.CANCELLED_SIGTERM


@pytest.mark.asyncio
async def test_a_sigterm_run_whose_teardown_failed_before_deciding_records_the_sigterm(
    monkeypatch, tmp_path
):
    """An external SIGTERM cancels the run and teardown fails before it resolves a code: the
    record names the SIGTERM beside the durable `running`, and the latch is consumed."""
    import asyncio
    import sqlite3
    import threading

    from lionagi import Branch
    from lionagi.cli import _runs
    from lionagi.ln.concurrency import utils as cu

    monkeypatch.setattr(cu, "_SIGTERM_RECEIVED", threading.Event())
    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path)
    monkeypatch.setattr(agent_mod, "teardown_agent_persist", real_teardown)
    run = _run(monkeypatch, tmp_path)

    async def fail_before_deciding(_ctx):
        raise sqlite3.OperationalError("database is locked")

    async def cancelled(self, instruction=None, **kwargs):
        cu._SIGTERM_RECEIVED.set()
        raise asyncio.CancelledError()

    monkeypatch.setattr(_runs, "_flush_pending_message_events", fail_before_deciding)
    monkeypatch.setattr(Branch, "operate", cancelled)

    from lionagi.cli.agent import _run_agent

    with pytest.raises(asyncio.CancelledError):
        await _run_agent("claude/sonnet", "go")

    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["status"] == "running"
    lost = manifest[TERMINAL_WRITE_LOST_FIELD]
    assert (lost["status"], lost["reason_code"]) == ("cancelled", RunReasons.CANCELLED_SIGTERM)
    assert not cu.sigterm_received()


@pytest.mark.asyncio
async def test_a_sigterm_run_whose_write_failed_after_teardown_resolved_its_code_records_the_sigterm(
    monkeypatch, tmp_path
):
    """Teardown resolved the run's code, consuming the SIGTERM latch, and a write after it failed
    before the outcome was decided: the record still names the SIGTERM, from the code teardown
    resolved, where a second read of the latch would find it clear."""
    import asyncio
    import sqlite3
    import threading

    from lionagi import Branch
    from lionagi.ln.concurrency import utils as cu
    from lionagi.state.db import StateDB

    monkeypatch.setattr(cu, "_SIGTERM_RECEIVED", threading.Event())
    agent_mod, real_teardown = _with_a_real_row(monkeypatch, tmp_path)
    seen = {}

    async def teardown(ctx, **kw):
        status = await real_teardown(ctx, **kw)
        seen["decision"] = ctx.get("terminal_reason")
        seen["resolved"] = ctx.get("requested_reason_code")
        return status

    monkeypatch.setattr(agent_mod, "teardown_agent_persist", teardown)
    run = _run(monkeypatch, tmp_path)

    async def verification_refused(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    async def cancelled(self, instruction=None, **kwargs):
        cu._SIGTERM_RECEIVED.set()
        raise asyncio.CancelledError()

    monkeypatch.setattr(StateDB, "update_artifact_verification", verification_refused)
    monkeypatch.setattr(Branch, "operate", cancelled)

    from lionagi.cli.agent import _run_agent

    with pytest.raises(asyncio.CancelledError):
        await _run_agent("claude/sonnet", "go")

    assert seen == {"decision": None, "resolved": RunReasons.CANCELLED_SIGTERM}
    manifest = json.loads(run.manifest_path.read_text())
    assert manifest["status"] == "running"
    lost = manifest[TERMINAL_WRITE_LOST_FIELD]
    assert (lost["status"], lost["reason_code"]) == ("cancelled", RunReasons.CANCELLED_SIGTERM)
    assert not cu.sigterm_received()
