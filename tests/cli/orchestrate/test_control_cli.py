# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for `li o ctl pause|resume|msg` (ADR-0085 part 1: session_controls transport).

Covers enqueue row shapes (verb, payload) and id resolution (session,
invocation, play, short prefix, unknown id) through the same generic
resolver `li o ctl status` uses.
"""

from __future__ import annotations

import argparse
import time
import uuid
from pathlib import Path

import pytest

from lionagi.cli.orchestrate._control import (
    run_ctl_msg,
    run_ctl_pause,
    run_ctl_resume,
)
from lionagi.cli.status import EXIT_UNKNOWN
from lionagi.state.db import StateDB


@pytest.fixture
def temp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("lionagi.state.db.DEFAULT_DB_PATH", db_path)
    return db_path


async def _make_session(db: StateDB, *, invocation_kind: str = "flow") -> str:
    sid = uuid.uuid4().hex[:12]
    pid = uuid.uuid4().hex
    await db.create_progression(pid)
    await db.create_session(
        {
            "id": sid,
            "progression_id": pid,
            "status": "running",
            "invocation_kind": invocation_kind,
            "started_at": time.time(),
        }
    )
    return sid


async def _make_invocation(db: StateDB, *, status: str = "running") -> str:
    inv_id = uuid.uuid4().hex[:12]
    await db.create_invocation(
        {"id": inv_id, "skill": "flow", "started_at": time.time(), "status": status}
    )
    return inv_id


# ── pause ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pause_enqueues_row_for_session_id(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(db)

    rc = run_ctl_pause(argparse.Namespace(id=sid))
    assert rc == 0

    async with StateDB() as db:
        pending = await db.list_pending_session_controls(sid)
    assert len(pending) == 1
    assert pending[0]["verb"] == "pause"
    assert pending[0]["payload"] is None


@pytest.mark.asyncio
async def test_pause_resolves_short_prefix(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(db)

    rc = run_ctl_pause(argparse.Namespace(id=sid[:6]))
    assert rc == 0

    async with StateDB() as db:
        pending = await db.list_pending_session_controls(sid)
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_pause_resolves_via_invocation_id(temp_db_path: Path):
    async with StateDB() as db:
        inv_id = await _make_invocation(db)
        sid = uuid.uuid4().hex[:12]
        pid = uuid.uuid4().hex
        await db.create_progression(pid)
        await db.create_session(
            {
                "id": sid,
                "progression_id": pid,
                "status": "running",
                "invocation_kind": "flow",
                "invocation_id": inv_id,
                "started_at": time.time(),
            }
        )

    rc = run_ctl_pause(argparse.Namespace(id=inv_id))
    assert rc == 0

    async with StateDB() as db:
        pending = await db.list_pending_session_controls(sid)
    assert len(pending) == 1
    assert pending[0]["verb"] == "pause"


def test_pause_unknown_id_returns_exit_unknown(temp_db_path: Path):
    rc = run_ctl_pause(argparse.Namespace(id="x" * 36))
    assert rc == EXIT_UNKNOWN


def test_pause_no_state_db_returns_exit_unknown(temp_db_path: Path):
    # temp_db_path is a fresh, never-created path — no state.db on disk yet.
    rc = run_ctl_pause(argparse.Namespace(id="anything"))
    assert rc == EXIT_UNKNOWN


# ── resume ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_enqueues_row(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(db)

    rc = run_ctl_resume(argparse.Namespace(id=sid))
    assert rc == 0

    async with StateDB() as db:
        pending = await db.list_pending_session_controls(sid)
    assert len(pending) == 1
    assert pending[0]["verb"] == "resume"
    assert pending[0]["payload"] is None


# ── msg ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_msg_enqueues_row_with_text_payload(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(db)

    rc = run_ctl_msg(argparse.Namespace(id=sid, text="please pause after this op"))
    assert rc == 0

    async with StateDB() as db:
        pending = await db.list_pending_session_controls(sid)
    assert len(pending) == 1
    assert pending[0]["verb"] == "message"
    assert pending[0]["payload"] == {"text": "please pause after this op"}


def test_msg_unknown_id_returns_exit_unknown(temp_db_path: Path):
    rc = run_ctl_msg(argparse.Namespace(id="x" * 36, text="hi"))
    assert rc == EXIT_UNKNOWN


# ── multiple controls queue independently ──────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_controls_queue_in_order(temp_db_path: Path):
    async with StateDB() as db:
        sid = await _make_session(db)

    assert run_ctl_pause(argparse.Namespace(id=sid)) == 0
    assert run_ctl_msg(argparse.Namespace(id=sid, text="hold on")) == 0
    assert run_ctl_resume(argparse.Namespace(id=sid)) == 0

    async with StateDB() as db:
        pending = await db.list_pending_session_controls(sid)
    assert [p["verb"] for p in pending] == ["pause", "message", "resume"]
