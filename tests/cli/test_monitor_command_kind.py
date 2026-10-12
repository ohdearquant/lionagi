# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li monitor` has to say what an invocation actually launched.

A command-kind schedule fire read as UNRECORDED. The row was always
written -- ``_fire_inner`` creates it before any kind branch -- but it carried
no kind and the table hardcoded the label ``invocation``, so a command action
was indistinguishable from every other fire, and the exit code and stderr tail
the runtime already persists were unreadable from the CLI. These tests pin the
reader half: the kind is projected the way a session's already is, ``--type``
can select it, and a failed fire shows its rc and its tail.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

import pytest

from lionagi.cli.monitor import _detail_invocation, _invocation_to_row, _run_table
from lionagi.state.db import StateDB
from lionagi.state.reasons import RunReasons

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


async def _make_invocation(
    db: StateDB,
    *,
    skill: str,
    status: str = "running",
    action_kind: str | None = None,
) -> str:
    inv_id = uuid.uuid4().hex[:12]
    await db.create_invocation(
        {
            "id": inv_id,
            "skill": skill,
            "started_at": time.time(),
            "status": status,
            "node_metadata": ({"action_kind": action_kind} if action_kind else None),
        }
    )
    return inv_id


async def _make_schedule_run(
    db: StateDB,
    *,
    invocation_id: str,
    exit_code: int | None,
    error_detail: str | None,
    status: str,
) -> str:
    sched_id = uuid.uuid4().hex[:12]
    await db.create_schedule(
        {
            "id": sched_id,
            "name": f"probe-{sched_id}",
            "trigger_type": "cron",
            "cron_expr": "0 2 * * *",
            "action_kind": "command",
            "next_fire_at": time.time() + 3600,
        }
    )
    run_id = uuid.uuid4().hex[:12]
    await db.create_schedule_run(
        {
            "id": run_id,
            "schedule_id": sched_id,
            "invocation_id": invocation_id,
            "trigger_context": {},
            "action_kind": "command",
            "action_args": [],
            "status": status,
            "exit_code": exit_code,
            "error_detail": error_detail,
            "fired_at": time.time(),
            "ended_at": time.time(),
        }
    )
    return run_id


async def test_invocation_row_shows_its_action_kind_and_still_defaults(
    temp_db_path: Path,
) -> None:
    """The kind when one was recorded; the generic label when none was.

    The default arm is the scope control: rows written before the kind was
    persisted, and every path that records no kind, must keep reading exactly
    as they did.
    """
    async with StateDB() as db:
        with_kind = await _make_invocation(db, skill="scheduled:probe", action_kind="command")
        without_kind = await _make_invocation(db, skill="show")

        rows = await db.fetch_all("SELECT * FROM invocations ORDER BY created_at", ())
        by_id = {r["id"]: dict(r) for r in rows}

    assert _invocation_to_row(by_id[with_kind])["type"] == "command"
    assert _invocation_to_row(by_id[without_kind])["type"] == "invocation"


async def test_type_command_selects_only_command_kind_invocations(
    temp_db_path: Path,
) -> None:
    """`--type command` narrows; `--type invocation` still returns both.

    The second half is the must-match control: a filter that returned nothing
    because the query broke would satisfy the first assertion alone.
    """
    async with StateDB() as db:
        command_id = await _make_invocation(
            db, skill="scheduled:command-probe", action_kind="command"
        )
        agent_id = await _make_invocation(db, skill="scheduled:agent-probe", action_kind="agent")

    narrowed = await _run_table(since=None, entity_type="command", project=None)
    assert command_id[:16] in narrowed
    assert agent_id[:16] not in narrowed

    unnarrowed = await _run_table(since=None, entity_type="invocation", project=None)
    assert command_id[:16] in unnarrowed
    assert agent_id[:16] in unnarrowed


async def test_failed_command_fire_detail_carries_rc_and_the_stderr_tail(
    temp_db_path: Path,
) -> None:
    """Fired-and-died has to be readable as such, with the reason.

    A command action spawns no `li`, so it has no child sessions, so
    ``resolve_invocation_terminal`` writes the invocation a reason naming the
    exit code while the tail stays on the schedule_run. The detail view reads
    both, which is what makes the two states distinguishable at the CLI.
    """
    async with StateDB() as db:
        inv_id = await _make_invocation(
            db, skill="scheduled:cloud/probe", status="running", action_kind="command"
        )
        await db.update_status(
            "invocation",
            inv_id,
            new_status="failed",
            reason_code=RunReasons.FAILED_EXIT_NONZERO,
            reason_summary="Invocation process failed with exit code 2.",
            evidence_refs=[],
            source="executor",
            actor=inv_id,
            metadata={},
            extra_fields={"ended_at": time.time()},
        )
        await _make_schedule_run(
            db,
            invocation_id=inv_id,
            exit_code=2,
            error_detail="Traceback (most recent call last):\nFileNotFoundError: probe.py",
            status="failed",
        )
        rows = await db.fetch_all("SELECT * FROM invocations WHERE id = ?", (inv_id,))
        inv = dict(rows[0])
        detail = await _detail_invocation(db, inv)

    assert "kind:          command" in detail
    assert "exit code 2" in detail
    assert "exit_code:  2" in detail
    assert "FileNotFoundError: probe.py" in detail


async def test_successful_command_fire_detail_shows_rc_zero_and_no_tail(
    temp_db_path: Path,
) -> None:
    """The known-good arm: the same surface on a run that worked.

    Without it a detail view that printed a failure section unconditionally,
    or printed nothing at all, would pass the failing test above.
    """
    async with StateDB() as db:
        inv_id = await _make_invocation(
            db, skill="scheduled:cloud/probe", status="running", action_kind="command"
        )
        await db.update_status(
            "invocation",
            inv_id,
            new_status="completed",
            reason_code=RunReasons.COMPLETED_OK,
            reason_summary="Invocation process completed successfully.",
            evidence_refs=[],
            source="executor",
            actor=inv_id,
            metadata={},
            extra_fields={"ended_at": time.time()},
        )
        await _make_schedule_run(
            db,
            invocation_id=inv_id,
            exit_code=0,
            error_detail=None,
            status="completed",
        )
        rows = await db.fetch_all("SELECT * FROM invocations WHERE id = ?", (inv_id,))
        inv = dict(rows[0])
        detail = await _detail_invocation(db, inv)

    assert "kind:          command" in detail
    assert "exit_code:  0" in detail
    assert "stderr tail" not in detail
