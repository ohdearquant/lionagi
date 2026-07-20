# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for the RunView reducer: outcome precedence over schedule_runs,
invocations, and sessions."""

from __future__ import annotations

import pytest

from lionagi.studio.services.run_view import (
    build_outcome,
    build_run_view,
    exit_code_for_view,
)

_BASE_RUN = {
    "id": "run1",
    "schedule_id": "sched1",
    "status": "completed",
    "exit_code": 0,
    "fired_at": 1000.0,
    "ended_at": 1002.5,
    "error_detail": None,
    "invocation_id": None,
}


def _run(**overrides):
    return {**_BASE_RUN, **overrides}


def test_duration_ms_computed_when_both_timestamps_present():
    view = build_run_view(_run(), None, [])
    assert view["duration_ms"] == 2500


def test_duration_ms_none_when_still_running():
    view = build_run_view(_run(status="running", ended_at=None), None, [])
    assert view["duration_ms"] is None


def test_pre_invocation_failure_uses_occurrence_error_detail():
    """Dispatch/pre-session failure: no invocation exists yet, only error_detail."""
    run = _run(status="failed", exit_code=None, error_detail="dispatch failed: missing cwd")
    view = build_run_view(run, None, [])
    assert view["outcome"]["source"] == "occurrence"
    assert view["outcome"]["summary"] == "dispatch failed: missing cwd"


def test_running_outcome_is_not_terminal():
    run = _run(status="running", ended_at=None)
    view = build_run_view(run, None, [])
    assert view["outcome"] == {"code": "running", "summary": "running", "source": "occurrence"}


def test_trusted_completion_prefers_session_reason():
    run = _run(invocation_id="inv1")
    invocation = {"id": "inv1", "status": "completed", "status_reason_summary": "invocation ok"}
    session = {
        "id": "sess1",
        "status": "completed",
        "created_at": 1.0,
        "status_reason_summary": "3 commits landed",
        "artifacts_path": "/runs/inv1/artifacts",
    }
    view = build_run_view(run, invocation, [session])
    assert view["outcome"] == {
        "code": "run.completed.ok",
        "summary": "3 commits landed",
        "source": "session",
    }
    assert view["artifacts"] == ["/runs/inv1/artifacts"]
    assert view["session_ids"] == ["sess1"]


def test_completed_empty_distinct_from_unqualified_success():
    run = _run(invocation_id="inv1")
    invocation = {"id": "inv1", "status": "completed_empty"}
    session = {"id": "sess1", "status": "completed_empty", "created_at": 1.0}
    outcome = build_outcome(run, invocation, [session])
    assert outcome["code"] == "run.completed_empty.no_evidence"
    assert "no_evidence" in outcome["code"] or "no artifacts" in outcome["summary"]


def test_failure_outcome_from_session():
    run = _run(status="failed", exit_code=1, invocation_id="inv1")
    invocation = {"id": "inv1", "status": "failed"}
    session = {
        "id": "sess1",
        "status": "failed",
        "created_at": 1.0,
        "status_reason_summary": "tests failed",
    }
    outcome = build_outcome(run, invocation, [session])
    assert outcome["source"] == "session"
    assert outcome["summary"] == "tests failed"


def test_timeout_outcome_from_session():
    run = _run(status="failed", exit_code=124, invocation_id="inv1")
    invocation = {"id": "inv1", "status": "timed_out"}
    session = {"id": "sess1", "status": "timed_out", "created_at": 1.0}
    outcome = build_outcome(run, invocation, [session])
    assert outcome["source"] == "session"
    assert "timed_out" in outcome["summary"] or "timed_out" in outcome["code"]


def test_skip_outcome_from_occurrence():
    run = _run(status="skipped", exit_code=None, error_detail="overlap policy: prior running")
    outcome = build_outcome(run, None, [])
    assert outcome == {
        "code": "skipped",
        "summary": "overlap policy: prior running",
        "source": "occurrence",
    }


def test_missing_session_falls_back_to_invocation():
    """Invocation is terminal but no session rows exist at all (e.g. a
    command target with no session concept)."""
    run = _run(status="completed", invocation_id="inv1")
    invocation = {"id": "inv1", "status": "completed", "status_reason_summary": "argv exited 0"}
    outcome = build_outcome(run, invocation, [])
    assert outcome == {
        "code": "run.completed.ok",
        "summary": "argv exited 0",
        "source": "invocation",
    }


def test_multiple_sessions_picks_most_recently_created_as_primary():
    run = _run(status="completed", invocation_id="inv1")
    invocation = {"id": "inv1", "status": "completed"}
    older = {"id": "sess-old", "status": "completed", "created_at": 1.0}
    newer = {
        "id": "sess-new",
        "status": "failed",
        "created_at": 5.0,
        "status_reason_summary": "retry attempt failed",
    }
    view = build_run_view(run, invocation, [older, newer])
    assert view["outcome"]["source"] == "session"
    assert view["outcome"]["summary"] == "retry attempt failed"
    assert set(view["session_ids"]) == {"sess-old", "sess-new"}


def test_artifact_paths_deduped_and_ordered():
    run = _run(invocation_id="inv1")
    sessions = [
        {"id": "a", "status": "completed", "created_at": 1.0, "artifacts_path": "/p/1"},
        {"id": "b", "status": "completed", "created_at": 2.0, "artifacts_path": "/p/1"},
        {"id": "c", "status": "completed", "created_at": 3.0, "artifacts_path": "/p/2"},
    ]
    view = build_run_view(run, {"id": "inv1", "status": "completed"}, sessions)
    assert view["artifacts"] == ["/p/1", "/p/2"]


def test_crash_state_disagreement_session_wins_over_invocation_and_occurrence():
    """schedule_run says 'failed', invocation says 'running' (crashed before
    update), session is the freshest terminal truth — session must win."""
    run = _run(status="failed", exit_code=1, error_detail="orphan tombstoned", invocation_id="inv1")
    invocation = {"id": "inv1", "status": "running"}
    session = {
        "id": "sess1",
        "status": "completed",
        "created_at": 1.0,
        "status_reason_summary": "actually finished before the crash",
    }
    outcome = build_outcome(run, invocation, [session])
    assert outcome["source"] == "session"
    assert outcome["summary"] == "actually finished before the crash"


def test_fallback_outcome_when_no_invocation_or_session_and_no_error_detail():
    run = _run(status="failed", exit_code=1, error_detail=None)
    outcome = build_outcome(run, None, [])
    assert outcome["source"] == "fallback"
    assert outcome["code"] == "failed_exit_nonzero"


def test_fallback_outcome_success_by_exit_code():
    run = _run(status="completed", exit_code=0, error_detail=None)
    outcome = build_outcome(run, None, [])
    assert outcome["source"] == "fallback"
    assert outcome["code"] == "completed"


def test_fallback_outcome_status_dominates_over_exit_code_zero():
    """A terminal non-completed status with exit_code 0 (e.g. crashed before
    any exit code was ever meaningfully set) must never read as 'completed'."""
    run = _run(status="failed", exit_code=0, error_detail=None)
    outcome = build_outcome(run, None, [])
    assert outcome["source"] == "fallback"
    assert outcome["code"] == "failed"
    assert outcome["code"] != "completed"


# ── exit_code_for_view: reuses the existing shared status vocabulary ───────


@pytest.mark.parametrize(
    "session_status, expected",
    [
        ("completed", 0),
        ("completed_empty", 1),
        ("failed", 1),
        ("timed_out", 124),
        ("aborted", 130),
        ("cancelled", 143),
    ],
)
def test_exit_code_reflects_session_terminal_status(session_status, expected):
    run = _run(
        status="completed" if session_status == "completed" else "failed", invocation_id="inv1"
    )
    invocation = {"id": "inv1", "status": "completed"}
    session = {"id": "sess1", "status": session_status, "created_at": 1.0}
    assert exit_code_for_view(run, invocation, [session]) == expected


def test_exit_code_running_is_exit_running():
    from lionagi.cli.status import EXIT_RUNNING

    run = _run(status="running", ended_at=None)
    assert exit_code_for_view(run, None, []) == EXIT_RUNNING


def test_exit_code_no_run_is_exit_unknown():
    from lionagi.cli.status import EXIT_UNKNOWN

    assert exit_code_for_view(None, None, []) == EXIT_UNKNOWN


def test_exit_code_skipped_is_ordinary_failure_bucket():
    run = _run(status="skipped", exit_code=None)
    assert exit_code_for_view(run, None, []) == 1


def test_exit_code_pre_invocation_failure_no_session_or_invocation():
    run = _run(status="failed", exit_code=1)
    assert exit_code_for_view(run, None, []) == 1


@pytest.mark.parametrize(
    "occurrence_status, expected",
    [
        ("timed_out", 124),
        ("cancelled", 143),
    ],
)
def test_exit_code_occurrence_only_terminal_status_uses_shared_vocabulary(
    occurrence_status, expected
):
    """No session or invocation reached terminal — the occurrence status
    itself must still map through EXIT_CODE_BY_STATUS, not collapse to 1."""
    run = _run(status=occurrence_status, exit_code=None)
    assert exit_code_for_view(run, None, []) == expected
