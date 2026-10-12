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
    assert view["outcome"] == {
        "code": "running",
        "summary": "running",
        "source": "occurrence",
        "summary_reported": False,
    }


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
        "summary_reported": True,
    }
    assert view["artifacts"] == ["/runs/inv1/artifacts"]
    assert view["session_ids"] == ["sess1"]


def test_summary_reported_splits_caller_text_from_generated_text():
    run = _run(invocation_id="inv1")
    reported = {
        "id": "s",
        "status": "completed",
        "created_at": 1.0,
        "status_reason_summary": "3 commits landed",
    }
    generated = {"id": "s", "status": "completed", "created_at": 1.0, "artifacts_path": "/p/1"}
    invocation = {"id": "inv1", "status": "completed"}
    assert build_outcome(run, invocation, [reported])["summary_reported"] is True
    outcome = build_outcome(run, invocation, [generated])
    assert outcome["summary_reported"] is False
    assert outcome["summary"] == "completed: 1 artifact(s)"


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
        "summary_reported": True,
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
        "summary_reported": True,
    }


def test_multiple_sessions_grade_from_the_least_trusted_not_the_newest():
    """The failure is the OLDER session, so recency and severity disagree.

    A reducer that picks the newest reports this run ok; the run had a session
    fail, so ok is the wrong answer. Ordering the fixture this way is what
    makes the assertion about the rule rather than about the fixture.
    """
    run = _run(status="completed", invocation_id="inv1")
    invocation = {"id": "inv1", "status": "completed"}
    older = {
        "id": "sess-old",
        "status": "failed",
        "created_at": 1.0,
        "status_reason_summary": "first attempt failed",
    }
    newer = {"id": "sess-new", "status": "completed", "created_at": 5.0}
    view = build_run_view(run, invocation, [older, newer])
    assert view["outcome"]["source"] == "session"
    assert view["outcome"]["summary"] == "first attempt failed"
    assert set(view["session_ids"]) == {"sess-old", "sess-new"}


def test_one_finished_session_does_not_speak_for_six_killed_ones():
    """Six sessions sigterm'd, the newest completed. Reporting ok here is the
    silent green the reducer exists to prevent: the answer to "did this run
    succeed" is no, and it is no six times over."""
    run = _run(status="completed", exit_code=0, invocation_id="inv1")
    invocation = {"id": "inv1", "status": "completed"}
    killed = [
        {
            "id": f"sess-killed-{i}",
            "status": "cancelled",
            "created_at": float(i),
            "status_reason_code": "run.cancelled.sigterm",
            "status_reason_summary": "sigterm",
        }
        for i in range(6)
    ]
    survivor = {"id": "sess-ok", "status": "completed", "created_at": 99.0}
    outcome = build_outcome(run, invocation, [*killed, survivor])
    assert outcome["source"] == "session"
    assert outcome["code"] == "run.cancelled.sigterm"
    assert outcome["code"] != "run.completed.ok"
    assert exit_code_for_view(run, invocation, [*killed, survivor]) == 143


def test_completed_occurrence_is_still_graded_by_its_killed_session():
    """The other direction, from a real occurrence: the schedule_run row said
    completed with exit 0 and seven sessions attached, one of them sigterm'd.

    Grading from the occurrence, or by majority, or by "most of it worked"
    turns this ok and loses the kill. The occurrence's own status is not
    evidence that every session it fanned out to survived.
    """
    run = _run(status="completed", exit_code=0, invocation_id="inv1")
    invocation = {"id": "inv1", "status": "completed"}
    finished = [
        {
            "id": f"sess-{i}",
            "status": "completed",
            "created_at": float(i),
            "artifacts_path": f"/p/{i}",
        }
        for i in range(6)
    ]
    killed = {
        "id": "sess-killed",
        "status": "cancelled",
        "created_at": 99.0,
        "status_reason_code": "run.cancelled.sigterm",
        "status_reason_summary": "unclassified",
    }
    outcome = build_outcome(run, invocation, [*finished, killed])
    assert outcome == {
        "code": "run.cancelled.sigterm",
        "summary": "unclassified",
        "source": "session",
        "summary_reported": True,
    }
    assert exit_code_for_view(run, invocation, [*finished, killed]) == 143


def test_a_session_still_in_flight_withholds_the_session_level_grade():
    """One session finished, another is still running: the run is not over, so
    the sessions have no terminal outcome to give and the caller falls through
    to the surfaces that know it is still running. Grading the finished one
    would report a live run as done."""
    from lionagi.cli.status import EXIT_RUNNING

    run = _run(status="running", exit_code=None, ended_at=None, invocation_id="inv1")
    invocation = {"id": "inv1", "status": "running"}
    done = {"id": "sess-done", "status": "completed", "created_at": 1.0}
    inflight = {"id": "sess-live", "status": "running", "created_at": 2.0}
    outcome = build_outcome(run, invocation, [done, inflight])
    assert outcome["source"] != "session"
    assert outcome["code"] == "running"
    assert exit_code_for_view(run, invocation, [done, inflight]) == EXIT_RUNNING


def test_single_session_runs_grade_exactly_as_before():
    """The rule changes which session speaks for a run, so it can only change
    an answer where there is more than one session. Pinned so a later widening
    of the rule has to notice it is changing the common case."""
    run = _run(status="completed", invocation_id="inv1")
    invocation = {"id": "inv1", "status": "completed"}
    for status, expected in (
        ("completed", "run.completed.ok"),
        ("completed_empty", "run.completed_empty.no_evidence"),
        ("failed", "run.failed.failed"),
        ("cancelled", "run.failed.cancelled"),
    ):
        session = {"id": "solo", "status": status, "created_at": 1.0}
        assert build_outcome(run, invocation, [session])["code"] == expected


def test_severity_order_is_the_shared_exit_code_table_not_a_second_one():
    """completed < completed_empty/failed < timed_out < aborted < cancelled,
    read off EXIT_CODE_BY_STATUS rather than restated here."""
    from lionagi.cli._util import EXIT_CODE_BY_STATUS

    run = _run(status="completed", invocation_id="inv1")
    invocation = {"id": "inv1", "status": "completed"}
    ranked = sorted(EXIT_CODE_BY_STATUS, key=lambda s: EXIT_CODE_BY_STATUS[s])
    assert ranked[0] == "completed", ranked
    for worse in ranked[1:]:
        sessions = [
            {"id": "clean", "status": "completed", "created_at": 99.0},
            {"id": "worse", "status": worse, "created_at": 1.0},
        ]
        outcome = build_outcome(run, invocation, sessions)
        assert outcome["code"] != "run.completed.ok", (worse, outcome)
        assert exit_code_for_view(run, invocation, sessions) == EXIT_CODE_BY_STATUS[worse]


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


# exit_code_for_view: reuses the existing shared status vocabulary


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


def test_every_outcome_this_module_returns_declares_its_summary_provenance():
    """Builder seven has to declare, and this is what tells whoever writes it.

    The consumer treats an outcome with no declaration as caller-reported and
    classifies it, which costs a readable summary rather than leaking one. That
    is the safe direction to fail, not a reason to leave the omission silent.
    """
    import ast
    import inspect

    from lionagi.studio.services import run_view

    tree = ast.parse(inspect.getsource(run_view))
    outcomes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        keys = {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
        if "source" in keys:
            outcomes.append((node.lineno, keys))

    assert len(outcomes) >= 4, f"outcome returns not found; the parse is wrong: {outcomes}"
    missing = [line for line, keys in outcomes if "summary_reported" not in keys]
    assert not missing, f"outcome dicts at line(s) {missing} declare no summary provenance"


def test_an_outcome_that_declares_nothing_is_treated_as_reported():
    from lionagi.studio.services.schedules import _reported_summary_class

    undeclared = {
        "code": "x",
        "summary": "PermissionError: /home/someone/.ssh/id_rsa",
        "source": "?",
    }
    assert _reported_summary_class(undeclared) == "permission"
    assert _reported_summary_class({**undeclared, "summary_reported": False}) is None
