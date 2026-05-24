# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li monitor` — real-time entity observation CLI."""

from __future__ import annotations

import signal
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from lionagi.cli.monitor import (
    _colour_status,
    _elapsed,
    _find_entity,
    _format_table,
    _gather_table_rows,
    _invocation_to_row,
    _pid_alive,
    _play_to_row,
    _run_detail,
    _run_table,
    _session_to_row,
    _show_to_row,
    _since_timestamp,
    _trunc,
)
from lionagi.state.db import StateDB

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Per-test temp DB; patch DEFAULT_DB_PATH so StateDB() opens it."""
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    monkeypatch.setattr("lionagi.cli.monitor._run_table", _run_table)  # identity; force DB path
    return db_path


async def _make_session(
    db: StateDB,
    *,
    status: str = "running",
    project: str | None = None,
    invocation_kind: str | None = "agent",
    model: str | None = "claude-3-5-sonnet",
    effort: str | None = "medium",
    provider: str | None = "anthropic",
    invocation_id: str | None = None,
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
            "effort": effort,
            "provider": provider,
            "started_at": time.time(),
            "invocation_id": invocation_id,
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
    db: StateDB, show_id: str, *, status: str = "running", name: str = "play-1"
) -> str:
    play_id = uuid.uuid4().hex[:12]
    await db.create_play(
        {
            "id": play_id,
            "show_id": show_id,
            "name": name,
            "status": status,
            "started_at": time.time(),
        }
    )
    return play_id


# ── Unit: formatting helpers ──────────────────────────────────────────────────


def test_elapsed_none_start():
    assert _elapsed(None) == "-"


def test_elapsed_seconds():
    start = time.time() - 45
    result = _elapsed(start)
    assert result.endswith("s")
    assert "45" in result or "44" in result  # allow 1s wall clock drift


def test_elapsed_minutes():
    start = time.time() - 90
    result = _elapsed(start)
    assert "m" in result


def test_elapsed_hours():
    start = time.time() - 7200
    result = _elapsed(start)
    assert "h" in result


def test_trunc_short():
    assert _trunc("hello", 10) == "hello"


def test_trunc_long():
    result = _trunc("hello world", 8)
    assert len(result) == 8
    assert result.endswith("…")


def test_since_timestamp_hours():
    cutoff = _since_timestamp("1h")
    assert abs(cutoff - (time.time() - 3600)) < 2


def test_since_timestamp_minutes():
    cutoff = _since_timestamp("30m")
    assert abs(cutoff - (time.time() - 1800)) < 2


def test_since_timestamp_days():
    cutoff = _since_timestamp("2d")
    assert abs(cutoff - (time.time() - 2 * 86400)) < 2


def test_since_timestamp_invalid():
    # "3x" passes int parsing but 'x' is not a known unit
    with pytest.raises(ValueError):
        _since_timestamp("3x")


def test_since_timestamp_bad_unit():
    with pytest.raises(ValueError, match="Unknown time unit"):
        _since_timestamp("5z")


def test_colour_status_running():
    result = _colour_status("running")
    # Should contain the word "running"
    assert "running" in result


def test_colour_status_unknown():
    result = _colour_status("some_unknown_status")
    # Unknown statuses are returned as-is
    assert result == "some_unknown_status"


def test_pid_alive_none():
    assert _pid_alive(None) is None


def test_pid_alive_own_process():
    import os

    assert _pid_alive(os.getpid()) is True


def test_pid_alive_nonexistent():
    # PID 0 is reserved on POSIX; sending a signal to it has special semantics.
    # Use a very high PID unlikely to exist instead.
    result = _pid_alive(9_999_999)
    assert result is False or result is None  # platform-dependent


# ── Unit: table formatting ────────────────────────────────────────────────────


def test_format_table_empty():
    output = _format_table([])
    assert "no running" in output.lower() or output.strip() == ""


def test_format_table_one_row():
    rows = [
        {
            "id": "abc123",
            "type": "session",
            "project": "myproject",
            "status": "running",
            "phase": "agent",
            "elapsed": "5m30s",
            "agents": "1",
        }
    ]
    output = _format_table(rows)
    assert "abc123" in output
    assert "session" in output
    assert "myproject" in output
    assert "running" in output


def test_format_table_header():
    rows = [
        {
            "id": "x",
            "type": "y",
            "project": "z",
            "status": "running",
            "phase": "-",
            "elapsed": "-",
            "agents": "-",
        }
    ]
    output = _format_table(rows)
    assert "ID" in output
    assert "TYPE" in output
    assert "STATUS" in output
    assert "ELAPSED" in output


# ── Unit: row builders ────────────────────────────────────────────────────────


def test_session_to_row():
    sess = {
        "id": "abc123def456",
        "invocation_kind": "agent",
        "project": "lionagi",
        "status": "running",
        "agent_name": "coder",
        "model": "claude-3-5-sonnet",
        "started_at": time.time() - 100,
    }
    row = _session_to_row(sess)
    assert row["type"] == "agent"
    assert row["project"] == "lionagi"
    assert row["status"] == "running"
    assert row["phase"] == "coder"


def test_session_to_row_no_optional():
    sess = {
        "id": "abc123def456",
        "status": "running",
    }
    row = _session_to_row(sess)
    assert row["project"] == "-"
    assert row["phase"] == "-"


def test_invocation_to_row():
    inv = {
        "id": "inv001abc",
        "status": "running",
        "skill": "show",
        "session_count": 3,
        "started_at": time.time() - 300,
    }
    row = _invocation_to_row(inv)
    assert row["type"] == "invocation"
    assert row["agents"] == "3"
    assert row["phase"] == "show"


def test_show_to_row():
    show = {
        "id": "show001abc",
        "status": "active",
        "topic": "my-feature",
        "repo": "octocat/hello",
    }
    row = _show_to_row(show)
    assert row["type"] == "show"
    assert row["project"] == "octocat/hello"
    assert "my-feature" in row["phase"]


def test_play_to_row():
    play = {
        "id": "play001abc",
        "status": "running",
        "name": "backend-impl",
        "started_at": time.time() - 60,
    }
    row = _play_to_row(play)
    assert row["type"] == "play"
    assert row["phase"] == "backend-impl"


# ── Integration: DB-backed list_running ───────────────────────────────────────


@pytest.mark.asyncio
async def test_gather_table_rows_empty(temp_db_path: Path) -> None:
    async with StateDB() as db:
        rows = await _gather_table_rows(db, since=None, entity_type=None, project=None)
    assert rows == []


@pytest.mark.asyncio
async def test_gather_table_rows_sessions(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sid1 = await _make_session(db, status="running", project="proj-a")
        sid2 = await _make_session(db, status="completed", project="proj-a")  # should be excluded
        rows = await _gather_table_rows(db, since=None, entity_type=None, project=None)

    session_ids = [r["id"] for r in rows]
    assert sid1[:16] in session_ids
    # completed session must NOT appear
    assert not any(sid2[:16] in rid for rid in session_ids)


@pytest.mark.asyncio
async def test_gather_table_rows_project_filter(temp_db_path: Path) -> None:
    async with StateDB() as db:
        s_a = await _make_session(db, project="proj-a")
        s_b = await _make_session(db, project="proj-b")
        rows_a = await _gather_table_rows(db, since=None, entity_type=None, project="proj-a")
        rows_b = await _gather_table_rows(db, since=None, entity_type=None, project="proj-b")

    ids_a = [r["id"] for r in rows_a]
    ids_b = [r["id"] for r in rows_b]
    assert s_a[:16] in ids_a
    assert s_b[:16] not in ids_a
    assert s_b[:16] in ids_b
    assert s_a[:16] not in ids_b


@pytest.mark.asyncio
async def test_gather_table_rows_type_filter_session(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sid = await _make_session(db)
        inv_id = await _make_invocation(db)
        # Only sessions
        rows = await _gather_table_rows(db, since=None, entity_type="session", project=None)

    assert any(sid[:16] in r["id"] for r in rows)
    assert not any(r["type"] == "invocation" for r in rows)


@pytest.mark.asyncio
async def test_gather_table_rows_invocations(temp_db_path: Path) -> None:
    async with StateDB() as db:
        inv_id = await _make_invocation(db, skill="show")
        rows = await _gather_table_rows(db, since=None, entity_type="invocation", project=None)

    assert any(inv_id[:16] in r["id"] for r in rows)
    assert all(r["type"] == "invocation" for r in rows)


@pytest.mark.asyncio
async def test_gather_table_rows_shows(temp_db_path: Path) -> None:
    async with StateDB() as db:
        show_id = await _make_show(db, topic="my-topic")
        rows = await _gather_table_rows(db, since=None, entity_type="show", project=None)

    assert any(show_id[:16] in r["id"] for r in rows)
    assert all(r["type"] == "show" for r in rows)


@pytest.mark.asyncio
async def test_gather_table_rows_plays(temp_db_path: Path) -> None:
    async with StateDB() as db:
        show_id = await _make_show(db)
        play_id = await _make_play(db, show_id, status="running")
        rows = await _gather_table_rows(db, since=None, entity_type="play", project=None)

    assert any(play_id[:16] in r["id"] for r in rows)
    assert all(r["type"] == "play" for r in rows)


@pytest.mark.asyncio
async def test_gather_table_rows_since_filter(temp_db_path: Path) -> None:
    """Sessions with updated_at before the cutoff should be excluded."""
    async with StateDB() as db:
        sid_old = await _make_session(db)
        # Force updated_at to be in the past
        cutoff_past = time.time() - 3600
        await db.db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (cutoff_past - 10, sid_old),
        )
        await db.db.commit()

        sid_new = await _make_session(db)
        since = time.time() - 60  # last minute only
        rows = await _gather_table_rows(db, since=since, entity_type="session", project=None)

    ids = [r["id"] for r in rows]
    assert sid_new[:16] in ids
    assert not any(sid_old[:16] in i for i in ids)


# ── Integration: _find_entity ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_entity_session(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sid = await _make_session(db)
        result = await _find_entity(db, sid)

    assert result is not None
    entity_type, row = result
    assert entity_type == "session"
    assert row["id"] == sid


@pytest.mark.asyncio
async def test_find_entity_invocation(temp_db_path: Path) -> None:
    async with StateDB() as db:
        inv_id = await _make_invocation(db)
        result = await _find_entity(db, inv_id)

    assert result is not None
    entity_type, row = result
    assert entity_type == "invocation"


@pytest.mark.asyncio
async def test_find_entity_show(temp_db_path: Path) -> None:
    async with StateDB() as db:
        show_id = await _make_show(db)
        result = await _find_entity(db, show_id)

    assert result is not None
    entity_type, row = result
    assert entity_type == "show"


@pytest.mark.asyncio
async def test_find_entity_play(temp_db_path: Path) -> None:
    async with StateDB() as db:
        show_id = await _make_show(db)
        play_id = await _make_play(db, show_id)
        result = await _find_entity(db, play_id)

    assert result is not None
    entity_type, row = result
    assert entity_type == "play"


@pytest.mark.asyncio
async def test_find_entity_prefix_match(temp_db_path: Path) -> None:
    async with StateDB() as db:
        inv_id = await _make_invocation(db)
        # Search with first 4 chars
        result = await _find_entity(db, inv_id[:4])

    assert result is not None
    assert result[0] == "invocation"


@pytest.mark.asyncio
async def test_find_entity_not_found(temp_db_path: Path) -> None:
    async with StateDB() as db:
        result = await _find_entity(db, "nonexistentid999")
    assert result is None


# ── Integration: _run_table and _run_detail ───────────────────────────────────


@pytest.mark.asyncio
async def test_run_table_no_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "nonexistent.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", missing)
    output = await _run_table(since=None, entity_type=None, project=None)
    # Should return a graceful "no state.db" message
    assert "state.db" in output or "no" in output.lower()


@pytest.mark.asyncio
async def test_run_table_with_running_session(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sid = await _make_session(db, project="test-project")
    output = await _run_table(since=None, entity_type=None, project=None)
    assert sid[:12] in output or "test-project" in output or "running" in output


@pytest.mark.asyncio
async def test_run_detail_session(temp_db_path: Path) -> None:
    async with StateDB() as db:
        sid = await _make_session(db, model="claude-opus-4", project="demo")
    output = await _run_detail(sid)
    assert "SESSION" in output
    assert "running" in output.lower()


@pytest.mark.asyncio
async def test_run_detail_invocation(temp_db_path: Path) -> None:
    async with StateDB() as db:
        inv_id = await _make_invocation(db, skill="codex-review")
    output = await _run_detail(inv_id)
    assert "INVOCATION" in output
    assert "codex-review" in output


@pytest.mark.asyncio
async def test_run_detail_show(temp_db_path: Path) -> None:
    async with StateDB() as db:
        show_id = await _make_show(db, topic="implement-auth")
    output = await _run_detail(show_id)
    assert "SHOW" in output
    assert "implement-auth" in output


@pytest.mark.asyncio
async def test_run_detail_play(temp_db_path: Path) -> None:
    async with StateDB() as db:
        show_id = await _make_show(db)
        play_id = await _make_play(db, show_id, name="backend-work")
    output = await _run_detail(play_id)
    assert "PLAY" in output
    assert "backend-work" in output


@pytest.mark.asyncio
async def test_run_detail_not_found(temp_db_path: Path) -> None:
    output = await _run_detail("no-such-id-xyz-999")
    assert "not found" in output.lower() or "error" in output.lower()


@pytest.mark.asyncio
async def test_run_detail_no_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "gone.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", missing)
    output = await _run_detail("some-id")
    assert "state.db" in output or "not found" in output.lower() or "error" in output.lower()


# ── Integration: argparse wiring ─────────────────────────────────────────────


def test_add_monitor_subparser():
    """Verify that `li monitor` is registered and accepts expected arguments."""
    import argparse

    from lionagi.cli.monitor import add_monitor_subparser

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    add_monitor_subparser(sub)

    # Table view
    args = parser.parse_args(["monitor"])
    assert args.id is None
    assert not args.watch

    # Detail view
    args = parser.parse_args(["monitor", "abc123"])
    assert args.id == "abc123"

    # Watch mode
    args = parser.parse_args(["monitor", "--watch"])
    assert args.watch

    # --since
    args = parser.parse_args(["monitor", "--since", "1h"])
    assert args.since == "1h"

    # --type
    args = parser.parse_args(["monitor", "--type", "session"])
    assert args.entity_type == "session"

    # --project
    args = parser.parse_args(["monitor", "--project", "myproject"])
    assert args.project == "myproject"

    # mon alias
    args = parser.parse_args(["mon", "--watch", "eid"])
    assert args.id == "eid"
    assert args.watch


def test_main_registers_monitor():
    """End-to-end: `li monitor --help` exits 0."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "lionagi.cli", "monitor", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "monitor" in result.stdout.lower() or "observe" in result.stdout.lower()


# ── Watch mode: SIGINT terminates cleanly ─────────────────────────────────────


def test_watch_mode_sigint_clean(temp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Watch loop exits cleanly when SIGINT is received."""
    import os
    import threading

    from lionagi.cli.monitor import _watch_loop

    # Trigger SIGINT after a short delay from a background thread
    def _send_interrupt():
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGINT)

    t = threading.Thread(target=_send_interrupt, daemon=True)
    t.start()

    exit_code = _watch_loop(
        1,
        None,
        since=None,
        entity_type=None,
        project=None,
    )
    t.join(timeout=2)
    # Watch loop must return 0 (not raise or hang)
    assert exit_code == 0
