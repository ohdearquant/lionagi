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

from lionagi.mcp import _notify_hook, config, jobs

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
    assert first["terminal_evidence"] == {
        "kind": jobs.EVIDENCE_PROCESS_GONE,
        "finding": finding,
    }

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


def test_a_hook_arriving_after_a_reap_keeps_the_recorded_end(sandbox, monkeypatch, no_delivery):
    """The child's own hook, running late, cannot replace an inferred end.

    Whichever end was recorded first is the one every reader has already been
    given, so it stays. The hook's arrival is still allowed to add what is
    missing beside it — which is what makes this a merge rule rather than a
    refusal to write.
    """
    _pid_absent(monkeypatch)
    rid = _stranded()
    reaped = jobs.status(rid)

    kept = jobs.mark_terminal(rid, "completed").record
    assert kept["finished_at"] == reaped["finished_at"]

    st = jobs.status(rid)
    assert st["status"] == "exited"
    assert st["outcome"] == jobs.OUTCOME_INDETERMINATE
    assert st["terminal_source"] == jobs.TERMINAL_SOURCE_ORPHAN_REAPER


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
    `lionagi.mcp._notify_hook`, so calling it IS how a run records its own
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
