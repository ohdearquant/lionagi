# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li agent status` / `li play status` / `li o ctl status` —
read-only lifecycle surfaces that resolve BY ID regardless of terminal
state (ADR-0085 section 6)."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from lionagi.cli.status import (
    EXIT_RUNNING,
    EXIT_UNKNOWN,
    _audit_degraded,
    _build_view,
    _classify,
    _detect_degraded,
    _dispatch,
    _resolve_agent_target,
    _resolve_any_target,
    _resolve_play_target,
    _run_status,
    run_agent_status,
    run_ctl_status,
    run_play_status,
)
from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test temp DB; patch DEFAULT_DB_PATH so StateDB() opens it."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


@pytest.fixture
def no_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force detect_project() to (None, None) so default-latest resolution
    isn't accidentally scoped to whatever project the test runner's own cwd
    happens to resolve to."""
    monkeypatch.setattr("lionagi.cli.status.detect_project", lambda cwd: (None, None))


async def _make_session(
    db: StateDB,
    *,
    status: str = "running",
    project: str | None = None,
    invocation_kind: str | None = "agent",
    model: str | None = "claude-3-5-sonnet",
    provider: str | None = "anthropic",
    invocation_id: str | None = None,
    source_kind: str | None = None,
) -> str:
    sid = uuid.uuid4().hex[:12]
    pid = uuid.uuid4().hex
    await db.create_progression(pid)
    await db.create_session(
        {
            "id": sid,
            "progression_id": pid,
            "status": status,
            "invocation_kind": invocation_kind,
            "project": project,
            "model": model,
            "provider": provider,
            "started_at": time.time(),
            "invocation_id": invocation_id,
            "source_kind": source_kind,
        }
    )
    return sid


async def _make_invocation(db: StateDB, *, status: str = "running", skill: str = "show") -> str:
    inv_id = uuid.uuid4().hex[:12]
    await db.create_invocation(
        {
            "id": inv_id,
            "skill": skill,
            "started_at": time.time(),
            "status": status,
        }
    )
    return inv_id


async def _make_show(db: StateDB, *, status: str = "active", topic: str = "test-topic") -> str:
    show_id = uuid.uuid4().hex[:12]
    await db.create_show(
        {
            "id": show_id,
            "topic": topic,
            "status": status,
            "show_dir": "/tmp/show",
        }
    )
    return show_id


async def _make_play(
    db: StateDB,
    show_id: str,
    *,
    status: str = "running",
    name: str = "play-1",
    session_id: str | None = None,
) -> str:
    play_id = uuid.uuid4().hex[:12]
    await db.create_play(
        {
            "id": play_id,
            "show_id": show_id,
            "name": name,
            "status": status,
            "started_at": time.time(),
            "session_id": session_id,
        }
    )
    return play_id


async def _set_fields(db: StateDB, table: str, id_: str, **fields) -> None:
    """Raw column UPDATE for fields create_session()/create_play() don't
    expose directly (current_phase, num_turns) — mirrors the pattern
    test_monitor.py uses for `updated_at` backdating."""
    sets = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*fields.values(), id_))  # noqa: S608


# ── Unit: _classify ───────────────────────────────────────────────────────


def test_classify_session_success():
    assert _classify("session", "completed") == (True, "success", 0)


def test_classify_session_failure():
    assert _classify("session", "failed") == (True, "failure", 1)


def test_classify_session_cancelled_is_failure():
    """'cancelled' has no literal mention in the ADR-0085 exit-code list but
    is a real terminal non-success status — lands in failure by elimination."""
    assert _classify("session", "cancelled") == (True, "failure", 1)


def test_classify_session_timed_out_is_failure():
    assert _classify("session", "timed_out") == (True, "failure", 1)


def test_classify_session_running():
    assert _classify("session", "running") == (False, "running", EXIT_RUNNING)


def test_classify_invocation_shares_session_vocabulary():
    assert _classify("invocation", "completed") == (True, "success", 0)
    assert _classify("invocation", "failed") == (True, "failure", 1)


def test_classify_play_success():
    assert _classify("play", "merged") == (True, "success", 0)


def test_classify_play_failure_gate_failed():
    assert _classify("play", "gate_failed") == (True, "failure", 1)


def test_classify_play_failure_escalated_and_blocked():
    assert _classify("play", "escalated") == (True, "failure", 1)
    assert _classify("play", "blocked") == (True, "failure", 1)


@pytest.mark.parametrize(
    "status", ["pending", "prepared", "running", "running_complete", "gated", "redoing"]
)
def test_classify_play_running_states(status):
    assert _classify("play", status) == (False, "running", EXIT_RUNNING)


# ── Unit: _detect_degraded ──────────────────────────────────────────────────


def test_detect_degraded_healthy_completed():
    primary = {"num_turns": 5, "source_kind": "live", "current_phase": None}
    assert _detect_degraded(entity_type="session", status="completed", primary_session=primary) == (
        False,
        None,
    )


def test_detect_degraded_missing_metrics_flagged():
    primary = {"num_turns": None, "source_kind": "live", "current_phase": "synthesizing"}
    degraded, reason = _detect_degraded(
        entity_type="session", status="completed", primary_session=primary
    )
    assert degraded is True
    assert "num_turns" in reason
    assert "synthesizing" in reason


def test_detect_degraded_missing_metrics_no_phase_still_flagged():
    """current_phase is only enrichment text — num_turns is the real signal."""
    primary = {"num_turns": None, "source_kind": None, "current_phase": None}
    degraded, reason = _detect_degraded(
        entity_type="session", status="completed", primary_session=primary
    )
    assert degraded is True
    assert reason is not None


def test_detect_degraded_excludes_imported_fs_mirror():
    """Mirrored Claude Code transcripts never carry num_turns by design —
    must not be flagged (claude_mirror.reconcile_session_status)."""
    primary = {"num_turns": None, "source_kind": "imported_fs", "current_phase": None}
    assert _detect_degraded(entity_type="session", status="completed", primary_session=primary) == (
        False,
        None,
    )


def test_detect_degraded_non_terminal_status_not_flagged():
    primary = {"num_turns": None, "source_kind": "live", "current_phase": "executing"}
    assert _detect_degraded(entity_type="session", status="running", primary_session=primary) == (
        False,
        None,
    )


def test_detect_degraded_no_primary_session():
    assert _detect_degraded(entity_type="session", status="completed", primary_session=None) == (
        False,
        None,
    )


def test_detect_degraded_play_success_scoped_to_backing_session():
    primary = {"num_turns": None, "source_kind": "live", "current_phase": "synthesizing"}
    degraded, _ = _detect_degraded(entity_type="play", status="merged", primary_session=primary)
    assert degraded is True


# ── Integration: entity resolution ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_agent_target_by_id(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(db)
        result = await _resolve_agent_target(db, sid, None)
    assert result is not None
    entity_type, row = result
    assert entity_type == "session"
    assert row["id"] == sid


@pytest.mark.asyncio
async def test_resolve_agent_target_prefix_match(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(db)
        result = await _resolve_agent_target(db, sid[:6], None)
    assert result is not None
    assert result[1]["id"] == sid


@pytest.mark.asyncio
async def test_resolve_agent_target_falls_back_to_invocation(temp_db_path: Path):
    async with StateDB() as db:
        inv_id = await _make_invocation(db)
        result = await _resolve_agent_target(db, inv_id, None)
    assert result is not None
    assert result[0] == "invocation"


@pytest.mark.asyncio
async def test_resolve_agent_target_explicit_id_ignores_kind_scoping(temp_db_path: Path):
    """An explicit id is honoured regardless of invocation_kind — only the
    no-id 'latest' default is kind-scoped."""
    async with StateDB() as db:
        sid = await _make_session(db, invocation_kind="play")
        result = await _resolve_agent_target(db, sid, None)
    assert result is not None
    assert result[1]["id"] == sid


@pytest.mark.asyncio
async def test_resolve_play_target_falls_back_to_play_table(temp_db_path: Path):
    async with StateDB() as db:
        show_id = await _make_show(db)
        play_id = await _make_play(db, show_id)
        result = await _resolve_play_target(db, play_id, None)
    assert result is not None
    assert result[0] == "play"


@pytest.mark.asyncio
async def test_resolve_any_target_no_kind_scoping(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(db, invocation_kind="play")
        result = await _resolve_any_target(db, sid)
    assert result is not None
    assert result[1]["id"] == sid


@pytest.mark.asyncio
async def test_resolve_unknown_id_returns_none(temp_db_path: Path):
    async with StateDB() as db:
        result = await _resolve_agent_target(db, "0" * 40, None)
    assert result is None


# ── Integration: the mandated scenarios ──────────────────────────────────────


@pytest.mark.asyncio
async def test_terminal_run_visible_by_id(temp_db_path: Path):
    """THE INVISIBILITY FIX: a completed run is excluded from `li monitor`'s
    table (running/active only) but MUST resolve here by id."""
    async with StateDB() as db:
        sid = await _make_session(db, status="completed")
        await db.update_status(
            "session", sid, new_status="completed", reason_code=RunReasons.COMPLETED_OK
        )
    output, exit_code = await _run_status(command="agent", entity_id=sid, as_json=True)
    view = json.loads(output)
    assert exit_code == 0
    assert view["id"] == sid
    assert view["terminal"] is True


@pytest.mark.asyncio
async def test_running_session_exit_running(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(db, status="running")
    _, exit_code = await _run_status(command="agent", entity_id=sid, as_json=True)
    assert exit_code == EXIT_RUNNING


@pytest.mark.asyncio
async def test_success_exit_0(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(db, status="completed")
        await db.update_status(
            "session", sid, new_status="completed", reason_code=RunReasons.COMPLETED_OK
        )
    _, exit_code = await _run_status(command="agent", entity_id=sid, as_json=True)
    assert exit_code == 0


@pytest.mark.asyncio
async def test_failure_missing_artifact_exit_1_with_reason_and_evidence(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(db, status="running")
        await db.update_status(
            "session",
            sid,
            new_status="failed",
            reason_code=RunReasons.FAILED_MISSING_ARTIFACT,
            reason_summary="expected artifact 'report.md' was not written",
            evidence_refs=[{"kind": "artifact", "label": "expected artifact", "path": "report.md"}],
        )
    output, exit_code = await _run_status(command="agent", entity_id=sid, as_json=True)
    view = json.loads(output)
    assert exit_code == 1
    assert view["exit_class"] == "failure"
    assert view["status_reason_code"] == "run.failed.missing_artifact"
    assert "report.md" in view["status_reason_summary"]
    assert view["status_evidence_refs"] == [
        {"kind": "artifact", "label": "expected artifact", "path": "report.md"}
    ]

    # Human-readable render surfaces the same reason + evidence.
    human, human_exit = await _run_status(command="agent", entity_id=sid, as_json=False)
    assert human_exit == 1
    assert "run.failed.missing_artifact" in human
    assert "report.md" in human
    assert "evidence" in human.lower()


@pytest.mark.asyncio
async def test_json_stable_shape(temp_db_path: Path):
    """--json emits exactly the documented flat key set — no more, no less."""
    async with StateDB() as db:
        sid = await _make_session(db, status="running")
    output, _ = await _run_status(command="agent", entity_id=sid, as_json=True)
    view = json.loads(output)
    expected_keys = {
        "id",
        "entity_type",
        "command",
        "status",
        "terminal",
        "exit_class",
        "exit_code",
        "current_phase",
        "progress_completed",
        "progress_total",
        "model",
        "provider",
        "project",
        "last_activity_at",
        "session_id",
        "branch_id",
        "invocation_id",
        "label",
        "degraded",
        "degraded_reason",
        "status_reason_code",
        "status_reason_summary",
        "status_evidence_refs",
    }
    assert set(view.keys()) == expected_keys


@pytest.mark.asyncio
async def test_default_latest_resolution(temp_db_path: Path, no_project):
    async with StateDB() as db:
        sid_old = await _make_session(db, status="completed")
        await _set_fields(db, "sessions", sid_old, updated_at=time.time() - 3600)
        sid_new = await _make_session(db, status="running")
    output, _ = await _run_status(command="agent", entity_id=None, as_json=True)
    view = json.loads(output)
    assert view["id"] == sid_new


@pytest.mark.asyncio
async def test_default_latest_scoped_to_invocation_kind(temp_db_path: Path, no_project):
    """`li agent status` (no id) must only ever default to an agent-kind
    session, even when a newer play-kind session exists."""
    async with StateDB() as db:
        await _make_session(db, status="running", invocation_kind="agent")
        agent_sid = await _make_session(db, status="running", invocation_kind="agent")
        await _make_session(db, status="running", invocation_kind="play")
    output, _ = await _run_status(command="agent", entity_id=None, as_json=True)
    view = json.loads(output)
    assert view["id"] == agent_sid
    assert view["entity_type"] == "session"


@pytest.mark.asyncio
async def test_unknown_id_exit_unknown(temp_db_path: Path):
    async with StateDB() as db:
        await _make_session(db)
    output, exit_code = await _run_status(
        command="agent", entity_id="no-such-id-999", as_json=False
    )
    assert exit_code == EXIT_UNKNOWN
    assert "no" in output.lower()


@pytest.mark.asyncio
async def test_no_db_exit_unknown(temp_db_path: Path):
    """DEFAULT_DB_PATH points somewhere nothing has ever written to."""
    output, exit_code = await _run_status(command="agent", entity_id=None, as_json=False)
    assert exit_code == EXIT_UNKNOWN
    assert "state.db" in output


@pytest.mark.asyncio
async def test_play_status_resolves_via_play_table(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(db, status="completed", invocation_kind="play", project="demo")
        await db.update_status(
            "session", sid, new_status="completed", reason_code=RunReasons.COMPLETED_OK
        )
        show_id = await _make_show(db)
        play_id = await _make_play(
            db, show_id, status="merged", name="backend-slice", session_id=sid
        )
    output, exit_code = await _run_status(command="play", entity_id=play_id, as_json=True)
    view = json.loads(output)
    assert exit_code == 0
    assert view["entity_type"] == "play"
    assert view["label"] == "backend-slice"
    assert view["project"] == "demo"  # inherited from backing session
    assert view["session_id"] == sid


@pytest.mark.asyncio
async def test_agent_status_resolves_by_invocation_id(temp_db_path: Path):
    async with StateDB() as db:
        inv_id = await _make_invocation(db, status="completed", skill="show")
    output, exit_code = await _run_status(command="agent", entity_id=inv_id, as_json=True)
    view = json.loads(output)
    assert exit_code == 0
    assert view["entity_type"] == "invocation"
    assert view["label"] == "show"


@pytest.mark.asyncio
async def test_branch_id_surfaced_as_resume_handle(temp_db_path: Path):
    """`-r/--resume` resumes by branch_id, not session_id — the human render
    must show the branch id, not the session id, in the resume hint."""
    async with StateDB() as db:
        sid = await _make_session(db, status="running")
        bpid = uuid.uuid4().hex
        await db.create_progression(bpid)
        branch_id = uuid.uuid4().hex[:12]
        await db.create_branch(
            {
                "id": branch_id,
                "session_id": sid,
                "progression_id": bpid,
                "model": "claude-3-5-sonnet",
            }
        )
    output, _ = await _run_status(command="agent", entity_id=sid, as_json=True)
    view = json.loads(output)
    assert view["branch_id"] == branch_id

    human, _ = await _run_status(command="agent", entity_id=sid, as_json=False)
    assert branch_id in human
    assert f"-r {branch_id}" in human


# ── Integration: degraded marker wired through _build_view ─────────────────


@pytest.mark.asyncio
async def test_view_flags_degraded_completed_flow(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(db, status="completed", invocation_kind="flow")
        await db.update_status(
            "session", sid, new_status="completed", reason_code=RunReasons.COMPLETED_OK
        )
        await _set_fields(db, "sessions", sid, current_phase="synthesizing")
        row = await db.get_session(sid)
        view = await _build_view(db, command="play", entity_type="session", row=row)
    assert view["degraded"] is True
    assert view["degraded_reason"] is not None
    assert view["current_phase"] == "synthesizing"


@pytest.mark.asyncio
async def test_view_not_degraded_when_num_turns_present(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(db, status="completed", invocation_kind="flow")
        await db.update_status(
            "session", sid, new_status="completed", reason_code=RunReasons.COMPLETED_OK
        )
        await _set_fields(db, "sessions", sid, num_turns=7)
        row = await db.get_session(sid)
        view = await _build_view(db, command="play", entity_type="session", row=row)
    assert view["degraded"] is False
    assert view["degraded_reason"] is None


@pytest.mark.asyncio
async def test_view_not_degraded_for_imported_fs_mirror(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(
            db, status="completed", invocation_kind="flow", source_kind="imported_fs"
        )
        await db.update_status(
            "session", sid, new_status="completed", reason_code=RunReasons.COMPLETED_OK
        )
        row = await db.get_session(sid)
        view = await _build_view(db, command="play", entity_type="session", row=row)
    assert view["degraded"] is False


# ── Integration: --audit-degraded ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_degraded_empty_db(temp_db_path: Path):
    async with StateDB() as db:
        result = await _audit_degraded(db)
    assert result == {
        "sessions_scanned": 0,
        "sessions_degraded": 0,
        "plays_scanned": 0,
        "plays_degraded": 0,
        "total_degraded": 0,
    }


@pytest.mark.asyncio
async def test_audit_degraded_counts_sessions_and_plays(temp_db_path: Path):
    async with StateDB() as db:
        # Two degraded flow sessions (no num_turns), one healthy.
        degraded1 = await _make_session(db, status="completed", invocation_kind="flow")
        await db.update_status(
            "session", degraded1, new_status="completed", reason_code=RunReasons.COMPLETED_OK
        )
        degraded2 = await _make_session(db, status="completed", invocation_kind="play")
        await db.update_status(
            "session", degraded2, new_status="completed", reason_code=RunReasons.COMPLETED_OK
        )
        healthy = await _make_session(db, status="completed", invocation_kind="flow")
        await db.update_status(
            "session", healthy, new_status="completed", reason_code=RunReasons.COMPLETED_OK
        )
        await _set_fields(db, "sessions", healthy, num_turns=3)

        # A play row backed by the degraded session, and one backed by the healthy one.
        show_id = await _make_show(db)
        await _make_play(db, show_id, status="merged", name="p-degraded", session_id=degraded1)
        await _make_play(db, show_id, status="merged", name="p-healthy", session_id=healthy)

        result = await _audit_degraded(db)

    assert result["sessions_scanned"] == 3  # all 3 are invocation_kind IN (play, flow)
    assert result["sessions_degraded"] == 2
    assert result["plays_scanned"] == 2
    assert result["plays_degraded"] == 1
    assert result["total_degraded"] == 3


# ── Integration: argparse wiring / dispatch ─────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_agent_success_prints_and_returns_0(temp_db_path: Path, capsys):
    async with StateDB() as db:
        sid = await _make_session(db, status="completed")
        await db.update_status(
            "session", sid, new_status="completed", reason_code=RunReasons.COMPLETED_OK
        )
    exit_code = _dispatch("agent", sid, True)
    assert exit_code == 0
    out = capsys.readouterr().out
    assert json.loads(out)["id"] == sid


@pytest.mark.asyncio
async def test_dispatch_unknown_id_logs_error_and_returns_unknown(
    temp_db_path: Path, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level(logging.ERROR, logger="lionagi.cli.error"):
        exit_code = _dispatch("agent", "totally-unknown-id", False)
    assert exit_code == EXIT_UNKNOWN
    assert "no" in caplog.text.lower()


def test_run_agent_status_parses_id_and_json_flag(temp_db_path: Path):
    """No id, empty db → graceful EXIT_UNKNOWN rather than an argparse crash."""
    assert run_agent_status([]) == EXIT_UNKNOWN
    assert run_agent_status(["--json"]) == EXIT_UNKNOWN


@pytest.mark.asyncio
async def test_run_play_status_audit_degraded_flag(temp_db_path: Path, capsys):
    async with StateDB() as db:  # ensure state.db exists before the sync dispatch call
        pass
    exit_code = run_play_status(["--audit-degraded", "--json"])
    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "sessions_scanned": 0,
        "sessions_degraded": 0,
        "plays_scanned": 0,
        "plays_degraded": 0,
        "total_degraded": 0,
    }


def test_run_ctl_status_dispatches_generic_lookup(temp_db_path: Path):
    import argparse

    args = argparse.Namespace(id="no-such-id", as_json=False)
    assert run_ctl_status(args) == EXIT_UNKNOWN


def test_cli_agent_status_help_subprocess():
    result = subprocess.run(
        [sys.executable, "-m", "lionagi.cli", "agent", "status", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "json" in result.stdout.lower()


def test_cli_play_status_help_subprocess():
    result = subprocess.run(
        [sys.executable, "-m", "lionagi.cli", "play", "status", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "audit-degraded" in result.stdout.lower()
