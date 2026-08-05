# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""``round_state: complete`` is published only after every recorded control
group has been observed empty, and these are the ways that observation can be
wrong.

Real subprocesses in real sessions throughout. The thing under test is a
statement about the kernel's process table, and a fake one would let a
predicate that never reads a group pass every case here.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from lionagi.cli.orchestrate._quiescence import (
    BUSY,
    NO_DOMAIN,
    QUIET,
    UNPROVEN,
    enforce_quiet,
    sweep_quiet,
)
from lionagi.cli.orchestrate._round_records import (
    LegDispatch,
    control_group_domain,
    write_leg_dispatch,
)
from lionagi.ln._proc import process_create_time

MARKER = "LIONAGI_TEST_ROUND_MARKER"

# Sits still in its own session until killed.
_SLEEPER = "import time; time.sleep(30)"

# Records its own group as the round's domain and sweeps it twice, once as the
# cooperative finalizer that belongs to that group and once as a reaper that
# does not. Spawned into a new session so it is the only member, which is what
# makes the two answers differ by the observer and nothing else.
_OBSERVER = """
import json, os, sys
from lionagi.cli.orchestrate._quiescence import sweep_quiet
from lionagi.cli.orchestrate._round_records import LegDispatch, write_leg_dispatch
from lionagi.ln._proc import process_create_time

run_dir, marker = sys.argv[1], sys.argv[2]
pgid = os.getpgid(0)
state, created = process_create_time(os.getpid())
assert state == "found"
write_leg_dispatch(run_dir, LegDispatch(
    label="runner", cwd=run_dir, model=None, env_keys=(), brief_hash="z" * 8,
    started_at="2026-08-04T00:00:00+00:00", pid=os.getpid(), pgid=pgid,
    pid_create_time=created,
))
coop = sweep_quiet(run_dir, marker_var=marker, exempt_pgid=pgid)
reap = sweep_quiet(run_dir, marker_var=marker, exempt_pgid=None)
print(json.dumps({
    "pid": os.getpid(),
    "pgid": pgid,
    "cooperative": coop.verdict,
    "cooperative_members": list(coop.groups[0].members),
    "reaper": reap.verdict,
    "reaper_members": list(reap.groups[0].members),
}))
"""


def _spawn_leader() -> subprocess.Popen:
    """A child leading its own process group, the shape every leg is spawned in."""
    return subprocess.Popen(  # noqa: S603 - fixed argv
        [sys.executable, "-c", _SLEEPER],
        start_new_session=True,
        env={**os.environ, MARKER: "this-round"},
    )


def _record_leg(run_dir, label: str, proc: subprocess.Popen | None, *, pinned: bool = True) -> None:
    """Write the spawn-time record a sweep reads its domain from."""
    if proc is None:
        pid = pgid = None
        created = None
    else:
        pid = proc.pid
        pgid = os.getpgid(proc.pid)
        state, created = process_create_time(proc.pid)
        assert state == "found", "the child must be alive when its identity is recorded"
    write_leg_dispatch(
        run_dir,
        LegDispatch(
            label=label,
            cwd=str(run_dir),
            model=None,
            env_keys=(),
            brief_hash="x" * 8,
            started_at="2026-08-04T00:00:00+00:00",
            pid=pid,
            pgid=pgid,
            pid_create_time=created if pinned else None,
        ),
    )


def _reap(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    proc.wait(timeout=10)


def _await_group_empty(pgid: int, deadline: float = 10.0) -> None:
    """Wait until the kernel agrees the group is gone, then assert it."""
    until = time.monotonic() + deadline
    while time.monotonic() < until:
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, OSError):
            return
        time.sleep(0.05)
    pytest.fail(f"group {pgid} was still present after {deadline}s")


class TestTheDomainIsWhatWasRecorded:
    def test_a_run_with_no_leg_records_is_not_quiet(self, tmp_path):
        """A sweep with nothing to sweep produces the same clean-looking result
        as a sweep that found nothing running, and the two mean opposite things
        to anyone about to publish on it."""
        result = sweep_quiet(tmp_path, marker_var=MARKER)

        assert result.verdict == NO_DOMAIN
        assert not result.quiet
        assert result.domain.groups == ()

    def test_a_leg_recorded_without_a_start_time_leaves_the_domain_short(self, tmp_path):
        """Its group id names whatever the kernel has since put at that number,
        so it contributes nothing to sweep. Dropping it silently would let two
        quiet groups out of three read exactly like two out of two."""
        proc = _spawn_leader()
        try:
            _record_leg(tmp_path, "pinned", proc)
            _record_leg(tmp_path, "unpinned", proc, pinned=False)

            domain = control_group_domain(tmp_path)

            assert domain.unpinned == 1
            assert not domain.complete
            assert len(domain.groups) == 1
        finally:
            _reap(proc)

    def test_an_incomplete_domain_is_never_quiet_however_quiet_its_groups_are(self, tmp_path):
        """The groups it does hold can all be empty and the round still has a
        leg nobody looked for."""
        proc = _spawn_leader()
        pgid = os.getpgid(proc.pid)
        _record_leg(tmp_path, "pinned", proc)
        _record_leg(tmp_path, "unpinned", proc, pinned=False)
        _reap(proc)
        _await_group_empty(pgid)

        result = sweep_quiet(tmp_path, marker_var=MARKER)

        assert [g.verdict for g in result.groups] == [QUIET]
        assert result.verdict == UNPROVEN, "a short domain is not a clean sweep"


class TestALiveLegIsFound:
    def test_a_running_leg_makes_the_round_busy(self, tmp_path):
        proc = _spawn_leader()
        try:
            _record_leg(tmp_path, "alive", proc)

            result = sweep_quiet(tmp_path, marker_var=MARKER)

            assert result.verdict == BUSY
            assert proc.pid in result.groups[0].members
        finally:
            _reap(proc)

    def test_the_same_round_is_quiet_once_the_leg_is_gone(self, tmp_path):
        """The control for the case above: same records, same sweep, and the
        only thing that changed is whether the process exists."""
        proc = _spawn_leader()
        pgid = os.getpgid(proc.pid)
        _record_leg(tmp_path, "alive", proc)
        assert sweep_quiet(tmp_path, marker_var=MARKER).verdict == BUSY

        _reap(proc)
        _await_group_empty(pgid)

        result = sweep_quiet(tmp_path, marker_var=MARKER)
        assert result.verdict == QUIET
        assert result.groups[0].members == ()

    def test_a_busy_group_outranks_a_short_domain(self, tmp_path):
        """A live member is a fact about the round. It stays the answer whatever
        else the sweep could not establish."""
        proc = _spawn_leader()
        try:
            _record_leg(tmp_path, "alive", proc)
            _record_leg(tmp_path, "unpinned", proc, pinned=False)

            assert sweep_quiet(tmp_path, marker_var=MARKER).verdict == BUSY
        finally:
            _reap(proc)


class TestWhereTheObserverSits:
    """A reaper belongs to no recorded group and demands absolute emptiness. A
    cooperative finalizer runs inside the runner's group and exempts exactly
    itself. Stated per path because a predicate that forgets its own observer
    fails toward unsatisfiable, and one that assumes it is outside when it is
    inside certifies a group holding the observer as empty."""

    def test_the_two_predicates_differ_by_exactly_the_observer(self, tmp_path):
        """Run from a process that is the sole member of its own group, so the
        only thing separating an empty group from an occupied one is whether the
        observer counts itself.

        It has to be a real sole-member process: this test process shares its
        group with the runner that started it, so both predicates would answer
        BUSY there and the comparison would prove nothing while looking as
        though it had.
        """
        out = subprocess.run(  # noqa: S603 - fixed argv
            [sys.executable, "-c", _OBSERVER, str(tmp_path), MARKER],
            start_new_session=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert out.returncode == 0, out.stderr
        seen = json.loads(out.stdout)

        # Cooperative: inside the runner's group, exempting exactly itself.
        assert seen["cooperative"] == QUIET
        assert seen["cooperative_members"] == []
        # A reaper shares nothing with the round and counts every member.
        assert seen["reaper"] == BUSY
        assert seen["reaper_members"] == [seen["pid"]]

    def test_the_observer_process_really_was_alone_in_its_group(self, tmp_path):
        """The control for the case above. If that child had shared its group,
        both predicates would answer BUSY and the contrast would come from the
        neighbours rather than from the exemption."""
        out = subprocess.run(  # noqa: S603 - fixed argv
            [sys.executable, "-c", _OBSERVER, str(tmp_path), MARKER],
            start_new_session=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert out.returncode == 0, out.stderr
        seen = json.loads(out.stdout)

        assert seen["pgid"] == seen["pid"], "start_new_session makes the child its own leader"
        assert seen["reaper_members"] == [seen["pid"]], "nobody else was in the group"

    def test_the_exemption_covers_the_observer_and_nobody_else(self, tmp_path):
        """Exempting a group rather than a pid would let the finalizer certify a
        group still holding every other member of it."""
        own_pgid = os.getpgid(0)
        sibling = subprocess.Popen([sys.executable, "-c", _SLEEPER])  # noqa: S603 - fixed argv
        try:
            assert os.getpgid(sibling.pid) == own_pgid, "sibling must share this group"
            state, created = process_create_time(os.getpid())
            assert state == "found"
            write_leg_dispatch(
                tmp_path,
                LegDispatch(
                    label="runner",
                    cwd=str(tmp_path),
                    model=None,
                    env_keys=(),
                    brief_hash="z" * 8,
                    started_at="2026-08-04T00:00:00+00:00",
                    pid=os.getpid(),
                    pgid=own_pgid,
                    pid_create_time=created,
                ),
            )

            result = sweep_quiet(tmp_path, marker_var=MARKER, exempt_pgid=own_pgid)

            assert result.verdict == BUSY
            assert sibling.pid in result.groups[0].members
            assert os.getpid() not in result.groups[0].members
        finally:
            sibling.kill()
            sibling.wait(timeout=10)


class TestAnUnfinishedScanIsNotAnAnswer:
    def test_a_scan_that_could_not_read_the_process_table_is_unproven(self, tmp_path, monkeypatch):
        """It saw no members, and that is exactly what a genuinely empty group
        also looks like. Reporting it as quiet would publish on a read that
        never happened."""
        import psutil

        proc = _spawn_leader()
        try:
            _record_leg(tmp_path, "alive", proc)

            def refuse():
                raise psutil.Error("process table unavailable")

            monkeypatch.setattr(psutil, "pids", refuse)
            result = sweep_quiet(tmp_path, marker_var=MARKER)

            assert result.groups[0].members == (), "the failed scan saw nothing"
            assert result.groups[0].scan_complete is False
            assert result.verdict == UNPROVEN, "seeing nothing through a broken instrument"
        finally:
            _reap(proc)

    def test_the_same_sweep_is_busy_once_the_table_can_be_read(self, tmp_path):
        """The control: the group above was never empty, so the refusal came
        from the instrument rather than from the round having finished."""
        proc = _spawn_leader()
        try:
            _record_leg(tmp_path, "alive", proc)
            assert sweep_quiet(tmp_path, marker_var=MARKER).verdict == BUSY
        finally:
            _reap(proc)


# A leg leader that starts a SIGTERM-ignoring descendant inside its own group,
# then reports ready. What makes this the right subject: a cooperative
# descendant dies to any signal at all, so every version of the enforcement
# path looks identical against one.
_LEADER_WITH_STUBBORN_CHILD = (
    "import os, subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]])\n"
    "while not os.path.exists(sys.argv[2]):\n"
    "    time.sleep(0.02)\n"
    "open(sys.argv[3], 'w').close()\n"
    "time.sleep(60)\n"
)

_STUBBORN_CHILD = (
    "import os, signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "time.sleep(60)\n"
)


def _spawn_leader_with_descendant(tmp_path) -> tuple[subprocess.Popen, int]:
    """A leg group holding two processes, the second of which ignores SIGTERM."""
    kid_pid = tmp_path / "kid.pid"
    ready = tmp_path / "ready"
    proc = subprocess.Popen(  # noqa: S603 - fixed argv
        [
            sys.executable,
            "-c",
            _LEADER_WITH_STUBBORN_CHILD,
            _STUBBORN_CHILD,
            str(kid_pid),
            str(ready),
        ],
        start_new_session=True,
        env={**os.environ, MARKER: "this-round"},
    )
    until = time.monotonic() + 20.0
    while time.monotonic() < until:
        if ready.exists():
            return proc, int(kid_pid.read_text())
        time.sleep(0.05)
    _reap(proc)
    pytest.fail("the leg never reported that its descendant was up")


class TestARoundIsMadeQuietBeforeItSaysSo:
    def test_a_straggler_is_ended_and_the_verdict_comes_from_re_observing(self, tmp_path):
        """The whole group is ended, not the leader. A descendant left running
        during the harvest window can still be writing the files about to be
        collected, and it belongs to no record anyone will read again."""
        proc, kid = _spawn_leader_with_descendant(tmp_path)
        # Read while the leader is known alive: after the enforcement its pid is
        # reaped and the group is no longer resolvable from it.
        pgid = os.getpgid(proc.pid)
        try:
            _record_leg(tmp_path, "straggler", proc)
            assert sweep_quiet(tmp_path, marker_var=MARKER).verdict == BUSY

            result = enforce_quiet(tmp_path, marker_var=MARKER, settle=10.0)

            assert result.before.verdict == BUSY
            assert result.quiet, result.describe()
            assert result.after.verdict == QUIET
            assert not result.already_quiet
            assert result.groups_killed == (pgid,)
            _assert_gone(kid, "the SIGTERM-ignoring descendant")
        finally:
            _reap(proc)

    def test_an_already_quiet_round_is_not_signalled_at_all(self, tmp_path):
        """Making a round quiet and finding it quiet are different facts about
        the round, and a close that had to kill something is reporting a leak."""
        proc = _spawn_leader()
        _record_leg(tmp_path, "finished", proc)
        _reap(proc)
        # start_new_session, so the leader IS the group id, readable after death.
        _await_group_empty(proc.pid)

        result = enforce_quiet(tmp_path, marker_var=MARKER, settle=1.0)

        assert result.quiet
        assert result.already_quiet
        assert result.groups_killed == ()
        assert result.pids_signalled == ()

    def test_an_unreadable_scan_is_never_signalled_on(self, tmp_path, monkeypatch):
        """An incomplete scan says an unread member MAY exist, never who is
        there. Signalling on it reaches whatever now holds a recycled group id,
        which is the one outcome worse than leaving a straggler."""
        import psutil

        proc = _spawn_leader()
        try:
            _record_leg(tmp_path, "unreadable", proc)

            def refuse():
                raise psutil.Error("process table unavailable")

            monkeypatch.setattr(psutil, "pids", refuse)
            result = enforce_quiet(tmp_path, marker_var=MARKER, settle=0.5)

            assert result.before.verdict == UNPROVEN
            assert result.groups_killed == (), "an unproven group must not be signalled"
            assert result.pids_signalled == ()
            assert not result.quiet
        finally:
            _reap(proc)

    def test_the_control_the_unreadable_case_needs(self, tmp_path):
        """The group above was never empty and the signal was withheld because
        the scan failed, not because there was nothing to end. Same group, same
        call, working instrument."""
        proc = _spawn_leader()
        try:
            _record_leg(tmp_path, "unreadable", proc)
            result = enforce_quiet(tmp_path, marker_var=MARKER, settle=10.0)

            assert result.before.verdict == BUSY
            assert result.groups_killed != (), "the working instrument does signal"
            assert result.quiet
        finally:
            _reap(proc)


def _alive(pid: int) -> bool:
    import psutil

    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _assert_gone(pid: int, what: str, deadline: float = 15.0) -> None:
    """Fail unless *pid* is gone, and never leave it running.

    The kill on the failure path is not tidiness: this pid ignores SIGTERM and
    sleeps for a minute, so a failing assertion that merely reported would leave
    it behind for every later test in the session.
    """
    until = time.monotonic() + deadline
    while time.monotonic() < until:
        if not _alive(pid):
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    pytest.fail(f"{what} ({pid}) survived the enforcement by {deadline}s")
