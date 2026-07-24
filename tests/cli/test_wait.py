# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li wait <id>...` — the ADR-0035 run completion contract: one
frozen tab-delimited line per run (`status=`/`reason=`/`artifact_dir=`/
`exit_code=`), any run kind (session, play, invocation, schedule_run)."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from lionagi._paths import RUNS_ROOT
from lionagi.cli.status import EXIT_UNKNOWN
from lionagi.cli.wait import format_wait_line, run_wait, wait_for_terminal
from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


def _uid() -> str:
    return uuid.uuid4().hex[:12]


async def _make_session(
    db: StateDB, *, status: str = "completed", artifacts_path: str | None = "/tmp/run-dir"
) -> str:
    prog_id = str(uuid.uuid4())
    await db.create_progression(prog_id)
    sid = _uid()
    await db.create_session({"id": sid, "progression_id": prog_id, "status": status})
    if artifacts_path is not None:
        await db.execute(
            "UPDATE sessions SET artifacts_path = ? WHERE id = ?", (artifacts_path, sid)
        )
    return sid


async def _make_show(db: StateDB) -> str:
    show_id = _uid()
    await db.create_show(
        {"id": show_id, "topic": f"topic-{show_id}", "show_dir": f"/tmp/show-{show_id}"}
    )
    return show_id


async def _make_play(
    db: StateDB, show_id: str, *, status: str = "merged", session_id: str | None = None
) -> str:
    play_id = _uid()
    await db.create_play(
        {
            "id": play_id,
            "show_id": show_id,
            "name": f"play-{play_id}",
            "status": status,
            "session_id": session_id,
        }
    )
    return play_id


async def _make_invocation(db: StateDB, *, status: str = "completed") -> str:
    iid = _uid()
    await db.create_invocation(
        {"id": iid, "skill": "test:wait", "started_at": 0.0, "status": status}
    )
    return iid


async def _make_schedule(db: StateDB) -> str:
    sid = _uid()
    await db.create_schedule(
        {
            "id": sid,
            "name": f"sched-{sid}",
            "trigger_type": "interval",
            "interval_sec": 60,
            "action_kind": "agent",
        }
    )
    return sid


async def _make_schedule_run(
    db: StateDB, schedule_id: str, *, status: str = "completed", invocation_id: str | None = None
) -> str:
    rid = _uid()
    await db.create_schedule_run(
        {
            "id": rid,
            "schedule_id": schedule_id,
            "invocation_id": invocation_id,
            "trigger_context": {},
            "action_kind": "agent",
            "action_args": [],
            "status": status,
            "fired_at": 0.0,
        }
    )
    return rid


# ── format_wait_line ─────────────────────────────────────────────────────────


def test_format_wait_line_is_one_frozen_tab_delimited_line() -> None:
    line = format_wait_line(
        {
            "run_id": "abc123",
            "status": "completed",
            "reason": "run.completed.evidence_present",
            "artifact_dir": "/tmp/run-dir",
            "exit_code": 0,
        }
    )
    assert (
        line
        == "abc123\tstatus=completed\treason=run.completed.evidence_present\tartifact_dir=/tmp/run-dir\texit_code=0"
    )


def test_format_wait_line_uses_dash_for_missing_exit_code_and_artifact_dir() -> None:
    line = format_wait_line(
        {
            "run_id": "abc123",
            "status": "failed",
            "reason": "unknown",
            "artifact_dir": None,
            "exit_code": None,
        }
    )
    assert line == "abc123\tstatus=failed\treason=unknown\tartifact_dir=-\texit_code=-"


# ── Acceptance: play + session, correct reason + resolvable artifact_dir ────


@pytest.mark.asyncio
async def test_wait_for_terminal_on_session_yields_correct_reason_and_artifact_dir(
    temp_db_path: Path,
) -> None:
    async with StateDB() as db:
        sid = await _make_session(db, status="running", artifacts_path="/tmp/session-run")
        await db.update_status(
            "session",
            sid,
            new_status="completed",
            reason_code=RunReasons.COMPLETED_OK,
            source="executor",
        )

    outcomes = await wait_for_terminal([sid])
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome["run_id"] == sid
    assert outcome["status"] == "completed"
    assert outcome["reason"] != "unknown"
    # artifact_dir is the run directory (manifest container), not artifacts_path.
    assert outcome["artifact_dir"] == str(RUNS_ROOT / sid)


@pytest.mark.asyncio
async def test_wait_for_terminal_artifact_dir_ignores_artifacts_path(
    temp_db_path: Path,
) -> None:
    """artifact_dir must anchor on RUNS_ROOT / session_id even when the
    session's artifacts_path column points somewhere else entirely — the
    contract line always reports the run directory, never the artifacts
    subdir."""
    async with StateDB() as db:
        sid = await _make_session(
            db, status="completed", artifacts_path="/tmp/totally-unrelated-dir"
        )

    outcomes = await wait_for_terminal([sid])
    assert len(outcomes) == 1
    assert outcomes[0]["artifact_dir"] == str(RUNS_ROOT / sid)
    assert outcomes[0]["artifact_dir"] != "/tmp/totally-unrelated-dir"


@pytest.mark.asyncio
async def test_wait_for_terminal_on_play_yields_correct_reason_and_artifact_dir(
    temp_db_path: Path,
) -> None:
    async with StateDB() as db:
        sid = await _make_session(db, status="completed", artifacts_path="/tmp/play-session-run")
        show_id = await _make_show(db)
        play_id = await _make_play(db, show_id, status="merged", session_id=sid)

    outcomes = await wait_for_terminal([play_id])
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome["run_id"] == play_id
    assert outcome["status"] == "merged"
    # Cross-kind: play resolves to its primary session's run dir, not the
    # session's artifacts_path.
    assert outcome["artifact_dir"] == str(RUNS_ROOT / sid)


@pytest.mark.asyncio
async def test_wait_for_terminal_on_schedule_run_yields_backing_session_run_dir(
    temp_db_path: Path,
) -> None:
    """Cross-kind: schedule_run → invocation → primary session run dir."""
    async with StateDB() as db:
        sid = await _make_session(db, status="completed", artifacts_path="/tmp/sched-session-run")
        inv_id = await _make_invocation(db, status="completed")
        await db.execute("UPDATE sessions SET invocation_id = ? WHERE id = ?", (inv_id, sid))
        sched_id = await _make_schedule(db)
        run_id = await _make_schedule_run(db, sched_id, status="completed", invocation_id=inv_id)

    outcomes = await wait_for_terminal([run_id])
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome["run_id"] == run_id
    assert outcome["kind"] == "schedule_run"
    assert outcome["artifact_dir"] == str(RUNS_ROOT / sid)


@pytest.mark.asyncio
async def test_wait_for_terminal_on_timed_out_schedule_run_is_treated_as_terminal(
    temp_db_path: Path,
) -> None:
    """TERMINAL_STATUSES_BY_ENTITY_TYPE must be derived from the lifecycle
    policy registry, not a stale hand-maintained set — the registry's
    schedule_run terminal_statuses includes 'timed_out', and `li wait` must
    resolve it immediately rather than polling forever."""
    async with StateDB() as db:
        sched_id = await _make_schedule(db)
        run_id = await _make_schedule_run(db, sched_id, status="timed_out")

    outcomes = await wait_for_terminal([run_id])
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome["run_id"] == run_id
    assert outcome["kind"] == "schedule_run"
    assert outcome["status"] == "timed_out"


@pytest.mark.asyncio
async def test_wait_for_terminal_on_mixed_play_and_session_ids(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sid = await _make_session(db, status="completed", artifacts_path="/tmp/mixed-run")
        show_id = await _make_show(db)
        play_id = await _make_play(db, show_id, status="merged", session_id=sid)

    outcomes = await wait_for_terminal([play_id, sid])
    assert {o["run_id"] for o in outcomes} == {play_id, sid}
    assert {o["kind"] for o in outcomes} == {"play", "session"}


# ── Acceptance: completed-empty gets its own reason, not a bare status ──────


@pytest.mark.asyncio
async def test_completed_empty_session_yields_no_evidence_reason(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sid = await _make_session(db, status="running", artifacts_path="/tmp/empty-run")
        await db.update_status(
            "session",
            sid,
            new_status="completed_empty",
            reason_code=RunReasons.COMPLETED_EMPTY_NO_EVIDENCE,
            source="executor",
        )

    outcomes = await wait_for_terminal([sid])
    assert len(outcomes) == 1
    assert outcomes[0]["status"] == "completed_empty"
    assert outcomes[0]["reason"] == "run.completed_empty.no_evidence"


# ── run_wait: the CLI entry point ───────────────────────────────────────────


def test_run_wait_prints_contract_line_for_terminal_session(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    import asyncio

    async def _setup() -> str:
        async with StateDB() as db:
            return await _make_session(db, status="completed", artifacts_path="/tmp/cli-run")

    sid = asyncio.run(_setup())

    exit_code = run_wait([sid])
    out = capsys.readouterr().out
    assert exit_code == 0
    lines = [ln for ln in out.splitlines() if ln]
    assert len(lines) == 1
    assert lines[0].startswith(f"{sid}\tstatus=completed\t")
    assert "reason=" in lines[0]
    assert f"artifact_dir={RUNS_ROOT / sid}" in lines[0]
    assert "exit_code=" in lines[0]


def test_run_wait_unrecognized_reason_surfaces_unknown_sentinel_never_invents_a_code(
    temp_db_path: Path, capsys: pytest.CaptureFixture
) -> None:
    import asyncio

    async def _setup() -> str:
        async with StateDB() as db:
            sid = await _make_session(db, status="running", artifacts_path="/tmp/bogus-reason")
            # Bypass update_status to plant an unrecognized reason code directly,
            # simulating a legacy row VALID_REASON_CODES doesn't know about.
            await db.execute(
                "UPDATE sessions SET status = ?, status_reason_code = ? WHERE id = ?",
                ("completed", "totally.made.up.code", sid),
            )
            return sid

    sid = asyncio.run(_setup())

    exit_code = run_wait([sid])
    out = capsys.readouterr().out
    assert "reason=unknown" in out
    assert "totally.made.up.code" not in out
    assert exit_code == 0


def test_run_wait_unknown_id_returns_exit_unknown(temp_db_path: Path) -> None:
    assert run_wait(["nonexistent-id"]) == EXIT_UNKNOWN


def test_run_wait_requires_at_least_one_id(temp_db_path: Path) -> None:
    with pytest.raises(SystemExit):
        run_wait([])


def test_run_wait_ambiguous_schedule_run_prefix_reports_error_not_crash(
    temp_db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """`li wait <prefix>` where the prefix matches two schedule_runs must
    report a clean error and EXIT_UNKNOWN — the schedule-run resolver raises
    AmbiguousIdError, and this CLI entry point must catch it rather than crash
    with an uncaught traceback (it previously caught only signal interrupts)."""
    import asyncio
    import logging

    async def _setup() -> None:
        async with StateDB() as db:
            sched_id = await _make_schedule(db)
            rid1 = await _make_schedule_run(db, sched_id)
            rid2 = await _make_schedule_run(db, sched_id)
            # Force a shared short prefix that resolves to neither a session,
            # invocation, nor play — so resolution reaches _resolve_schedule_run
            # and its LIKE query matches both rows.
            await db.execute(
                "UPDATE schedule_runs SET id = ? WHERE id = ?", ("abc" + rid1[3:], rid1)
            )
            await db.execute(
                "UPDATE schedule_runs SET id = ? WHERE id = ?", ("abc" + rid2[3:], rid2)
            )

    asyncio.run(_setup())

    with caplog.at_level(logging.ERROR, logger="lionagi.cli.error"):
        exit_code = run_wait(["abc"])
    assert exit_code == EXIT_UNKNOWN
    assert "matches" in caplog.text.lower()
    assert "abc" in caplog.text.lower()
