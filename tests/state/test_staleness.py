# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0057 D6 staleness detection tests.

Covers: kind-aware thresholds, terminal sessions are non-stale,
last_message_at preferred over updated_at, touch_session_activity()
heartbeat.
"""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import pytest

from lionagi.state.db import StateDB
from lionagi.state.staleness import (
    DEFAULT_STALE_THRESHOLD,
    STALE_THRESHOLDS,
    staleness_check,
    threshold_for_kind,
)


@pytest.fixture
async def db():
    state = StateDB(":memory:")
    await state.open()
    yield state
    await state.close()


def _uid() -> str:
    return str(uuid.uuid4())


async def _make_session(db: StateDB, **fields) -> dict:
    prog_id = _uid()
    await db.create_progression(prog_id)
    session = {"id": _uid(), "progression_id": prog_id, **fields}
    await db.create_session(session)
    return session


# ── staleness_check pure logic ─────────────────────────────────────────────────


def test_running_under_threshold_is_active():
    now = 1_000_000.0
    session = {
        "status": "running",
        "invocation_kind": "agent",
        "last_message_at": now - 1_000,  # ~17 minutes ago, well under 6h
    }
    assert staleness_check(session, now=now) is None


def test_running_over_agent_threshold_is_stale():
    now = 1_000_000.0
    session = {
        "status": "running",
        "invocation_kind": "agent",
        "last_message_at": now - (6 * 3600 + 60),  # 6h 1m
    }
    assert staleness_check(session, now=now) == "stale"


def test_flow_threshold_is_more_lenient_than_agent():
    """A 9h-quiet single agent is stale; a 9h-quiet flow is still active."""
    now = 1_000_000.0
    nine_hours_ago = now - (9 * 3600)
    agent = {
        "status": "running",
        "invocation_kind": "agent",
        "last_message_at": nine_hours_ago,
    }
    flow = {
        "status": "running",
        "invocation_kind": "flow",
        "last_message_at": nine_hours_ago,
    }
    assert staleness_check(agent, now=now) == "stale"
    assert staleness_check(flow, now=now) is None


def test_terminal_status_is_not_stale():
    """Terminal sessions are never stale; classification defers to ADR-0057 health."""
    now = 1_000_000.0
    for status in ("completed", "failed", "timed_out", "aborted", "cancelled"):
        s = {
            "status": status,
            "invocation_kind": "agent",
            "last_message_at": now - (100 * 3600),
        }
        assert staleness_check(s, now=now) is None, f"{status!r} should not be stale"


def test_unknown_invocation_kind_falls_back_to_default():
    now = 1_000_000.0
    session = {
        "status": "running",
        "invocation_kind": "mystery-kind",
        "last_message_at": now - (DEFAULT_STALE_THRESHOLD + 60),
    }
    assert staleness_check(session, now=now) == "stale"


def test_missing_last_message_at_falls_back_to_updated_at():
    now = 1_000_000.0
    session = {
        "status": "running",
        "invocation_kind": "agent",
        "last_message_at": None,
        "updated_at": now - (6 * 3600 + 60),
    }
    assert staleness_check(session, now=now) == "stale"


def test_session_with_no_activity_columns_is_stale():
    """Legacy rows with neither last_message_at nor updated_at — definitely dead."""
    now = 1_000_000.0
    session = {"status": "running", "invocation_kind": "agent"}
    assert staleness_check(session, now=now) == "stale"


def test_threshold_for_kind_returns_expected_values():
    assert threshold_for_kind("agent") == 6 * 3600
    assert threshold_for_kind("flow") == 12 * 3600
    assert threshold_for_kind(None) == DEFAULT_STALE_THRESHOLD
    assert threshold_for_kind("mystery-kind") == DEFAULT_STALE_THRESHOLD


# ── touch_session_activity DB heartbeat ────────────────────────────────────────


async def test_touch_session_activity_bumps_last_message_at(
    db: StateDB, monkeypatch: pytest.MonkeyPatch
):
    import lionagi.state.db as state_db_mod

    fixed_now = 1_000_000.0
    monkeypatch.setattr(state_db_mod, "time", SimpleNamespace(time=lambda: fixed_now))
    s = await _make_session(db, status="running", invocation_kind="agent")
    await db.touch_session_activity(s["id"])
    row = await db.get_session(s["id"])
    assert row["last_message_at"] == fixed_now


async def test_touch_session_activity_with_explicit_at(db: StateDB):
    s = await _make_session(db, status="running", invocation_kind="agent")
    # Monotonic: use a future timestamp so the MAX guard doesn't reject it.
    pinned = time.time() + 1_000
    await db.touch_session_activity(s["id"], at=pinned)
    row = await db.get_session(s["id"])
    assert row["last_message_at"] == pinned


async def test_touch_session_activity_monotonic(db: StateDB):
    """Past timestamps must not regress last_message_at."""
    s = await _make_session(db, status="running", invocation_kind="agent")
    row = await db.get_session(s["id"])
    original = row["last_message_at"]
    # A timestamp in the past must not overwrite the current value.
    await db.touch_session_activity(s["id"], at=original - 10_000)
    row = await db.get_session(s["id"])
    assert row["last_message_at"] == original


async def test_touch_updates_updated_at_too(db: StateDB):
    """updated_at and last_message_at move together so list ordering stays
    consistent with activity, not just lifecycle writes."""
    s = await _make_session(db, status="running", invocation_kind="agent")
    # Monotonic: use a future timestamp so the MAX guard doesn't reject it.
    pinned = time.time() + 1_000
    await db.touch_session_activity(s["id"], at=pinned)
    row = await db.get_session(s["id"])
    assert row["updated_at"] == pinned


# ── ADR scope check: thresholds dict shape ────────────────────────────────────


def test_thresholds_cover_all_invocation_kinds():
    """Every invocation_kind must have an explicit staleness threshold.

    A missing kind silently falls back to DEFAULT_STALE_THRESHOLD, which
    is correct *behavior* but indicates the ADR was updated without
    updating thresholds. Catch the drift here.
    """
    from lionagi.state.db import _INVOCATION_KINDS

    missing = _INVOCATION_KINDS - STALE_THRESHOLDS.keys()
    assert not missing, f"invocation_kinds without explicit threshold: {missing}"
