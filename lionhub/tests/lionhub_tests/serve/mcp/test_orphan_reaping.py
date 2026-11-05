# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A run whose process is conclusively gone gets one attributable end.

No test here spawns a long-running child or signals a process it did not start:
the liveness probes are doubled, so the observations under test are the ones
this module classifies rather than ones the machine happens to produce. The
concurrency tests do use the real per-run lock, in threads, because the property
being checked is that the lock serializes — a doubled lock would prove nothing.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from lionagi.state.reasons import RunReasons
from lionhub.serve.mcp import _notify_hook, config, jobs

_SPAWNED_AT = 1_700_000_000.0


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Point job/run state at a tmp dir so tests never touch the real ~/.lionagi."""
    monkeypatch.setattr(config, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(config, "li_command", lambda: ["echo"])
    monkeypatch.setattr(jobs, "_read_lifecycle", lambda run_id: None)
    return tmp_path


@pytest.fixture
def no_delivery(monkeypatch):
    """Record every delivery attempt and make none of them run a command.

    Returned as the list of run ids a notice was attempted for, so a test can
    assert that exactly one was made — or that none was.
    """
    attempts: list[str] = []

    def _fake(run_id, job, status, **kw):
        attempts.append(run_id)
        return {"attempted": True, "ok": True, "exit_code": 0, "error": None, "command": "notify"}

    monkeypatch.setattr(_notify_hook, "deliver_terminal_notice", _fake)
    return attempts


def _record(rid: str, **fields: Any) -> str:
    base: dict[str, Any] = {
        "run_id": rid,
        "pid": 4242,
        "pid_create_time": _SPAWNED_AT,
        "pgid": 4242,
        "kind": "agent",
        "label": "a-label",
        "status": "running",
        "spawn_state": "started",
        "submitted_at": "2026-07-25T00:00:00+00:00",
        "finished_at": None,
        "log": None,
    }
    base.update(fields)
    jobs._write_job(base)
    return rid


def _stranded(**fields: Any) -> str:
    return _record(jobs.new_run_id(), **fields)


def _pid_absent(monkeypatch) -> None:
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)


def _disappeared_during_probe(monkeypatch) -> None:
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("gone", None))


def _pid_recycled(monkeypatch) -> None:
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", _SPAWNED_AT + 5000))
    # Two agreeing mismatching reads only conclude `pid_recycled` once the
    # inherited marker names a different run; a foreign marker here is
    # what makes that positive.
    monkeypatch.setattr(jobs, "_process_marker", lambda pid: ("found", "some-other-run-id"))


_CONCLUSIVE = {
    jobs.FINDING_PID_ABSENT: _pid_absent,
    jobs.FINDING_DISAPPEARED_DURING_PROBE: _disappeared_during_probe,
    jobs.FINDING_PID_RECYCLED: _pid_recycled,
}


# --- verify-by 1: each positive finding writes exactly one transition ----------


@pytest.mark.parametrize("finding", sorted(_CONCLUSIVE))
def test_each_conclusive_finding_writes_one_terminal_transition(
    sandbox, monkeypatch, no_delivery, finding
):
    """Three observations, one contract, and one transition each.

    Each of these positively establishes that the process this run recorded is
    gone: the pid held nothing when it was asked, it emptied between the two
    probes, or a live process holds the number and is demonstrably a different
    one. Each ends the run once — the second read of the same record returns
    what the first one wrote, without observing anything, which is the property
    that keeps two readers from disagreeing.
    """
    _CONCLUSIVE[finding](monkeypatch)
    rid = _stranded()

    first = jobs.status(rid)
    assert first["terminal"] is True
    assert first["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert first["reason_code"] == jobs.LOST_REASON
    assert first["liveness_conclusion"] == "process_gone"
    assert first["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER
    evidence = first["terminal_evidence"]
    assert evidence["kind"] == jobs.EVIDENCE_PROCESS_GONE
    assert evidence["finding"] == finding
    assert evidence["recorded_create_time"] == _SPAWNED_AT
    assert evidence["tolerance"] == jobs._CREATE_TIME_TOLERANCE
    assert evidence["observed_at"] == first["finished_at"]
    assert evidence["observer"] == jobs._server_identity()
    if finding == jobs.FINDING_PID_RECYCLED:
        assert evidence["observed_create_time"] == _SPAWNED_AT + 5000
    else:
        assert evidence["observed_create_time"] is None

    on_disk = jobs._read_job(rid)
    assert on_disk["finished_at"] == first["finished_at"] is not None
    assert on_disk["outcome"] == jobs.OUTCOME_INDETERMINATE

    # The second reader answers from the record alone. It is given the opposite
    # observation — a live, confirmed process at that pid — and must still
    # report the end, because a latch that a later observation could move is not
    # one two readers can agree on.
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", _SPAWNED_AT))
    second = jobs.status(rid)
    assert second["alive"] is True
    assert second["terminal"] is True
    assert second["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert second["finished_at"] == first["finished_at"]
    assert len(no_delivery) == 1


# --- verify-by 2: nothing inconclusive ends a run -----------------------------


def _unusable_pid(monkeypatch) -> dict[str, Any]:
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    return {"pid": "4242"}


def _access_denied(monkeypatch) -> dict[str, Any]:
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("unknown", None))
    return {}


def _creation_time_unreadable(monkeypatch) -> dict[str, Any]:
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("unknown", None))
    return {"pid_create_time": None}


def _preparing(monkeypatch) -> dict[str, Any]:
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: False)
    return {"pid": None, "pid_create_time": None, "pgid": None, "spawn_state": "preparing"}


@pytest.mark.parametrize(
    "setup",
    [_unusable_pid, _access_denied, _creation_time_unreadable, _preparing],
    ids=["unusable_pid", "access_denied", "creation_time_unreadable", "preparing"],
)
def test_an_observation_that_establishes_nothing_never_ends_a_run(
    sandbox, monkeypatch, no_delivery, setup
):
    """The four inconclusive shapes stay where they were, and wake nobody.

    None of these is evidence that this run's process is gone. A pid the OS
    cannot be asked about was never probed; a denied or unreadable identity read
    is a measurement that did not come off; a spawn still preparing never
    acquired a process identity to lose. Ending a run on any of them would latch
    a claim the observation does not support, and — since the end owns the
    notice — would also send a completion notice for a run that may be running.
    """
    fields = setup(monkeypatch)
    rid = _stranded(**fields)

    st = jobs.status(rid)

    assert st["liveness_conclusion"] != "process_gone"
    assert st["terminal"] is False
    assert st["outcome"] is None
    assert st["finished_at"] is None
    assert st["terminal_source"] is None
    assert jobs._read_job(rid)["finished_at"] is None
    assert no_delivery == []


def test_a_finding_outside_the_conclusive_set_is_refused_at_the_primitive(sandbox, no_delivery):
    """The admission rule is membership in the positive set, and nothing else.

    Called directly with a finding that names a real inconclusive observation,
    the guarded operation refuses it. That is the property that survives someone
    adding a value to the liveness vocabulary later: a new finding is not
    conclusive until it is written down as conclusive.
    """
    rid = _stranded()

    result = jobs.reap_orphan(
        rid, finding="identity_unreadable", observed_at="2026-07-27T00:00:00+00:00"
    )

    assert result.won_transition is False
    assert jobs._read_job(rid)["finished_at"] is None
    assert no_delivery == []


def test_reap_evidence_cannot_override_kind_or_finding(sandbox, no_delivery):
    """*evidence* is merged onto `terminal_evidence` beside `kind`/`finding`,
    never over them — a caller supplying either key is refused before the
    lock is taken, and nothing is written.
    """
    rid = _stranded()

    with pytest.raises(ValueError):
        jobs.reap_orphan(
            rid,
            finding=jobs.FINDING_PID_ABSENT,
            observed_at="2026-07-27T00:00:00+00:00",
            evidence={"finding": "x", "secret": "y"},
        )

    assert jobs._read_job(rid)["finished_at"] is None
    assert no_delivery == []


def test_reap_evidence_rejects_unknown_keys(sandbox, no_delivery):
    """A key this branch never populates is refused, not silently accepted —
    the allow-list is exhaustive, not a denylist of the two reserved names.
    """
    rid = _stranded()

    with pytest.raises(ValueError):
        jobs.reap_orphan(
            rid,
            finding=jobs.FINDING_PID_ABSENT,
            observed_at="2026-07-27T00:00:00+00:00",
            evidence={"payload": "whatever an attacker likes"},
        )

    assert jobs._read_job(rid)["finished_at"] is None
    assert no_delivery == []


def test_reap_evidence_rejects_wrong_shaped_values(sandbox, no_delivery):
    """A known key with a value outside what it's allowed to hold — an
    oversized string standing in for `observed_at` — is refused the same way.
    """
    rid = _stranded()

    with pytest.raises(ValueError):
        jobs.reap_orphan(
            rid,
            finding=jobs.FINDING_PID_ABSENT,
            observed_at="2026-07-27T00:00:00+00:00",
            evidence={"observed_at": "x" * 1000},
        )

    assert jobs._read_job(rid)["finished_at"] is None
    assert no_delivery == []


def test_reap_evidence_accepts_the_bounded_shape_this_branch_writes(sandbox, no_delivery):
    """The exact shape `_reap_if_conclusively_gone` builds is accepted — the
    allow-list bounds a caller, not the module's own evidence construction.
    """
    rid = _stranded()

    result = jobs.reap_orphan(
        rid,
        finding=jobs.FINDING_PID_RECYCLED,
        observed_at="2026-07-27T00:00:00+00:00",
        evidence={
            "observed_create_time": _SPAWNED_AT + 5000,
            "recorded_create_time": _SPAWNED_AT,
            "tolerance": jobs._CREATE_TIME_TOLERANCE,
            "observed_at": "2026-07-27T00:00:00+00:00",
            "observer": jobs._server_identity(),
            "marker": "some-other-run-id",
        },
    )

    assert result.won_transition is True
    assert result.record["terminal_evidence"]["marker"] == "some-other-run-id"


# --- verify-by 3 and 4: interleavings ------------------------------------------


def _hold_lock(rid: str):
    """Hold the real per-run lock, so a mutation under test genuinely blocks."""
    return jobs._locked_job(rid)


def _run_blocked(fn) -> tuple[threading.Thread, list[Any]]:
    """Start *fn* in a thread and hand back the thread and where it will answer."""
    out: list[Any] = []
    t = threading.Thread(target=lambda: out.append(fn()), daemon=True)
    t.start()
    return t, out


def test_two_observers_produce_one_transition_and_one_notice(sandbox, monkeypatch, no_delivery):
    """Two readers racing on one stranded run end it once between them.

    The interleaving is forced rather than hoped for: the test takes the run's
    own mutation lock first, so both observers reach their guarded write and
    stop there, with both of them holding a snapshot that says the run has not
    ended. Releasing the lock lets them proceed one after the other, which is
    exactly the double transition the guard has to prevent — the loser rereads
    inside the lock, finds the end, and returns it instead of writing a second
    one. The notice belongs to whoever won, so one attempt is made, not two.
    """
    _pid_absent(monkeypatch)
    rid = _stranded()

    with _hold_lock(rid):
        threads = [_run_blocked(lambda: jobs.status(rid)) for _ in range(2)]
        for t, _ in threads:
            t.join(timeout=0.5)
            assert t.is_alive(), "an observer that did not block took no lock to be serialized by"

    results = []
    for t, out in threads:
        t.join(timeout=10)
        assert not t.is_alive()
        results.extend(out)

    assert len(results) == 2
    assert all(r["terminal"] is True for r in results)
    assert all(r["outcome"] == jobs.OUTCOME_INDETERMINATE for r in results)
    # One transition: both readers report the same recorded moment.
    assert results[0]["finished_at"] == results[1]["finished_at"]
    assert len(no_delivery) == 1


def test_a_hook_end_that_lands_first_survives_the_reaper(sandbox, monkeypatch, no_delivery):
    """The reaper holds a stale snapshot and must not write over a reported end.

    The observer reads the record, concludes the process is gone, and reaches
    its guarded write — where the test is holding the lock. The hook's end is
    recorded inside that same lock, under the observer's feet. What the observer
    then does is the whole question: rereading is what makes it report the
    reported end, and merging its own snapshot would replace how the run came
    out with a guess about it.
    """
    _pid_absent(monkeypatch)
    rid = _stranded()

    with _hold_lock(rid) as guard:
        thread, out = _run_blocked(lambda: jobs.status(rid))
        thread.join(timeout=0.5)
        assert thread.is_alive()
        guard.record["status"] = "completed"
        guard.record["finished_at"] = "2026-07-27T00:00:00+00:00"
        guard.record["terminal_source"] = jobs.TERMINAL_SOURCE_HOOK

    thread.join(timeout=10)
    st = out[0]

    assert st["status"] == "completed"
    assert st["terminal"] is True
    assert st["outcome"] == "succeeded"
    assert st["terminal_source"] == jobs.TERMINAL_SOURCE_HOOK
    assert jobs._read_job(rid)["finished_at"] == "2026-07-27T00:00:00+00:00"
    assert no_delivery == [], "the run reported its own end, so its own hook owns the notice"


def test_a_lifecycle_end_cached_first_survives_the_reaper(sandbox, monkeypatch, no_delivery):
    """The same, for the end a kill leaves in the lifecycle store.

    A cancelled run's only trace is the lifecycle row, and an observer that
    cached it got there with a reported status and reason. An inferred loss must
    not overwrite either.
    """
    _pid_absent(monkeypatch)
    rid = _stranded()

    with _hold_lock(rid) as guard:
        thread, out = _run_blocked(lambda: jobs.status(rid))
        thread.join(timeout=0.5)
        assert thread.is_alive()
        guard.record.update(
            {
                "status": "cancelled",
                "finished_at": "2026-07-27T00:00:00+00:00",
                "reason_code": "killed_by_operator",
                "terminal_source": jobs.TERMINAL_SOURCE_LIFECYCLE,
            }
        )

    thread.join(timeout=10)
    st = out[0]

    assert st["outcome"] == "cancelled"
    assert st["reason_code"] == "killed_by_operator"
    assert st["terminal_source"] == jobs.TERMINAL_SOURCE_LIFECYCLE
    assert no_delivery == []


def test_the_pid_attachment_cannot_roll_back_a_reaped_end(sandbox, monkeypatch, no_delivery):
    """The submit-side write adds identity fields and touches nothing else.

    It is the one mutation that runs while a run is still being created, so it
    is also the one most likely to hold a record older than everything else on
    disk. Reaching the lock after a transition, it must merge its own fields and
    leave the end and the delivery result exactly as they are.
    """
    _pid_absent(monkeypatch)
    rid = _stranded(pid_create_time=None, pgid=None, spawn_state="started")

    reaped = jobs.status(rid)
    assert reaped["outcome"] == jobs.OUTCOME_INDETERMINATE

    with jobs._locked_job(rid) as guard:
        guard.record["pid"] = 4242
        guard.record["pid_create_time"] = _SPAWNED_AT
        guard.record["pgid"] = 4242
        guard.record["spawn_state"] = "started"

    after = jobs._read_job(rid)
    assert after["pid"] == 4242
    assert after["finished_at"] == reaped["finished_at"]
    assert after["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert after["notify_delivery"]["ok"] is True


def test_a_delivery_result_never_rolls_terminal_fields_backward(sandbox, monkeypatch, no_delivery):
    """A notice result merges itself and carries no stale lifecycle fields.

    The hook writes the delivery outcome after the run has ended, from a process
    that read the record before it ended. Merging its whole copy back would
    un-terminalise the run; merging the one field it owns cannot.
    """
    _pid_absent(monkeypatch)
    rid = _stranded()
    reaped = jobs.status(rid)

    jobs.record_notify_delivery(
        rid, {"attempted": True, "ok": False, "exit_code": 7, "error": None}
    )

    after = jobs._read_job(rid)
    assert after["finished_at"] == reaped["finished_at"]
    assert after["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert after["notify_delivery"]["exit_code"] == 7
    assert jobs.status(rid)["outcome"] == jobs.OUTCOME_INDETERMINATE


def test_a_hook_arriving_after_a_reap_leaves_the_latch_and_records_the_report(
    sandbox, monkeypatch, no_delivery
):
    """The child's own hook, running late, is the owner reporting in — but the
    first recorded terminal state remains authoritative, including the reap's
    `indeterminate`: the hook's report does not replace it. What it leaves is
    a `late_report` beside the untouched latch, so the contradiction between
    what the reaper concluded and what the run's own process reported stays
    visible without moving anything the reap already wrote.
    """
    _pid_absent(monkeypatch)
    rid = _stranded()
    reaped = jobs.status(rid)
    assert reaped["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER
    assert reaped["outcome"] == jobs.OUTCOME_INDETERMINATE

    kept = jobs.mark_terminal(rid, "completed").record

    assert kept["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER
    assert kept["finished_at"] == reaped["finished_at"]
    assert kept["status"] == reaped["status"]
    assert kept["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert "superseded_reap" not in kept
    late_report = kept["late_report"]
    assert late_report["source"] == "hook"
    assert late_report["status"] == "completed"
    assert late_report["outcome"] == "succeeded"
    assert late_report["reported_at"]

    st = jobs.status(rid)
    assert st["status"] == reaped["status"]
    assert st["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert st["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER
    assert st["reason_code"] == jobs.LOST_REASON
    assert st["reap_contradicted_by_owner_report"] is True
    assert st["late_report"] == late_report

    # First late report wins too — a second one is dropped, not overwritten.
    again = jobs.mark_terminal(rid, "timed_out", reason_code="deadline").record
    assert again["late_report"] == late_report


def test_a_lifecycle_end_arriving_after_a_reap_leaves_the_latch_and_records_the_report(
    sandbox, monkeypatch, no_delivery
):
    """The same first-writer-wins rule, reached through the lifecycle cache
    race instead of the hook: a `_cache_lifecycle_end` call that started
    before the reap and lost the lock to it must not move the latched end,
    only record its own report beside it.
    """
    _pid_absent(monkeypatch)
    rid = _stranded()
    reaped = jobs.status(rid)
    assert reaped["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER

    stale_job = jobs._read_job(rid)
    updated = jobs._cache_lifecycle_end(
        stale_job,
        {
            "terminal": True,
            "status": "cancelled",
            "ended_at": _SPAWNED_AT + 30,
            "reason_code": "killed_by_operator",
        },
    )

    assert updated["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER
    assert updated["finished_at"] == reaped["finished_at"]
    assert updated["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert "superseded_reap" not in updated
    late_report = updated["late_report"]
    assert late_report["source"] == "lifecycle"
    assert late_report["status"] == "cancelled"
    assert late_report["outcome"] == "cancelled"
    assert late_report["reason_code"] == "killed_by_operator"

    st = jobs.status(rid)
    assert st["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert st["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER
    assert st["reap_contradicted_by_owner_report"] is True
    assert st["late_report"] == late_report


def test_a_lifecycle_report_with_a_non_string_status_is_sanitized_after_a_reap(
    sandbox, monkeypatch, no_delivery
):
    _pid_absent(monkeypatch)
    rid = _stranded()
    jobs.status(rid)

    updated = jobs._cache_lifecycle_end(
        jobs._read_job(rid),
        {
            "terminal": True,
            "status": ["completed"],
            "ended_at": _SPAWNED_AT + 30,
            "reason_code": "killed_by_operator",
        },
    )

    assert updated["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER
    assert updated["late_report"]["status"] == "unknown"
    assert updated["late_report"]["outcome"] == "failed"


def test_a_terminal_record_with_a_non_string_status_is_sanitized(sandbox, monkeypatch, no_delivery):
    _pid_absent(monkeypatch)
    rid = _stranded(status=["completed"], finished_at="2026-07-27T00:00:00+00:00")

    st = jobs.status(rid)

    assert st["status"] == "unknown"
    assert st["terminal"] is True
    assert st["outcome"] == "failed"


# --- confirmation read before a pid_recycled reap ------------------------------


def _two_reads(monkeypatch, first: tuple[str, float | None], second: tuple[str, float | None]):
    """Make `_process_create_time` answer *first*, then *second*; a third call
    fails the test outright rather than repeating *second* forever, since a
    probe called more than twice here is a bug in the liveness function under
    test, not something a fixture should mask by answering on its behalf."""
    calls = iter((first, second))

    def _fake(pid: int) -> tuple[str, float | None]:
        try:
            return next(calls)
        except StopIteration:
            raise AssertionError("unexpected third create-time read")

    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", _fake)


def _agreeing_mismatch_with_marker(
    monkeypatch,
    *,
    marker: tuple[str, str | None],
    bracket: tuple[str, float | None] | None = None,
):
    """Two agreeing live reads that both mismatch the record, followed by the
    marker consultation `_run_process_liveness` makes before concluding
    anything from that agreement, and the bracketing create-time re-read the
    consultation takes right after the marker read. *bracket* defaults to
    matching the two agreeing reads; a fourth create-time call fails the test,
    same discipline as `_two_reads`.
    """
    agreeing = ("found", _SPAWNED_AT + 5000)
    bracket = bracket or agreeing
    calls = iter((agreeing, agreeing, bracket))

    def _fake_time(pid: int) -> tuple[str, float | None]:
        try:
            return next(calls)
        except StopIteration:
            raise AssertionError("unexpected extra create-time read")

    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", _fake_time)
    monkeypatch.setattr(jobs, "_process_marker", lambda pid: marker)


def test_a_mismatch_confirmed_gone_is_disappeared_during_probe(sandbox, monkeypatch, no_delivery):
    """First read mismatches the record; the process is gone by the second."""
    _two_reads(monkeypatch, ("found", _SPAWNED_AT + 5000), ("gone", None))
    rid = _stranded()

    st = jobs.status(rid)

    assert st["liveness_conclusion"] == "process_gone"
    assert st["terminal_evidence"]["finding"] == jobs.FINDING_DISAPPEARED_DURING_PROBE


def test_two_agreeing_mismatched_reads_with_a_foreign_marker_are_pid_recycled(
    sandbox, monkeypatch, no_delivery
):
    """(b) Two live reads agreeing with each other, both mismatching the
    record, are proof the two reads agree — not proof the single spawn-time
    read the record carries was the accurate one. `pid_recycled` is concluded
    only once the marker inherited from another run names a *different* run.
    """
    rid = _stranded()
    _agreeing_mismatch_with_marker(monkeypatch, marker=("found", "some-other-run-id"))

    st = jobs.status(rid)

    assert st["liveness_conclusion"] == "process_gone"
    assert st["pid_identity"] == "recycled"
    assert st["terminal_evidence"]["finding"] == jobs.FINDING_PID_RECYCLED
    assert st["terminal_evidence"]["observed_create_time"] == _SPAWNED_AT + 5000
    assert st["terminal_evidence"]["recorded_create_time"] == _SPAWNED_AT
    assert st["terminal_evidence"]["marker"] == "some-other-run-id"


def test_two_agreeing_mismatched_reads_confirmed_by_this_runs_marker_stay_alive(
    sandbox, monkeypatch, no_delivery
):
    """(a) The same two agreeing, record-mismatching reads — but the
    marker inherited from this run names it. That confirms the process at this
    pid descends from this run; what's suspect is the record's own recorded
    start time. Nothing is reaped.
    """
    rid = _stranded()
    _agreeing_mismatch_with_marker(monkeypatch, marker=("found", rid))

    st = jobs.status(rid)

    assert st["alive"] is True
    assert st["terminal"] is False
    assert st["liveness_conclusion"] == "alive"
    assert st["pid_identity"] == "confirmed"
    assert jobs._read_job(rid)["finished_at"] is None


def test_two_agreeing_mismatched_reads_with_an_unreadable_marker_settle_nothing(
    sandbox, monkeypatch, no_delivery
):
    """(c) A marker read that could not be made — an unreadable environment,
    or a `found` reply carrying no value at all, which a platform may return
    for a protected process without that meaning ownership was disclaimed —
    rules nothing in or out, so nothing is reaped on it.
    """
    rid = _stranded()
    _agreeing_mismatch_with_marker(monkeypatch, marker=("unknown", None))

    st = jobs.status(rid)

    assert st["alive"] is True
    assert st["terminal"] is False
    assert st["liveness_conclusion"] == "unknown"
    assert st["pid_identity"] == "unconfirmed"
    assert jobs._read_job(rid)["finished_at"] is None


def test_a_marker_match_unsettled_by_a_changing_bracket_read_is_identity_unstable(
    sandbox, monkeypatch, no_delivery
):
    """(d) The marker consultation is bracketed the way `kill` brackets its
    own: even a marker naming this run doesn't settle identity if the
    create-time re-read taken right after it no longer matches the two reads
    that came before — the process at this pid changed out from under the
    probe between the marker read and the bracket read.
    """
    rid = _stranded()
    _agreeing_mismatch_with_marker(
        monkeypatch, marker=("found", rid), bracket=("found", _SPAWNED_AT + 9000)
    )

    st = jobs.status(rid)

    assert st["alive"] is True
    assert st["terminal"] is False
    assert st["liveness_conclusion"] == "unknown"
    assert st["pid_identity"] == "unstable"
    assert jobs._read_job(rid)["finished_at"] is None


def test_two_disagreeing_mismatched_reads_settle_nothing(sandbox, monkeypatch, no_delivery):
    """The two live reads disagree with each other: the probe, not the
    process, is what's unreliable here, and nothing may end the run on it."""
    _two_reads(monkeypatch, ("found", _SPAWNED_AT + 5000), ("found", _SPAWNED_AT + 9000))
    rid = _stranded()

    st = jobs.status(rid)

    assert st["alive"] is True
    assert st["liveness_conclusion"] == "unknown"
    assert st["pid_identity"] == "unstable"
    assert st["terminal"] is False
    assert jobs._read_job(rid)["finished_at"] is None


# --- status() names a reap its own fresh probe now contradicts -----------------


def test_status_names_a_fresh_probe_that_contradicts_its_own_reap(
    sandbox, monkeypatch, no_delivery
):
    """A later call's fresh probe can reconfirm identity after an earlier call
    already reaped the run on the opposite observation. `status()` only reaps
    a record that isn't terminal yet, so this contradiction does not undo the
    reap — but it must be named, not left for a caller to notice by comparing
    `terminal_source` against `alive`/`liveness_conclusion` by hand.
    """
    _pid_absent(monkeypatch)
    rid = _stranded()

    first = jobs.status(rid)
    assert first["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER
    assert first["reap_contradicted_by_fresh_probe"] is False

    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", _SPAWNED_AT))
    second = jobs.status(rid)

    assert second["alive"] is True
    assert second["liveness_conclusion"] == "alive"
    assert second["pid_identity"] == "confirmed"
    assert second["terminal"] is True
    assert second["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER
    assert second["reap_contradicted_by_fresh_probe"] is True


def test_status_names_a_marker_confirmed_probe_that_contradicts_its_own_reap(
    sandbox, monkeypatch, no_delivery
):
    """The contradiction is named for either way identity gets confirmed.

    Two findings confirm that the live process is this run's: a start time that
    matches the record, and a start time that does not while the process's own
    marker does. Only the second can occur here at all — a reap on
    `pid_recycled` happened *because* the start times disagreed, and nothing
    since makes them agree — so a check that recognizes only the first is blind
    to the case the flag exists for.
    """
    _pid_recycled(monkeypatch)
    rid = _stranded()

    first = jobs.status(rid)
    assert first["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER
    assert first["pid_identity"] == "recycled"
    assert first["reap_contradicted_by_fresh_probe"] is False

    # Same disagreeing-but-stable start time; this time the marker is the run's.
    monkeypatch.setattr(jobs, "_process_marker", lambda pid: ("found", rid))
    second = jobs.status(rid)

    assert second["liveness_conclusion"] == "alive"
    assert second["pid_identity"] == "confirmed"
    assert second["terminal"] is True
    assert second["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER
    assert second["reap_contradicted_by_fresh_probe"] is True


# --- a read never raises on evidence it did not choose -------------------------


@pytest.mark.parametrize(
    "recorded",
    ["not-a-number", True, [1, 2], {"a": "b"}],
    ids=["string", "bool", "list", "dict"],
)
def test_a_record_whose_create_time_is_not_a_number_still_reaps(
    sandbox, monkeypatch, no_delivery, recorded
):
    """A malformed `pid_create_time` is a bad record, not a bad caller.

    The value reaches evidence straight off disk, and a dead pid settles the
    finding before the record's own start time is ever consulted — so the
    unusable value arrives at a conclusive reap rather than being screened out
    earlier. Rejecting it there would raise out of a read, lose the transition,
    and leave the run unreapable for good.
    """
    _pid_absent(monkeypatch)
    rid = _stranded(pid_create_time=recorded)

    st = jobs.status(rid)

    assert st["terminal"] is True
    assert st["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER
    assert st["reason_code"] == jobs.LOST_REASON
    # The unusable value is dropped rather than persisted or guessed at.
    assert "recorded_create_time" not in jobs._read_job(rid)["terminal_evidence"]
    assert jobs._read_job(rid)["terminal_evidence"]["finding"] == jobs.FINDING_PID_ABSENT


def test_a_foreign_over_long_marker_does_not_abort_status_or_the_listing(
    sandbox, monkeypatch, no_delivery
):
    """A stranger at a recycled pid controls the marker's length, not us.

    `_process_marker` reads the environment of whatever process now holds the
    pid, so its value is attacker-influenced in the only sense that matters
    here: a long enough one must not turn a status read into an exception, nor
    take out `list_jobs`, which calls `status` once per entry.
    """
    monkeypatch.setattr(jobs, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(jobs, "_process_create_time", lambda pid: ("found", _SPAWNED_AT + 5000))
    monkeypatch.setattr(jobs, "_process_marker", lambda pid: ("found", "x" * 257))
    rid = _stranded()

    st = jobs.status(rid)

    assert st["terminal"] is True
    assert st["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER
    assert "marker" not in jobs._read_job(rid)["terminal_evidence"]
    # The surface a caller actually polls survives it too.
    assert [row["run_id"] for row in jobs.list_jobs()] == [rid]


# --- verify-by 7: list and status agree ---------------------------------------


def test_list_and_status_classify_and_attribute_a_reaped_run_identically(
    sandbox, monkeypatch, no_delivery
):
    """Two surfaces, one classification — including who wrote the end.

    The listing is the surface a caller polls, so a row that showed the outcome
    without the attribution would hide the difference between a run that
    reported its own end and one this server ended on its behalf.
    """
    _pid_absent(monkeypatch)
    rid = _stranded()

    rows = jobs.list_jobs()
    st = jobs.status(rid)

    row = next(r for r in rows if r["run_id"] == rid)
    assert row["terminal"] == st["terminal"] is True
    assert row["outcome"] == st["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert row["reason_code"] == st["reason_code"] == jobs.LOST_REASON
    assert row["finished_at"] == st["finished_at"]
    assert row["terminal_source"] == st["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER
    assert len(no_delivery) == 1, "the listing reaped it; the status read found it already ended"


# --- verify-by 8 and 12: delivery outcomes and the crash gap -------------------


@pytest.mark.parametrize(
    "outcome",
    [
        {"attempted": True, "ok": True, "exit_code": 0, "error": None, "command": "notify"},
        {"attempted": False, "ok": False, "exit_code": None, "error": "refused", "command": None},
        {"attempted": True, "ok": False, "exit_code": 3, "error": None, "command": "notify"},
        {
            "attempted": True,
            "ok": False,
            "exit_code": None,
            "error": "TimeoutExpired",
            "command": "notify",
        },
    ],
    ids=["delivered", "refused", "nonzero_exit", "timeout"],
)
def test_every_delivery_outcome_is_visible_and_none_of_them_changes_the_run(
    sandbox, monkeypatch, outcome
):
    """How the notice went is recorded; how the run came out does not move."""
    monkeypatch.setattr(_notify_hook, "deliver_terminal_notice", lambda *a, **k: outcome)
    _pid_absent(monkeypatch)
    rid = _stranded()

    st = jobs.status(rid)

    assert st["notify_delivery"] == outcome
    assert st["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert st["terminal"] is True


def test_a_delivery_that_comes_apart_keeps_the_attempt_unknown(sandbox, monkeypatch):
    """The fault is injected after the end is durable and before the notice.

    The transition and its no-attempt outcome are published together, then an
    attempted-with-unknown-outcome marker is written before delivery begins.
    An unexpected exception therefore cannot make a terminal run look like it
    never reached the notification path.
    """

    def _crash(*_a, **_k):
        raise RuntimeError("the notifier came apart in a way nobody classified")

    monkeypatch.setattr(_notify_hook, "deliver_terminal_notice", _crash)
    _pid_absent(monkeypatch)
    rid = _stranded()

    st = jobs.status(rid)

    assert st["terminal"] is True
    assert st["outcome"] == jobs.OUTCOME_INDETERMINATE
    expected = {
        "attempted": True,
        "ok": None,
        "exit_code": None,
        "error": "delivery_outcome_unknown",
        "command": None,
    }
    assert st["notify_delivery"] == expected

    on_disk = jobs._read_job(rid)
    assert on_disk["finished_at"] == st["finished_at"]
    assert on_disk["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER
    assert on_disk["notify_delivery"] == expected


def test_the_notice_is_the_one_the_run_configured(sandbox, monkeypatch):
    """The observer sends what the dead child would have sent, not its own idea.

    The fields come off the run's own record — its per-submit delivery override,
    its target and its sender — and go through the hook's resolution, so there
    is one place a notifier is configured rather than two that can disagree.
    """
    seen: dict[str, Any] = {}

    def _capture(run_id, job, status, **kw):
        seen.update({"run_id": run_id, "status": status, "label": (job or {}).get("label"), **kw})
        return {"attempted": True, "ok": True, "exit_code": 0, "error": None, "command": "x"}

    monkeypatch.setattr(_notify_hook, "deliver_terminal_notice", _capture)
    _pid_absent(monkeypatch)
    rid = _stranded(
        notify_command='["notify-me", "{status}"]',
        notify_target="seat",
        notify_sender="who",
    )

    jobs.status(rid)

    assert seen["run_id"] == rid
    assert seen["status"] == "exited"
    assert seen["label"] == "a-label"
    assert seen["command"] == '["notify-me", "{status}"]'
    assert seen["target"] == "seat"
    assert seen["sender"] == "who"


def test_a_run_that_named_a_recipient_with_no_command_ends_with_that_recorded(sandbox, monkeypatch):
    """The observer's notice honours the run's own request, not the environment.

    The record names a recipient and nothing anywhere says how to reach it, so
    the delivery outcome says so instead of reading like a run that never asked.
    The real delivery function runs; only the settings lookup is doubled.
    """
    from types import SimpleNamespace

    monkeypatch.delenv("LIONAGI_MCP_NOTIFY_COMMAND", raising=False)
    monkeypatch.setattr(
        "lionagi.state.lifecycle.notify_settings.resolve_notify_config",
        lambda **_kw: SimpleNamespace(reason=None, handler=None),
    )
    _pid_absent(monkeypatch)
    rid = _stranded(notify_target="recipient")

    delivery = jobs.status(rid)["notify_delivery"]

    assert delivery == {
        "attempted": False,
        "ok": False,
        "exit_code": None,
        "error": "no_delivery_command",
        "command": None,
    }
    assert jobs._read_job(rid)["notify_delivery"] == delivery


# --- verify-by 9: records written before this existed --------------------------


def test_a_record_written_before_any_of_this_is_reaped_on_its_next_read(
    sandbox, monkeypatch, no_delivery
):
    """No migration: a stranded record is closed by the next conclusive read.

    The record here carries none of the fields this change introduces, which is
    what every record on disk when it ships looks like. It receives the current
    observation time and the observer's attribution, and nothing about it is
    rewritten for being old.
    """
    _pid_absent(monkeypatch)
    rid = jobs.new_run_id()
    jobs._write_job(
        {
            "run_id": rid,
            "pid": 4242,
            "pid_create_time": _SPAWNED_AT,
            "kind": "agent",
            "status": "running",
            "spawn_state": "started",
            "submitted_at": "2026-01-01T00:00:00+00:00",
            "finished_at": None,
            "log": None,
        }
    )

    st = jobs.status(rid)

    assert st["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert st["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER
    assert st["submitted_at"] == "2026-01-01T00:00:00+00:00"
    assert len(no_delivery) == 1


# --- a mutation that cannot be serialized refuses, loudly ----------------------


def _lock_file_cannot_be_created(monkeypatch) -> None:
    """Make creating the per-run lock file fail, and only that."""
    real_open = jobs.os.open

    def _refuse(path, *args, **kwargs):
        if str(path).endswith(jobs._LOCK_NAME):
            raise PermissionError(13, "permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(jobs.os, "open", _refuse)


def _lock_cannot_be_acquired(monkeypatch) -> None:
    """Make the lock file open and the lock itself unobtainable."""

    def _refuse(fd):
        raise OSError(11, "resource temporarily unavailable")

    monkeypatch.setattr(jobs, "_lock_fd", _refuse)


def _console(rid: str) -> str:
    path = config.job_dir(rid) / "console.log"
    return path.read_text() if path.exists() else ""


@pytest.mark.parametrize(
    "break_the_lock",
    [_lock_file_cannot_be_created, _lock_cannot_be_acquired],
    ids=["lock_file_cannot_be_created", "lock_cannot_be_acquired"],
)
def test_a_terminal_write_that_cannot_be_serialized_sends_no_notice(
    sandbox, monkeypatch, no_delivery, break_the_lock
):
    """No lock, no terminal record, no notice — and the refusal is visible.

    The two ways the critical section is not entered are exercised separately
    because they fail at different lines and only one of them was ever
    classified. Either way the record must be left as it stands: a notice sent
    here would assert an end that every reader of the record contradicts, which
    is worse than the missing write it would be papering over, because the
    missing write is at least still missing on the next observation.
    """
    rid = _stranded()
    break_the_lock(monkeypatch)

    rc = _notify_hook.main(["--run-id", rid, "--status", "completed"])

    assert rc != 0
    assert no_delivery == []
    on_disk = jobs._read_job(rid)
    assert on_disk["finished_at"] is None
    assert on_disk["status"] == "running"
    assert "could not record the terminal status" in _console(rid)


def test_an_attempt_marker_that_cannot_be_serialized_sends_no_notice(
    sandbox, monkeypatch, no_delivery
):
    """No delivery starts unless its attempted state is already durable."""
    rid = _stranded()
    real_lock = jobs._lock_fd
    taken: list[int] = []

    def _refuse_after_the_first(fd):
        taken.append(fd)
        if len(taken) > 1:
            raise OSError(11, "resource temporarily unavailable")
        real_lock(fd)

    monkeypatch.setattr(jobs, "_lock_fd", _refuse_after_the_first)

    rc = _notify_hook.main(["--run-id", rid, "--status", "completed"])

    assert rc != 0
    assert no_delivery == []
    assert jobs._read_job(rid)["notify_delivery"] == {"attempted": False}
    assert "could not record the delivery attempt" in _console(rid)


def test_a_final_delivery_result_that_cannot_be_recorded_keeps_the_attempt(
    sandbox, monkeypatch, no_delivery
):
    """The notice went out against a durable end; its result did not land.

    A different case from the one above and reported the same way: the end is
    on disk, so the notice was owed and was attempted, and what is missing is
    the record of how it went. A hook that exits 0 here would say the run's
    completion signal is accounted for when nothing on disk accounts for it.
    """
    rid = _stranded()
    real_lock = jobs._lock_fd
    taken: list[int] = []

    def _refuse_after_the_second(fd):
        taken.append(fd)
        if len(taken) > 2:
            raise OSError(11, "resource temporarily unavailable")
        real_lock(fd)

    monkeypatch.setattr(jobs, "_lock_fd", _refuse_after_the_second)

    rc = _notify_hook.main(["--run-id", rid, "--status", "completed"])

    assert rc != 0
    assert no_delivery == [rid]
    assert jobs._read_job(rid)["notify_delivery"] == {
        "attempted": True,
        "ok": None,
        "exit_code": None,
        "error": "delivery_outcome_unknown",
        "command": None,
    }
    assert "could not record the delivery result" in _console(rid)


def test_an_absent_record_is_unchanged_by_any_of_this(sandbox, monkeypatch, no_delivery):
    """A run nobody submitted still resolves its notice and still exits 0.

    The refusal above is about a write that could not be attempted. A run with
    no record is a settled answer, not a refused write, and it keeps the
    behaviour it had: whatever is configured is resolved and the hook reports
    no failure of its own.
    """
    rc = _notify_hook.main(["--run-id", "no-such-run", "--status", "completed"])

    assert rc == 0
    assert no_delivery == ["no-such-run"]


def test_a_kill_that_cannot_be_recorded_says_so(sandbox, monkeypatch):
    """The signal went out and nothing durable says it did.

    The kill is not undone by the failure to record it — it already happened —
    so `killed` stays true and the code carries the part that did not. Reported
    rather than swallowed: a caller that reads a success and then finds the run
    still recorded as running has no way to tell which of the two to believe.
    """
    rid = _stranded()
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(jobs.os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)))
    _lock_cannot_be_acquired(monkeypatch)

    result = jobs._signal_group(rid, jobs._read_job(rid), 4242, 4242, 15, jobs.KILL_SIGNALLED)

    assert signalled == [(4242, 15)]
    assert result["killed"] is True
    assert result["reason_code"] == jobs.KILL_NOT_RECORDED
    assert jobs._read_job(rid)["finished_at"] is None


def test_a_recorded_kill_names_the_kill_as_what_ended_the_run(sandbox, monkeypatch):
    """The fifth writer of an end attributes it like the other four.

    No writer inside the serialization discipline may publish a terminal fact
    without saying what made it; an unattributed end is exactly what the
    attribution field exists to prevent.
    """
    rid = _stranded()
    monkeypatch.setattr(jobs.os, "killpg", lambda pgid, sig: None)

    result = jobs._signal_group(rid, jobs._read_job(rid), 4242, 4242, 15, jobs.KILL_SIGNALLED)

    assert result["reason_code"] == jobs.KILL_SIGNALLED
    on_disk = jobs._read_job(rid)
    assert on_disk["status"] == "killed"
    assert on_disk["terminal_source"] == jobs.TERMINAL_SOURCE_KILL


# --- reap reason-code split (docs/internals/mcp.md#reap-reason-code-split) ----


def test_reap_upgrades_the_reason_code_when_the_run_recorded_an_undelivered_notice(
    sandbox, monkeypatch, no_delivery
):
    """A run whose own directory says its terminal notice never arrived is not
    pure silence, and the reap it gets should say so.

    This is the shape `deliver_flow_notify_now` (`lionagi/cli/orchestrate/
    _notify.py`) and the pre-existing `record_notify_rejection_to_run` both
    leave behind for a run that lost its persistence: `notify_outcome.json`
    with `ok: false`.
    """
    _pid_absent(monkeypatch)
    rid = _stranded()

    run_dir = config.run_dir(rid)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "notify_outcome.json").write_text(
        json.dumps(
            {
                "ok": False,
                "exit_code": None,
                "stderr_path": None,
                "reason": "run_has_no_persisted_session_to_notify_on",
            }
        )
    )

    st = jobs.status(rid)

    assert st["terminal"] is True
    assert st["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert st["reason_code"] == jobs.LOST_REASON_NOTICE_RECORDED_UNDELIVERED


def test_reap_keeps_true_silence_when_nothing_was_recorded(sandbox, monkeypatch, no_delivery):
    """No file at all -- never wrote one, or retention pruned it -- reaps to
    the pre-existing reason code. Absence is never read as evidence either
    way; it is the same signal a genuinely silent run produces."""
    _pid_absent(monkeypatch)
    rid = _stranded()

    st = jobs.status(rid)

    assert st["reason_code"] == jobs.LOST_REASON


def test_reap_ignores_a_delivered_outcome_file(sandbox, monkeypatch, no_delivery):
    """`ok: true` never upgrades the reason code. Moot for a real MCP-spawned
    run (a delivered `--notify` already set `finished_at` via `mark_terminal`,
    disqualifying it from reaping before this file is even read) but must not
    misclassify a record that somehow reaches here anyway."""
    _pid_absent(monkeypatch)
    rid = _stranded()

    run_dir = config.run_dir(rid)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "notify_outcome.json").write_text(
        json.dumps({"ok": True, "exit_code": 0, "stderr_path": None})
    )

    st = jobs.status(rid)

    assert st["reason_code"] == jobs.LOST_REASON


def test_a_child_recorded_end_wins_over_a_later_reaper_observation(
    sandbox, monkeypatch, no_delivery
):
    """Ties the general hook-wins-over-reaper guarantee (see
    ``test_a_hook_end_that_lands_first_survives_the_reaper`` above) to the
    concrete mechanism: `--notify` for an MCP-spawned run always resolves to
    `lionhub.serve.mcp._notify_hook`, so calling it IS how a run records its own
    end, whichever CLI-side notify path triggered it. Ordering matters here
    specifically: the child's hook runs and wins the write BEFORE the reaper
    ever observes this run, and the reap that follows must decline rather
    than overwrite it with `indeterminate`.
    """
    _pid_absent(monkeypatch)
    rid = _stranded()

    terminal = _notify_hook.main(["--run-id", rid, "--status", "completed"])
    assert terminal == 0
    assert jobs._read_job(rid)["terminal_source"] == jobs.TERMINAL_SOURCE_HOOK
    assert len(no_delivery) == 1, "the hook's own delivery attempt"

    st = jobs.status(rid)

    assert st["status"] == "completed"
    assert st["terminal"] is True
    assert st["outcome"] == "succeeded"
    assert st["reason_code"] != jobs.LOST_REASON
    assert st["reason_code"] != jobs.LOST_REASON_NOTICE_RECORDED_UNDELIVERED
    assert st["terminal_source"] == jobs.TERMINAL_SOURCE_HOOK
    assert len(no_delivery) == 1, "the reaper must not attempt its own delivery over a recorded end"


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_a_non_finite_number_is_refused_from_caller_evidence(bad):
    """`Infinity` survives the trip in, so an isinstance check is not enough.

    `json.loads` accepts the bare tokens `NaN` and `Infinity`, so a caller can
    put a real non-finite float into `evidence=` without doing anything exotic.
    Stored, it comes back out through `json.dumps` as those same tokens, which
    are not JSON -- and Python's reader accepts them, so nothing in this process
    ever notices. The strict reader at the other end of the protocol is the one
    that cannot parse the reply, for every later read of that record.
    """
    with pytest.raises(ValueError):
        jobs._validate_reap_evidence({"recorded_create_time": bad})


def test_a_finite_number_is_still_accepted():
    """The must-match control: the check rejects non-finite, not numeric."""
    kept = jobs._validate_reap_evidence({"recorded_create_time": _SPAWNED_AT, "tolerance": 0.1})

    assert kept["recorded_create_time"] == _SPAWNED_AT
    assert kept["tolerance"] == 0.1


def test_a_non_finite_number_from_our_own_side_is_dropped_not_raised():
    """The two paths differ on purpose, and this is the difference.

    Caller evidence is refused out loud, because the caller can fix it. Evidence
    this module assembles itself is sanitized instead: a probe returning
    something unusable must not take down the reap it was gathered for, which is
    the whole reason the reap became reversible.
    """
    kept = jobs._sanitized_reap_evidence(
        {"recorded_create_time": float("inf"), "observed_create_time": _SPAWNED_AT}
    )

    assert "recorded_create_time" not in kept
    assert kept["observed_create_time"] == _SPAWNED_AT


def test_an_integer_past_float_range_is_dropped_rather_than_raised_from():
    """The finiteness check is a predicate over `float(value)`, not over *value*.

    So an integer larger than float range makes it raise instead of answer.
    Python's ints are unbounded and JSON's are too, which is how this arrives by
    the ordinary route: a 309-digit number is a valid document and `json.loads`
    hands it back as an `int`. Raising here is the exact failure the sanitizer
    exists to prevent, reached through the line that prevents the other one.
    """
    kept = jobs._sanitized_reap_evidence(
        {"recorded_create_time": 10**309, "observed_create_time": _SPAWNED_AT}
    )

    assert "recorded_create_time" not in kept
    assert kept["observed_create_time"] == _SPAWNED_AT


def test_an_integer_inside_float_range_is_still_kept():
    """The must-match control: what is refused is unrepresentable, not large."""
    kept = jobs._sanitized_reap_evidence({"recorded_create_time": 10**300})

    assert kept["recorded_create_time"] == 10**300


def test_caller_evidence_past_float_range_is_refused_as_a_value_error():
    """Refused, and refused as the kind of error this path promises.

    The caller path raises on purpose. But an `OverflowError` out of a validator
    whose documented answer is `ValueError` is not that refusal -- it is the
    same uncaught escape wearing a rejection's clothes, and a caller catching
    what the signature names would not catch it.
    """
    with pytest.raises(ValueError):
        jobs._validate_reap_evidence({"recorded_create_time": 10**309})


def test_a_persisted_start_time_past_float_range_does_not_take_down_a_read(
    sandbox, monkeypatch, no_delivery
):
    """The reachable arm, and why the three unit arms above do not cover it.

    An absent pid is conclusive before the recorded start time is consulted, so
    the identity check that already refuses this value -- it catches the same
    `OverflowError` around the same conversion, and answers `identity_unusable`
    -- never runs. The record goes straight into the evidence the reap is
    assembled from. That is the ordinary orphan case rather than an exotic one,
    which is what makes the read that reports the run the read that raises.
    """
    rid = _stranded(pid_create_time=10**309)
    _pid_absent(monkeypatch)

    st = jobs.status(rid)

    assert st["terminal"] is True
    assert st["terminal_evidence"]["finding"] == jobs.FINDING_PID_ABSENT
    # Dropped rather than stored: the field the record could not supply is
    # absent from the evidence, not present and unreadable.
    assert "recorded_create_time" not in st["terminal_evidence"]
    assert any(j["run_id"] == rid for j in jobs.list_jobs())
    json.dumps(st, allow_nan=False)


def test_what_survives_sanitizing_is_serializable_as_strict_json():
    """Assert the OUTCOME, not that nothing raised.

    The defect is not that a non-finite value is present, it is that the record
    stops being readable. So the assertion is the property that was lost:
    `allow_nan=False` is what a conforming reader does.
    """
    kept = jobs._sanitized_reap_evidence(
        {
            "recorded_create_time": float("nan"),
            "observed_create_time": float("inf"),
            "tolerance": 0.1,
            "observed_at": "2026-09-03T00:00:00Z",
        }
    )

    json.dumps(kept, allow_nan=False)


# --- reap reasons read from the run's own run.json ---------------------------

_UNDELIVERED = {"ok": False, "exit_code": None, "stderr_path": None, "reason": "no session"}
_DEGRADED_REASON = "OperationalError('database is locked')"
_LOST = {"status": "completed", "reason_code": RunReasons.COMPLETED_OK, "at": 1_700_000_100.0}
_SIGNAL_DEGRADED_AT = 1_700_000_050.0


def _run_json(rid: str, **fields: Any) -> None:
    config.run_dir(rid).mkdir(parents=True, exist_ok=True)
    config.run_manifest(rid).write_text(json.dumps({"run_id": rid, "status": "running", **fields}))


def _run_json_bytes(rid: str, raw: bytes) -> None:
    config.run_dir(rid).mkdir(parents=True, exist_ok=True)
    config.run_manifest(rid).write_bytes(raw)


def _leave(
    rid: str, *, notice: bool, degraded: bool, lost: bool = False, signal_degraded: bool = False
) -> None:
    fields: dict[str, Any] = {}
    if degraded:
        fields["persistence_degraded_reason"] = _DEGRADED_REASON
    if lost:
        fields["terminal_write_lost"] = _LOST
    if signal_degraded:
        fields["signal_persistence_degraded_at"] = _SIGNAL_DEGRADED_AT
    _run_json(rid, **fields)
    if notice:
        config.run_dir(rid).joinpath("notify_outcome.json").write_text(json.dumps(_UNDELIVERED))


def _expected_reason(*, notice: bool, **_: bool) -> str:
    # An undelivered notice is the pre-existing reason split, and it outranks the
    # detail-only case; every finer cause stays on the main reason code with
    # its name in reason_detail.
    if notice:
        return jobs.LOST_REASON_NOTICE_RECORDED_UNDELIVERED
    return jobs.LOST_REASON


def _expected_detail(
    *, notice: bool, degraded: bool, lost: bool = False, signal_degraded: bool = False
) -> str | None:
    # Beside silence only, in order: a lost terminal write, persistence turned off,
    # signal persistence that stopped waiting on a refusing store.
    if notice:
        return None
    if lost:
        return jobs.REASON_DETAIL_TERMINAL_WRITE_LOST
    if degraded:
        return jobs.REASON_DETAIL_PERSISTENCE_DEGRADED
    if signal_degraded:
        return jobs.REASON_DETAIL_SIGNAL_PERSISTENCE_DEGRADED
    return None


_RECORDED = [
    (notice, degraded, lost, signal_degraded)
    for notice in (True, False)
    for degraded in (True, False)
    for lost in (True, False)
    for signal_degraded in (True, False)
]


@pytest.mark.parametrize(("notice", "degraded", "lost", "signal_degraded"), _RECORDED)
def test_the_reason_code_follows_what_the_run_recorded_in_order(
    sandbox, monkeypatch, no_delivery, notice, degraded, lost, signal_degraded
):
    """An undelivered notice, then silence, for the code; beside silence, a lost
    terminal write, then persistence turned off, then signal persistence that
    stopped waiting, for the detail. A present record wins over every later one
    and an absent one falls through. The outcome stays indeterminate, and the
    status a lost write names rides in the evidence whatever the code."""
    _pid_absent(monkeypatch)
    rid = _stranded()
    recorded = {
        "notice": notice,
        "degraded": degraded,
        "lost": lost,
        "signal_degraded": signal_degraded,
    }
    _leave(rid, **recorded)

    st = jobs.status(rid)

    assert st["terminal"] is True
    assert st["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert st["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER
    assert st["reason_code"] == _expected_reason(**recorded)
    assert st["reason_detail"] == _expected_detail(**recorded)
    assert jobs._read_job(rid)["reason_code"] == st["reason_code"]
    if lost:
        assert st["terminal_evidence"]["decided_status"] == "completed"
    else:
        assert "decided_status" not in st["terminal_evidence"]


def test_the_reason_detail_is_distinct_from_every_reason_code():
    details = {
        jobs.REASON_DETAIL_PERSISTENCE_DEGRADED,
        jobs.REASON_DETAIL_TERMINAL_WRITE_LOST,
        jobs.REASON_DETAIL_SIGNAL_PERSISTENCE_DEGRADED,
    }
    assert jobs.REASON_DETAIL_PERSISTENCE_DEGRADED == "persistence_degraded"
    assert jobs.REASON_DETAIL_TERMINAL_WRITE_LOST == "terminal_write_lost"
    assert jobs.REASON_DETAIL_SIGNAL_PERSISTENCE_DEGRADED == "signal_persistence_degraded"
    assert len(details) == 3
    assert not details & {jobs.LOST_REASON, jobs.LOST_REASON_NOTICE_RECORDED_UNDELIVERED}


def test_degraded_persistence_keeps_the_existing_reason_code(sandbox, monkeypatch, no_delivery):
    """The finer cause belongs beside the reason, not in place of it."""
    _pid_absent(monkeypatch)
    rid = _stranded()
    _run_json(rid, persistence_degraded_reason=_DEGRADED_REASON)

    st = jobs.status(rid)

    assert st["reason_code"] == jobs.LOST_REASON
    assert st["reason_detail"] == jobs.REASON_DETAIL_PERSISTENCE_DEGRADED


def test_the_reason_detail_rides_the_notice_payload_without_replacing_the_code(
    sandbox, monkeypatch, no_delivery
):
    seen: list[dict[str, Any]] = []

    def fake(run_id, job, status, **kw):
        seen.append(kw)
        return {"attempted": True, "ok": True, "exit_code": 0, "error": None, "command": "notify"}

    monkeypatch.setattr(_notify_hook, "deliver_terminal_notice", fake)
    _pid_absent(monkeypatch)
    rid = _stranded()
    _run_json(rid, persistence_degraded_reason=_DEGRADED_REASON)

    jobs.status(rid)

    assert seen[0]["reason_code"] == jobs.LOST_REASON
    assert seen[0]["reason_detail"] == jobs.REASON_DETAIL_PERSISTENCE_DEGRADED


def test_the_reason_detail_is_visible_on_status_and_the_listing(sandbox, monkeypatch, no_delivery):
    _pid_absent(monkeypatch)
    rid = _stranded()
    _run_json(rid, persistence_degraded_reason=_DEGRADED_REASON)

    row = next(item for item in jobs.list_jobs() if item["run_id"] == rid)

    assert row["reason_code"] == jobs.LOST_REASON
    assert row["reason_detail"] == jobs.REASON_DETAIL_PERSISTENCE_DEGRADED


@pytest.mark.parametrize(("notice", "degraded", "lost", "signal_degraded"), _RECORDED)
def test_the_reaper_notice_carries_the_reason_code(
    sandbox, monkeypatch, notice, degraded, lost, signal_degraded
):
    seen: list[dict[str, Any]] = []

    def fake(run_id, job, status, **kw):
        seen.append(kw)
        return {"attempted": True, "ok": True, "exit_code": 0, "error": None, "command": "notify"}

    monkeypatch.setattr(_notify_hook, "deliver_terminal_notice", fake)
    _pid_absent(monkeypatch)
    rid = _stranded()
    recorded = {
        "notice": notice,
        "degraded": degraded,
        "lost": lost,
        "signal_degraded": signal_degraded,
    }
    _leave(rid, **recorded)

    jobs.status(rid)

    assert [(kw["reason_code"], kw["reason_detail"]) for kw in seen] == [
        (_expected_reason(**recorded), _expected_detail(**recorded))
    ]


@pytest.mark.parametrize(
    "reason",
    ["", True, None, 5, ["locked"], {"reason": "locked"}],
    ids=["empty", "bool", "null", "number", "list", "object"],
)
def test_a_malformed_degraded_reason_assigns_nothing(sandbox, monkeypatch, no_delivery, reason):
    _pid_absent(monkeypatch)
    rid = _stranded()
    _run_json(rid, persistence_degraded_reason=reason)

    assert jobs.status(rid)["reason_code"] == jobs.LOST_REASON
    assert jobs.status(rid)["reason_detail"] is None


def test_a_run_json_naming_another_run_assigns_nothing(sandbox, monkeypatch, no_delivery):
    """A run.json is the run's own only when it names the run it sits under."""
    _pid_absent(monkeypatch)
    rid = _stranded()
    _run_json_bytes(
        rid,
        json.dumps(
            {"run_id": "another-run", "persistence_degraded_reason": _DEGRADED_REASON}
        ).encode(),
    )

    assert jobs.status(rid)["reason_code"] == jobs.LOST_REASON
    assert jobs.status(rid)["reason_detail"] is None


@pytest.mark.parametrize(
    "lost",
    [
        "completed",
        ["completed"],
        {},
        {"status": "completed"},
        {"status": "running", "reason_code": "x", "at": 1.0},
        {"status": "bogus", "reason_code": "x", "at": 1.0},
        {"status": 5, "reason_code": "x", "at": 1.0},
        {"status": ["completed"], "reason_code": "x", "at": 1.0},
        {"status": "completed", "reason_code": 5, "at": 1.0},
        {"status": "completed", "reason_code": "", "at": 1.0},
        {"status": "completed", "reason_code": "x" * 257, "at": 1.0},
        {"status": "completed", "reason_code": "x", "at": "yesterday"},
        {"status": "completed", "reason_code": "x", "at": True},
        {"status": "completed", "reason_code": "x", "at": None},
        {"status": "completed", "reason_code": "x", "at": float("nan")},
    ],
    ids=[
        "string",
        "list",
        "empty-object",
        "status-only",
        "status-running",
        "status-unknown-word",
        "status-int",
        "status-list",
        "code-int",
        "code-empty",
        "code-too-long",
        "at-string",
        "at-bool",
        "at-null",
        "at-nan",
    ],
)
def test_a_malformed_terminal_write_lost_record_assigns_nothing(
    sandbox, monkeypatch, no_delivery, lost
):
    _pid_absent(monkeypatch)
    rid = _stranded()
    _run_json(rid, terminal_write_lost=lost)
    # the same record well formed names the cause, so the reader is there to refuse this one
    control = _stranded()
    _run_json(control, terminal_write_lost=_LOST)

    st = jobs.status(rid)

    assert st["reason_code"] == jobs.LOST_REASON
    assert st["reason_detail"] is None
    assert st["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert "decided_status" not in st["terminal_evidence"]
    assert jobs.status(control)["reason_detail"] == jobs.REASON_DETAIL_TERMINAL_WRITE_LOST


@pytest.mark.parametrize(
    "at",
    ["yesterday", True, None, [1.0], {"at": 1.0}, float("nan"), float("inf")],
    ids=["string", "bool", "null", "list", "object", "nan", "inf"],
)
def test_a_malformed_signal_degraded_time_assigns_nothing(sandbox, monkeypatch, no_delivery, at):
    _pid_absent(monkeypatch)
    rid = _stranded()
    _run_json(rid, signal_persistence_degraded_at=at)
    # a well-formed time names the cause, so the reader is there to refuse this one
    control = _stranded()
    _run_json(control, signal_persistence_degraded_at=_SIGNAL_DEGRADED_AT)

    st = jobs.status(rid)

    assert st["reason_code"] == jobs.LOST_REASON
    assert st["reason_detail"] is None
    assert jobs.status(control)["reason_detail"] == jobs.REASON_DETAIL_SIGNAL_PERSISTENCE_DEGRADED


def test_a_malformed_lost_write_does_not_hide_a_later_well_formed_record(
    sandbox, monkeypatch, no_delivery
):
    _pid_absent(monkeypatch)
    rid = _stranded()
    _run_json(
        rid,
        terminal_write_lost={"status": "bogus", "reason_code": "x", "at": 1.0},
        signal_persistence_degraded_at=_SIGNAL_DEGRADED_AT,
    )

    st = jobs.status(rid)

    assert st["reason_code"] == jobs.LOST_REASON
    assert st["reason_detail"] == jobs.REASON_DETAIL_SIGNAL_PERSISTENCE_DEGRADED
    assert "decided_status" not in st["terminal_evidence"]


def _padded_run_json(rid: str, size: int) -> bytes:
    """*rid*'s run.json of exactly *size* bytes, recording degraded persistence."""
    payload = {"run_id": rid, "persistence_degraded_reason": _DEGRADED_REASON, "pad": ""}
    base = len(json.dumps(payload).encode())
    payload["pad"] = "x" * (size - base)
    raw = json.dumps(payload).encode()
    assert len(raw) == size
    return raw


def test_a_run_json_at_the_size_cap_is_read_and_one_byte_over_it_is_not(
    sandbox, monkeypatch, no_delivery
):
    _pid_absent(monkeypatch)
    at_cap = _stranded()
    _run_json_bytes(at_cap, _padded_run_json(at_cap, jobs._MAX_RUN_MANIFEST_BYTES))
    over_cap = _stranded()
    _run_json_bytes(over_cap, _padded_run_json(over_cap, jobs._MAX_RUN_MANIFEST_BYTES) + b"\n")

    assert jobs.status(at_cap)["reason_code"] == jobs.LOST_REASON
    assert jobs.status(at_cap)["reason_detail"] == jobs.REASON_DETAIL_PERSISTENCE_DEGRADED
    assert jobs.status(over_cap)["reason_code"] == jobs.LOST_REASON
    assert jobs.status(over_cap)["reason_detail"] is None


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"\xff\xfe\x00",
        b'["persistence_degraded_reason"]',
        b'"persistence_degraded_reason"',
        b"null",
        b"7",
        b"[" * 200_000,
    ],
    ids=["empty", "text", "not-utf8", "list", "string", "null", "number", "deeply-nested"],
)
def test_a_run_json_that_is_not_an_object_assigns_nothing(sandbox, monkeypatch, no_delivery, raw):
    _pid_absent(monkeypatch)
    rid = _stranded()
    _run_json_bytes(rid, raw)

    st = jobs.status(rid)

    assert st["reason_code"] == jobs.LOST_REASON
    assert st["reason_detail"] is None
    assert st["outcome"] == jobs.OUTCOME_INDETERMINATE


def test_a_missing_run_json_assigns_nothing(sandbox, monkeypatch, no_delivery):
    _pid_absent(monkeypatch)
    rid = _stranded()

    st = jobs.status(rid)

    assert st["reason_code"] == jobs.LOST_REASON
    assert st["reason_detail"] is None
