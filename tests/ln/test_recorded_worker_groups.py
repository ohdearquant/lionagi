# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Recorded worker evidence never substitutes liveness for process identity."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import psutil
import pytest

from lionagi.ln import _proc


class _ProcessReads:
    def __init__(self):
        self.births = {5001: ("found", 10.0), 5002: ("found", 11.0)}
        self.groups = {5001: 5001, 5002: 5001}
        self.members = [(5001, 10.0), (5002, 11.0)]
        self.complete = True
        self.host = "worker-host"
        self.boot = 100.0
        self.own_group = 9000
        self.after_census = None
        self.after_group = None
        self.probe_outcome = "forbidden"
        self.events = []
        self.invalid_calls = []

    def birth(self, pid):
        self.events.append(("birth", pid))
        value = self.births.get(pid, ("gone", None))
        if isinstance(value, Exception):
            raise value
        return value

    def recorded_birth(self, pid, *, expected_created=None):
        # The production observer uses the expected birth to preserve a known
        # mismatch; the subject still independently checks the returned value.
        return self.birth(pid)

    def forbid_process(self, pid):
        self.invalid_calls.append(("process", pid))
        raise AssertionError("the fixture does not inspect operating-system processes")

    def group(self, pid):
        self.events.append(("group", pid))
        value = self.groups.get(pid, ProcessLookupError())
        if isinstance(value, Exception):
            raise value
        if self.after_group is not None:
            self.after_group(pid)
        return value

    def census(self, pgid):
        self.events.append(("census", pgid))
        result = (list(self.members), self.complete)
        if self.after_census is not None:
            self.after_census()
        return result

    def hostname(self):
        if isinstance(self.host, Exception):
            raise self.host
        return self.host

    def boot_time(self):
        if isinstance(self.boot, Exception):
            raise self.boot
        return self.boot

    def caller_group(self):
        if isinstance(self.own_group, Exception):
            raise self.own_group
        return self.own_group

    def probe(self, pgid, sig):
        self.events.append(("probe", pgid, sig))
        if pgid != 5001 or sig != 0 or self.probe_outcome == "forbidden":
            self.invalid_calls.append((pgid, sig))
            raise AssertionError("unexpected process-group operation")
        if isinstance(self.probe_outcome, Exception):
            raise self.probe_outcome

    def forbid_population(self):
        self.invalid_calls.append("population")
        raise AssertionError("the unit fixture does not enumerate operating-system processes")


@pytest.fixture(autouse=True)
def reads(monkeypatch):
    model = _ProcessReads()
    monkeypatch.setattr(_proc, "process_create_time", model.birth)
    monkeypatch.setattr(_proc, "_recorded_process_create_time", model.recorded_birth)
    monkeypatch.setattr(_proc, "_group_member_identities", model.census)
    monkeypatch.setattr(_proc.socket, "gethostname", model.hostname)
    monkeypatch.setattr(psutil, "boot_time", model.boot_time)
    monkeypatch.setattr(psutil, "pids", model.forbid_population)
    monkeypatch.setattr(psutil, "Process", model.forbid_process)
    monkeypatch.setattr(_proc.os, "getpgid", model.group, raising=False)
    monkeypatch.setattr(_proc.os, "getpgrp", model.caller_group, raising=False)
    monkeypatch.setattr(_proc.os, "killpg", model.probe, raising=False)
    yield model
    assert model.invalid_calls == []


def _identity(**changes):
    return replace(
        _proc._RecordedWorkerIdentity(
            pid=5001,
            pgid=5001,
            pid_create_time=10.0,
            pid_host="worker-host",
            pid_boot_time=100.0,
            process_identity_mode="local",
        ),
        **changes,
    )


def _admit(identity=None, *, tolerance=5.0):
    return _proc._admit_recorded_worker_group(
        _identity() if identity is None else identity,
        boot_time_tolerance=tolerance,
    )


def _witness():
    result = _admit()
    assert isinstance(result, _proc._RecordedGroupWitness)
    return result


def test_recorded_evidence_values_are_frozen():
    identity = _identity()
    witness = _witness()
    refusal = _proc._RecordedGroupRefusal("identity_mismatch")
    for value, field, replacement in (
        (identity, "pid", 5003),
        (witness, "members", ((5003, 12.0),)),
        (refusal, "reason", "identity_unverifiable"),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, replacement)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"pid_host": "another-host"}, "host_mismatch"),
        ({"pid_boot_time": 106.0}, "boot_mismatch"),
        ({"process_identity_mode": "foreign"}, "foreign_mode"),
        ({"process_identity_mode": None}, "foreign_mode"),
        ({"pid": 1, "pgid": 1}, "unsafe_group"),
        ({"pid": True, "pgid": True}, "unsafe_group"),
        ({"pgid": 5002}, "unsafe_group"),
        ({"pid": 9000, "pgid": 9000}, "unsafe_group"),
        ({"pid_create_time": None}, "identity_unverifiable"),
        ({"pid_create_time": float("nan")}, "identity_unverifiable"),
        ({"pid_create_time": float("inf")}, "identity_unverifiable"),
        ({"pid_create_time": 10**1000}, "identity_unverifiable"),
        ({"pid_create_time": True}, "identity_unverifiable"),
        ({"pid_boot_time": float("nan")}, "identity_unverifiable"),
        ({"pid_host": " "}, "identity_unverifiable"),
    ],
)
def test_foreign_or_malformed_evidence_is_refused_before_a_process_read(reads, changes, reason):
    result = _admit(_identity(**changes))

    assert result == _proc._RecordedGroupRefusal(reason)
    assert reads.events == []


@pytest.mark.parametrize("field", ["host", "boot", "own_group"])
def test_runtime_read_failure_is_not_an_identity_mismatch(reads, field):
    setattr(reads, field, PermissionError())

    assert _admit() == _proc._RecordedGroupRefusal("identity_unverifiable")
    assert reads.events == []


@pytest.mark.parametrize("tolerance", [None, -1.0, float("nan"), float("inf"), True])
def test_an_invalid_boot_tolerance_cannot_admit_a_group(reads, tolerance):
    assert _admit(tolerance=tolerance) == _proc._RecordedGroupRefusal("identity_unverifiable")
    assert reads.events == []


@pytest.mark.parametrize(("tolerance", "admitted"), [(4.0, False), (5.0, True), (6.0, True)])
def test_boot_comparison_uses_the_callers_tolerance(reads, tolerance, admitted):
    result = _admit(_identity(pid_boot_time=105.0), tolerance=tolerance)

    if admitted:
        assert isinstance(result, _proc._RecordedGroupWitness)
    else:
        assert result == _proc._RecordedGroupRefusal("boot_mismatch")
        assert reads.events == []


def test_a_platform_without_killpg_refuses_recorded_cleanup(reads, monkeypatch):
    monkeypatch.delattr(_proc.os, "killpg")

    assert _admit() == _proc._RecordedGroupRefusal("unsupported_platform")
    assert _proc._recorded_worker_group_state(_identity()) == "unreadable"
    assert reads.events == []


@pytest.mark.parametrize(
    ("birth", "group", "reason"),
    [
        (("gone", None), 5001, "leader_not_live"),
        (("unknown", None), 5001, "identity_unverifiable"),
        (("found", 20.0), 5001, "identity_mismatch"),
        (("found", 10.0), 6000, "identity_mismatch"),
        (("found", 10.0), PermissionError(), "identity_unverifiable"),
    ],
)
def test_initial_leader_failures_keep_mismatch_and_unreadable_distinct(reads, birth, group, reason):
    reads.births[5001] = birth
    reads.groups[5001] = group

    assert _admit() == _proc._RecordedGroupRefusal(reason)
    assert ("census", 5001) not in reads.events


@pytest.mark.parametrize(
    ("birth", "group", "reason"),
    [
        (("gone", None), 5001, "leader_not_live"),
        (("unknown", None), 5001, "identity_unverifiable"),
        (("found", 20.0), 5001, "identity_mismatch"),
        (("found", 10.0), 6000, "identity_mismatch"),
        (("found", 10.0), PermissionError(), "identity_unverifiable"),
    ],
)
def test_a_changed_or_unreadable_second_leader_anchor_refuses_the_census(
    reads, birth, group, reason
):
    def change_leader():
        reads.births[5001] = birth
        reads.groups[5001] = group

    reads.after_census = change_leader

    assert _admit() == _proc._RecordedGroupRefusal(reason)
    assert reads.events.count(("census", 5001)) == 1


def test_two_live_leader_anchors_must_be_exact_not_merely_within_record_tolerance(reads):
    reads.births[5001] = ("found", 10.02)

    def change_leader():
        reads.births[5001] = ("found", 10.04)

    reads.after_census = change_leader

    assert _admit() == _proc._RecordedGroupRefusal("identity_mismatch")


def test_each_leader_observation_brackets_its_group_read(reads):
    def change_birth(pid):
        if pid == 5001:
            reads.births[pid] = ("found", 20.0)

    reads.after_group = change_birth

    assert _admit() == _proc._RecordedGroupRefusal("identity_mismatch")
    assert ("census", 5001) not in reads.events


def test_a_known_birth_mismatch_cannot_become_leader_not_live_after_a_later_read(reads):
    reads.births[5001] = ("found", 20.0)
    reads.groups[5001] = ProcessLookupError()

    assert _admit() == _proc._RecordedGroupRefusal("identity_mismatch")
    assert reads.events == [("birth", 5001)]


def test_the_live_leader_and_members_are_frozen_between_the_anchors(reads):
    reads.members = [(5002, 11.0)]
    witness = _witness()

    assert witness.identity == _identity()
    assert witness.leader == (5001, 10.0)
    assert witness.members == ((5001, 10.0), (5002, 11.0))
    census = reads.events.index(("census", 5001))
    assert ("birth", 5001) in reads.events[:census]
    assert ("birth", 5001) in reads.events[census + 1 :]


def test_a_partial_census_keeps_a_known_survivor_after_the_leader_exits(reads):
    reads.complete = False
    reads.members = [(5002, 11.0)]
    witness = _witness()
    reads.births[5001] = ("gone", None)

    assert witness.members == ((5001, 10.0), (5002, 11.0))
    assert _proc._recorded_worker_group_matches(witness) == "matches"
    assert reads.events.count(("census", 5001)) == 1


def test_an_unreadable_census_keeps_only_its_live_leader_and_never_a_numeric_anchor(reads):
    reads.complete = False
    reads.members = []
    witness = _witness()

    assert witness.members == ((5001, 10.0),)
    assert _proc._recorded_worker_group_matches(witness) == "matches"
    reads.births[5001] = ("gone", None)
    assert _proc._recorded_worker_group_matches(witness) == "missing"
    assert not any(event[0] == "probe" for event in reads.events)


def test_a_failed_census_still_requires_both_live_leader_anchors(reads, monkeypatch):
    def unavailable(pgid):
        reads.events.append(("census", pgid))
        raise PermissionError()

    monkeypatch.setattr(_proc, "_group_member_identities", unavailable)

    witness = _witness()

    assert witness.members == ((5001, 10.0),)
    assert reads.events.count(("birth", 5001)) == 4
    reads.births[5001] = ("gone", None)
    assert _proc._recorded_worker_group_matches(witness) == "missing"


def test_a_new_member_cannot_replace_every_frozen_witness(reads):
    reads.members = []
    witness = _witness()
    reads.births[5001] = ("gone", None)
    reads.births[5003] = ("found", 12.0)
    reads.groups[5003] = 5001
    reads.members = [(5003, 12.0)]

    assert _proc._recorded_worker_group_matches(witness) == "missing"
    assert reads.events.count(("census", 5001)) == 1
    assert ("birth", 5003) not in reads.events


def test_a_reused_frozen_survivor_pid_cannot_authorize_the_group(reads):
    witness = _witness()
    reads.births[5001] = ("gone", None)
    reads.births[5002] = ("found", 21.0)

    assert _proc._recorded_worker_group_matches(witness) == "missing"
    assert not any(event[0] == "probe" for event in reads.events)


def test_frozen_member_revalidation_does_not_reuse_record_birth_tolerance(reads):
    reads.members = []
    witness = _witness()
    reads.births[5001] = ("found", 10.02)

    assert _proc._recorded_worker_group_matches(witness) == "missing"


def test_an_unreadable_frozen_member_is_distinct_from_every_member_missing(reads):
    witness = _witness()
    reads.births[5001] = ("gone", None)
    reads.births[5002] = ("unknown", None)

    assert _proc._recorded_worker_group_matches(witness) == "unreadable"


def test_one_matching_frozen_survivor_is_enough_despite_another_unreadable_member(reads):
    witness = _witness()
    reads.births[5001] = ("unknown", None)

    assert _proc._recorded_worker_group_matches(witness) == "matches"


@pytest.mark.parametrize(
    ("birth", "expected"),
    [
        (("found", 10.0), "present"),
        (("found", 20.0), "unreadable"),
        (("unknown", None), "unreadable"),
        (("found", float("nan")), "unreadable"),
        (PermissionError(), "unreadable"),
    ],
)
def test_absence_requires_the_original_leader_not_live_before_any_kernel_probe(
    reads, birth, expected
):
    reads.births[5001] = birth

    assert _proc._recorded_worker_group_state(_identity()) == expected
    assert not any(event[0] == "probe" for event in reads.events)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (ProcessLookupError(), "empty"),
        (None, "present"),
        (PermissionError(), "unreadable"),
        (OSError(), "unreadable"),
        (RuntimeError(), "unreadable"),
    ],
    ids=["kernel-empty", "kernel-present", "permission-denied", "probe-error", "read-failure"],
)
def test_kernel_absence_has_three_distinct_outcomes(reads, outcome, expected):
    reads.births[5001] = ("gone", None)
    reads.probe_outcome = outcome
    reads.members = []

    assert _proc._recorded_worker_group_state(_identity()) == expected
    assert reads.events == [("birth", 5001), ("probe", 5001, 0)]


def test_a_zombie_only_group_is_not_empty_when_the_kernel_still_reports_it(reads):
    # The original leader is a matching zombie; the kernel still owns absence.
    reads.births[5001] = ("zombie", 10.0)
    reads.members = []
    reads.complete = True
    reads.probe_outcome = None

    assert _proc._recorded_worker_group_state(_identity()) == "present"
    assert ("census", 5001) not in reads.events


def test_kernel_absence_does_not_depend_on_population_scan_results(reads):
    reads.births[5001] = ("gone", None)
    reads.members = [(5002, 11.0)]
    reads.complete = False
    reads.probe_outcome = ProcessLookupError()

    assert _proc._recorded_worker_group_state(_identity()) == "empty"
    assert ("census", 5001) not in reads.events


_real_process_create_time_for_zombie_tests = _proc._recorded_process_create_time


def _use_fake_zombie(monkeypatch, *, birth):
    """Use the recorded process reader with a fake zombie and fake birth reader."""
    observed_pids = []

    class FakeZombie:
        def status(self):
            return psutil.STATUS_ZOMBIE

    zombie = FakeZombie()

    def fake_process(pid):
        observed_pids.append(pid)
        assert pid == 5001, "a process outside the fixture was queried"
        return zombie

    def fake_raw_create_time(proc):
        assert proc is zombie
        return birth

    monkeypatch.setattr(psutil, "Process", fake_process)
    monkeypatch.setattr(_proc, "raw_create_time", fake_raw_create_time)
    monkeypatch.setattr(
        _proc, "_recorded_process_create_time", _real_process_create_time_for_zombie_tests
    )
    return observed_pids


def test_reused_zombie_birth_mismatch_refuses_recorded_group_admission(reads, monkeypatch):
    # A new zombie has reused the old leader's PID but belongs to another
    # process group. Its readable birth does not match the recorded leader.
    reads.groups[5001] = 7654
    reads.probe_outcome = ProcessLookupError()
    observed_pids = _use_fake_zombie(monkeypatch, birth=20.0)

    assert _admit() == _proc._RecordedGroupRefusal("identity_mismatch")
    assert observed_pids and set(observed_pids) == {5001}
    assert not any(event[0] in ("census", "probe") for event in reads.events)


def test_reused_zombie_birth_mismatch_cannot_confirm_group_empty(reads, monkeypatch):
    # The witness was captured while the original leader lived. The PID is
    # then reused by a zombie before the final settlement observation.
    witness = _witness()
    reads.groups[5001] = 7654
    reads.probe_outcome = ProcessLookupError()
    observed_pids = _use_fake_zombie(monkeypatch, birth=20.0)

    assert _proc._recorded_worker_group_state(witness.identity) == "unreadable"
    assert observed_pids and set(observed_pids) == {5001}
    assert not any(event[0] == "probe" and event[2] != 0 for event in reads.events)


def test_matched_zombie_with_kernel_group_present_remains_unsettled(reads, monkeypatch):
    # Control: a matching zombie in a kernel-present group is not empty.
    reads.probe_outcome = None
    observed_pids = _use_fake_zombie(monkeypatch, birth=10.0)

    assert _proc._recorded_worker_group_state(_identity()) == "present"
    assert observed_pids and set(observed_pids) == {5001}
    assert ("probe", 5001, 0) in reads.events


class _FakeRecordedProcess:
    def __init__(self, birth, *, status=psutil.STATUS_ZOMBIE):
        self.birth = birth
        self.recorded_status = status

    def status(self):
        if isinstance(self.recorded_status, Exception):
            raise self.recorded_status
        return self.recorded_status


def _use_recorded_process_sequence(monkeypatch, *observations):
    """Return only ordered fake handles; no real PID or process is queried."""
    observations = iter(observations)
    seen = []

    def process(pid):
        assert pid == 5001, "a process outside the fixture was queried"
        seen.append(pid)
        value = next(observations)
        if isinstance(value, Exception):
            raise value
        return value

    def birth(handle):
        if isinstance(handle.birth, Exception):
            raise handle.birth
        return handle.birth

    monkeypatch.setattr(psutil, "Process", process)
    monkeypatch.setattr(_proc, "raw_create_time", birth)
    monkeypatch.setattr(
        _proc, "_recorded_process_create_time", _real_process_create_time_for_zombie_tests
    )
    return seen


@pytest.mark.parametrize(
    "birth_error",
    [psutil.ZombieProcess(5001), psutil.AccessDenied(5001), psutil.NoSuchProcess(5001)],
    ids=["zombie", "access-denied", "reaped-after-handle"],
)
def test_unreadable_birth_after_handle_never_confirms_empty(reads, monkeypatch, birth_error):
    reads.probe_outcome = ProcessLookupError()
    seen = _use_recorded_process_sequence(monkeypatch, _FakeRecordedProcess(birth_error))

    assert _proc._recorded_worker_group_state(_identity()) == "unreadable"
    assert seen == [5001]
    assert not any(event[0] == "probe" for event in reads.events)


def test_zombie_exception_is_not_treated_as_an_absent_pid(reads, monkeypatch):
    reads.probe_outcome = ProcessLookupError()
    seen = _use_recorded_process_sequence(monkeypatch, psutil.ZombieProcess(5001))

    assert _proc._recorded_worker_group_state(_identity()) == "unreadable"
    assert seen == [5001]
    assert not any(event[0] == "probe" for event in reads.events)


def test_initial_absence_can_be_confirmed_by_kernel(reads, monkeypatch):
    reads.probe_outcome = ProcessLookupError()
    seen = _use_recorded_process_sequence(monkeypatch, psutil.NoSuchProcess(5001))

    assert _proc._recorded_worker_group_state(_identity()) == "empty"
    assert seen == [5001]
    assert ("probe", 5001, 0) in reads.events


def test_zombie_reaped_before_second_birth_read_fails_closed(reads, monkeypatch):
    reads.probe_outcome = ProcessLookupError()
    seen = _use_recorded_process_sequence(
        monkeypatch, _FakeRecordedProcess(10.0), psutil.NoSuchProcess(5001)
    )

    assert _proc._recorded_worker_group_state(_identity()) == "unreadable"
    assert seen == [5001, 5001]
    assert not any(event[0] == "probe" for event in reads.events)


@pytest.mark.parametrize("bad_birth", [float("nan"), float("inf"), 10**1000])
def test_nonfinite_zombie_birth_cannot_confirm_empty(reads, monkeypatch, bad_birth):
    reads.probe_outcome = ProcessLookupError()
    seen = _use_recorded_process_sequence(monkeypatch, _FakeRecordedProcess(bad_birth))

    assert _proc._recorded_worker_group_state(_identity()) == "unreadable"
    assert seen == [5001]
    assert not any(event[0] == "probe" for event in reads.events)


def test_matching_zombie_without_group_can_be_confirmed_empty(reads, monkeypatch):
    reads.probe_outcome = ProcessLookupError()
    seen = _use_recorded_process_sequence(
        monkeypatch, _FakeRecordedProcess(10.0), _FakeRecordedProcess(10.0)
    )

    assert _proc._recorded_worker_group_state(_identity()) == "empty"
    assert seen == [5001, 5001]
    assert ("probe", 5001, 0) in reads.events


def test_matching_zombie_cannot_authorize_group_admission(reads, monkeypatch):
    seen = _use_recorded_process_sequence(
        monkeypatch, _FakeRecordedProcess(10.0), _FakeRecordedProcess(10.0)
    )

    assert _admit() == _proc._RecordedGroupRefusal("leader_not_live")
    assert seen == [5001, 5001]
    assert not any(event[0] in ("census", "probe") for event in reads.events)


@pytest.mark.parametrize(
    "status",
    [
        "running",
        "sleeping",
        "disk-sleep",
        "stopped",
        "tracing-stop",
        "wake-kill",
        "waking",
        "idle",
        "parked",
        "locked",
        "waiting",
        "suspended",
    ],
)
def test_known_live_status_is_present(reads, monkeypatch, status):
    seen = _use_recorded_process_sequence(monkeypatch, _FakeRecordedProcess(10.0, status=status))

    assert _proc._recorded_worker_group_state(_identity()) == "present"
    assert seen == [5001]
    assert not any(event[0] == "probe" for event in reads.events)


@pytest.mark.parametrize("status", ["dead", "?", "unexpected", None, []])
def test_unknown_or_dead_status_never_confirms_empty(reads, monkeypatch, status):
    reads.probe_outcome = ProcessLookupError()
    seen = _use_recorded_process_sequence(monkeypatch, _FakeRecordedProcess(10.0, status=status))

    assert _proc._recorded_worker_group_state(_identity()) == "unreadable"
    assert seen == [5001]
    assert not any(event[0] == "probe" for event in reads.events)


def test_zombie_birth_changed_between_fresh_handles_refuses_admission(reads, monkeypatch):
    seen = _use_recorded_process_sequence(
        monkeypatch, _FakeRecordedProcess(10.0), _FakeRecordedProcess(20.0)
    )

    assert _admit() == _proc._RecordedGroupRefusal("identity_mismatch")
    assert seen == [5001, 5001]
    assert not any(event[0] in ("census", "probe") for event in reads.events)


def test_mismatched_birth_is_not_lost_if_status_read_would_reap(reads, monkeypatch):
    seen = _use_recorded_process_sequence(
        monkeypatch, _FakeRecordedProcess(20.0, status=psutil.NoSuchProcess(5001))
    )

    assert _admit() == _proc._RecordedGroupRefusal("identity_mismatch")
    assert seen == [5001]
    assert not any(event[0] in ("census", "probe") for event in reads.events)


def test_zombie_that_changes_status_between_fresh_handles_is_unreadable(reads, monkeypatch):
    reads.probe_outcome = ProcessLookupError()
    seen = _use_recorded_process_sequence(
        monkeypatch,
        _FakeRecordedProcess(10.0),
        _FakeRecordedProcess(10.0, status=psutil.STATUS_RUNNING),
    )

    assert _proc._recorded_worker_group_state(_identity()) == "unreadable"
    assert seen == [5001, 5001]
    assert not any(event[0] == "probe" for event in reads.events)


def test_reused_zombie_frozen_member_never_authorizes_signal(reads, monkeypatch):
    reads.members = []
    witness = _witness()
    seen = _use_recorded_process_sequence(monkeypatch, _FakeRecordedProcess(20.0))

    assert _proc._recorded_worker_group_matches(witness) == "missing"
    assert seen == [5001]
    assert not any(event[0] == "probe" for event in reads.events)


def test_zombie_reuse_after_group_read_does_not_erase_first_birth(reads):
    def changed(pid):
        if pid == 5001:
            reads.births[5001] = ("zombie", 20.0)

    reads.after_group = changed
    assert _admit() == _proc._RecordedGroupRefusal("identity_mismatch")
    assert not any(event[0] in ("census", "probe") for event in reads.events)
