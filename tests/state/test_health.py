# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""ADR-0024 session health classifier tests — pure-function; caller passes process/artifact/lock signals."""

from __future__ import annotations

import pytest

from lionagi.state.health import (
    HEALTH_SEVERITY,
    IDLE_THRESHOLD,
    SessionHealth,
    classify_session_health,
    worst_health,
)

NOW = 1_000_000.0


# ── Terminal sessions ────────────────────────────────────────────────────────


def test_completed_session_with_clean_resources_is_healthy():
    s = {"status": "completed"}
    assert (
        classify_session_health(
            s,
            now=NOW,
            process_alive=False,
            has_artifacts=True,
            has_stale_locks=False,
        )
        == SessionHealth.HEALTHY
    )


def test_terminal_session_with_stale_locks_is_zombie():
    """Stale locks left after a terminal session = leaked resources."""
    for status in ("completed", "failed", "timed_out", "aborted", "cancelled"):
        s = {"status": status}
        h = classify_session_health(
            s,
            now=NOW,
            process_alive=False,
            has_artifacts=True,
            has_stale_locks=True,
        )
        assert h == SessionHealth.ZOMBIE, f"{status!r} with locks should be zombie"


def test_terminal_session_artifacts_alone_not_zombie():
    """Artifacts are a *good* outcome — not the zombie signal."""
    s = {"status": "completed"}
    h = classify_session_health(
        s,
        now=NOW,
        process_alive=False,
        has_artifacts=True,
        has_stale_locks=False,
    )
    assert h == SessionHealth.HEALTHY


# ── Running, process alive ────────────────────────────────────────────────────


def test_running_with_recent_messages_is_healthy():
    s = {
        "status": "running",
        "invocation_kind": "agent",
        "last_message_at": NOW - 60,
    }
    h = classify_session_health(
        s,
        now=NOW,
        process_alive=True,
        has_artifacts=True,
        has_stale_locks=False,
    )
    assert h == SessionHealth.HEALTHY


def test_running_quiet_under_2h_is_idle_not_unresponsive():
    """Quiet for >1h but under the kind-aware threshold = idle."""
    s = {
        "status": "running",
        "invocation_kind": "agent",
        "last_message_at": NOW - (IDLE_THRESHOLD + 60),  # ~1h 1m
    }
    h = classify_session_health(
        s,
        now=NOW,
        process_alive=True,
        has_artifacts=True,
        has_stale_locks=False,
    )
    assert h == SessionHealth.IDLE


def test_running_past_threshold_alive_is_unresponsive():
    """Process alive but past kind-aware threshold (6h for agent)."""
    s = {
        "status": "running",
        "invocation_kind": "agent",
        "last_message_at": NOW - (6 * 3600 + 60),
    }
    h = classify_session_health(
        s,
        now=NOW,
        process_alive=True,
        has_artifacts=True,
        has_stale_locks=False,
    )
    assert h == SessionHealth.UNRESPONSIVE


def test_flow_threshold_more_lenient_than_agent():
    """Same 9h gap → unresponsive for agent, idle for flow."""
    nine_hours_ago = NOW - (9 * 3600)
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
    assert (
        classify_session_health(
            agent,
            now=NOW,
            process_alive=True,
            has_artifacts=True,
            has_stale_locks=False,
        )
        == SessionHealth.UNRESPONSIVE
    )
    assert (
        classify_session_health(
            flow,
            now=NOW,
            process_alive=True,
            has_artifacts=True,
            has_stale_locks=False,
        )
        == SessionHealth.IDLE
    )


# ── Running, process dead ────────────────────────────────────────────────────


def test_running_process_dead_but_recently_messaging_is_healthy():
    """Externally-driven sessions expose no matchable pid; recent messages
    outrank process visibility as life-evidence."""
    s = {
        "status": "running",
        "invocation_kind": "agent",
        "last_message_at": NOW - 200,
        "message_count": 5,
    }
    h = classify_session_health(
        s,
        now=NOW,
        process_alive=False,
        has_artifacts=True,
        has_stale_locks=False,
    )
    assert h == SessionHealth.HEALTHY


def test_running_process_dead_quiet_hours_is_idle():
    s = {
        "status": "running",
        "invocation_kind": "agent",
        "last_message_at": NOW - 2 * 3600,
        "message_count": 5,
    }
    h = classify_session_health(
        s,
        now=NOW,
        process_alive=False,
        has_artifacts=True,
        has_stale_locks=False,
    )
    assert h == SessionHealth.IDLE


def test_running_process_dead_exactly_at_threshold_is_idle():
    """Equality with the kind threshold stays on the alive side of the
    boundary — the same contract as the alive branch's UNRESPONSIVE cut."""
    s = {
        "status": "running",
        "invocation_kind": "agent",
        "last_message_at": NOW - 6 * 3600,
        "message_count": 5,
    }
    h = classify_session_health(
        s,
        now=NOW,
        process_alive=False,
        has_artifacts=True,
        has_stale_locks=False,
    )
    assert h == SessionHealth.IDLE


def test_running_process_alive_exactly_at_threshold_is_idle():
    """Alive/dead branches share the equality boundary."""
    s = {
        "status": "running",
        "invocation_kind": "agent",
        "last_message_at": NOW - 6 * 3600,
        "message_count": 5,
    }
    h = classify_session_health(
        s,
        now=NOW,
        process_alive=True,
        has_artifacts=True,
        has_stale_locks=False,
    )
    assert h == SessionHealth.IDLE


def test_running_process_dead_past_threshold_is_stale():
    s = {
        "status": "running",
        "invocation_kind": "agent",
        "last_message_at": NOW - 7 * 3600,
        "message_count": 5,
    }
    h = classify_session_health(
        s,
        now=NOW,
        process_alive=False,
        has_artifacts=True,
        has_stale_locks=False,
    )
    assert h == SessionHealth.STALE


def test_running_process_dead_no_output_is_orphaned():
    """Never wrote a message, never produced artifacts, process gone."""
    s = {
        "status": "running",
        "invocation_kind": "agent",
        "last_message_at": None,
        "message_count": 0,
    }
    h = classify_session_health(
        s,
        now=NOW,
        process_alive=False,
        has_artifacts=False,
        has_stale_locks=False,
    )
    assert h == SessionHealth.ORPHANED


def test_running_process_dead_no_messages_but_has_artifacts_not_orphaned():
    """Artifacts present means the session produced *something* — not orphaned.
    Recent activity still classifies it alive despite the missing process."""
    s = {
        "status": "running",
        "invocation_kind": "agent",
        "last_message_at": NOW - 200,
        "message_count": 0,
    }
    h = classify_session_health(
        s,
        now=NOW,
        process_alive=False,
        has_artifacts=True,
        has_stale_locks=False,
    )
    assert h == SessionHealth.HEALTHY


# ── Edge cases ────────────────────────────────────────────────────────────────


def test_null_status_treated_as_completed():
    """ADR-0017 legacy rows with NULL status fall through the terminal branch."""
    s = {"status": None}
    h = classify_session_health(
        s,
        now=NOW,
        process_alive=False,
        has_artifacts=False,
        has_stale_locks=False,
    )
    assert h == SessionHealth.HEALTHY


def test_missing_invocation_kind_uses_default_threshold():
    """Unknown kinds fall back to DEFAULT_STALE_THRESHOLD (6h)."""
    s = {
        "status": "running",
        "invocation_kind": "mystery-kind",
        "last_message_at": NOW - (6 * 3600 + 60),
    }
    h = classify_session_health(
        s,
        now=NOW,
        process_alive=True,
        has_artifacts=True,
        has_stale_locks=False,
    )
    assert h == SessionHealth.UNRESPONSIVE


# ── worst_health aggregator (grouped runs view) ──────────────────────────────


def test_worst_health_picks_highest_severity():
    inputs = [
        SessionHealth.HEALTHY,
        SessionHealth.STALE,
        SessionHealth.IDLE,
    ]
    assert worst_health(inputs) == SessionHealth.STALE


def test_worst_health_empty_is_healthy():
    """Nothing to worry about yet."""
    assert worst_health([]) == SessionHealth.HEALTHY


def test_worst_health_zombie_beats_orphaned():
    """Zombie sits at the top of the severity scale."""
    assert worst_health([SessionHealth.ORPHANED, SessionHealth.ZOMBIE]) == SessionHealth.ZOMBIE


def test_severity_table_covers_all_health_levels():
    """Catch drift between SessionHealth and HEALTH_SEVERITY."""
    assert set(HEALTH_SEVERITY) == set(SessionHealth)
