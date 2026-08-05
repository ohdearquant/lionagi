# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The exclusive claim that decides which of three possible finalizers acts.

Two properties carry the weight. The claim is an ``flock`` whose lifetime the
kernel ties to the holding process, so a dead holder's claim is gone and
takeover is just acquisition. And the disposition a holder acts on is read
AFTER acquiring, never from whatever sent it to the lock in the first place —
the gap between observing and acquiring is precisely where the previous holder
finishes and releases.

Several of these use real second processes. A claim's exclusivity is the
kernel's, and two flocks taken from one process do not test it: POSIX
``flock`` is per open-file-description, so the same process can re-lock its own
file and a same-process test would pass against an implementation with no
exclusivity at all.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from lionagi.cli.orchestrate._finalize import (
    CLAIM_ROLE_KILL_REAPER,
    CLAIM_ROLE_RUNNER,
    FULL_PATH,
    LATE_FACTS,
    NOTHING_OWED,
    TERMINAL_WRITE_ONLY,
    claim_finalization,
    claim_path,
)
from lionagi.cli.orchestrate._round_records import (
    ROUND_STATE_COMPLETE,
    flip_round_complete,
    write_round_summary,
)


def _claim(run_dir, *, role=CLAIM_ROLE_RUNNER, terminal=False, marker="job-1"):
    return claim_finalization(
        run_dir,
        role=role,
        job_marker=marker,
        read_run_is_terminal=lambda: terminal,
    )


# A child that takes the claim, reports, and holds it until told to exit.
_HOLDER = """
import sys
sys.path.insert(0, {repo!r})
from lionagi.cli.orchestrate._finalize import claim_finalization
claim = claim_finalization({run_dir!r}, role="runner", job_marker="job-1",
                           read_run_is_terminal=lambda: False)
print("held" if claim is not None else "refused", flush=True)
sys.stdin.readline()
"""


def _repo_root() -> str:
    import lionagi

    return str(os.path.dirname(os.path.dirname(os.path.abspath(lionagi.__file__))))


def _spawn_holder(run_dir) -> subprocess.Popen:
    script = textwrap.dedent(_HOLDER).format(repo=_repo_root(), run_dir=str(run_dir))
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, generated script
        [sys.executable, "-c", script],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "held"
    return proc


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.stdin.write("\n")
        proc.stdin.flush()
    proc.wait(timeout=30)


class TestExclusivity:
    def test_a_second_claimant_is_refused_while_a_live_holder_exists(self, tmp_path):
        holder = _spawn_holder(tmp_path)
        try:
            assert _claim(tmp_path) is None
        finally:
            _stop(holder)

    def test_the_claim_is_available_again_once_the_holder_dies(self, tmp_path):
        """The kernel couples the lock to its holder, so a dead owner's claim
        vanishes with the process. That is what makes takeover identical to
        acquisition, with no stale-lock repair path for two reapers to race."""
        holder = _spawn_holder(tmp_path)
        assert _claim(tmp_path) is None

        holder.kill()
        holder.wait(timeout=30)

        claim = _claim(tmp_path)
        assert claim is not None
        claim.release()

    def test_releasing_hands_the_claim_to_the_next_claimant(self, tmp_path):
        first = _claim(tmp_path)
        assert first is not None
        holder = None
        try:
            # A second process is refused while the first claim is open.
            script = textwrap.dedent(
                """
                import sys
                sys.path.insert(0, {repo!r})
                from lionagi.cli.orchestrate._finalize import claim_finalization
                c = claim_finalization({run_dir!r}, role="kill-reaper", job_marker="job-1",
                                       read_run_is_terminal=lambda: False)
                print("held" if c is not None else "refused", flush=True)
                """
            ).format(repo=_repo_root(), run_dir=str(tmp_path))
            out = subprocess.run(  # noqa: S603 - fixed argv, generated script
                [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
            )
            assert out.stdout.strip() == "refused"

            first.release()

            out = subprocess.run(  # noqa: S603 - fixed argv, generated script
                [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
            )
            assert out.stdout.strip() == "held"
        finally:
            first.release()
            if holder is not None:
                _stop(holder)

    def test_the_context_manager_releases(self, tmp_path):
        with _claim(tmp_path) as claim:
            assert claim is not None
        assert _claim(tmp_path) is not None

    def test_release_is_idempotent(self, tmp_path):
        claim = _claim(tmp_path)
        claim.release()
        claim.release()


class TestTheDescriptorIsNotInherited:
    def test_a_spawned_child_does_not_hold_the_claim(self, tmp_path):
        """A leg that inherited the claim descriptor would keep a dead runner's
        claim alive from inside a living child, and no reaper could ever take
        over. Measured by killing the claim holder while a child it spawned is
        still running, then claiming: if the child held a copy, this refuses."""
        claim = _claim(tmp_path)
        assert claim is not None

        child = subprocess.Popen(  # noqa: S603 - fixed argv
            [sys.executable, "-c", "import sys; sys.stdin.readline()"],
            stdin=subprocess.PIPE,
            text=True,
        )
        try:
            claim.release()
            # The child is still alive. If the descriptor had leaked into it,
            # the flock would still be held and this would come back None.
            second = _claim(tmp_path)
            assert second is not None
            second.release()
        finally:
            child.kill()
            child.wait(timeout=30)

    def test_the_open_descriptor_is_not_inheritable(self, tmp_path):
        """Asserted as the outcome rather than as the O_CLOEXEC flag, because
        the flag is not what provides it: since PEP 446 every descriptor Python
        opens is non-inheritable, measured here on both arms, so a test that
        failed when the flag was removed would be pinning an implementation
        detail while the property it names went on holding. The one thing that
        does break the property is marking the descriptor inheritable, and that
        is what the arm below detects."""
        import fcntl

        claim = _claim(tmp_path)
        try:
            assert os.get_inheritable(claim.fd) is False
            assert fcntl.fcntl(claim.fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
        finally:
            claim.release()

    def test_marking_the_descriptor_inheritable_is_what_would_break_it(self, tmp_path):
        """The control for the test above: it shows the assertion can fail, and
        it names the single change that would make a leg inherit the claim."""
        import fcntl

        claim = _claim(tmp_path)
        try:
            os.set_inheritable(claim.fd, True)
            assert os.get_inheritable(claim.fd) is True
            assert not (fcntl.fcntl(claim.fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC)
        finally:
            claim.release()


class TestTheFourDispositions:
    """Exhaustive over (run terminal?, round complete?). Every claimant shares
    them, and each names a different amount of remaining work."""

    def test_terminal_and_complete_means_nothing_is_owed(self, tmp_path):
        write_round_summary(tmp_path, labels=["a"])
        flip_round_complete(tmp_path, result="completed", legs_succeeded=1)

        with _claim(tmp_path, terminal=True) as claim:
            assert claim.disposition == NOTHING_OWED
            assert claim.round_state == ROUND_STATE_COMPLETE

    def test_complete_but_not_terminal_leaves_only_the_terminal_write(self, tmp_path):
        """A dead holder finished everything except the parent's terminal
        write. `complete` is published only after a proved-quiet sweep, so no
        kill and no harvest are owed here — the work is one write from facts
        that are already on disk."""
        write_round_summary(tmp_path, labels=["a"])
        flip_round_complete(tmp_path, result="completed", legs_succeeded=1)

        with _claim(tmp_path, terminal=False) as claim:
            assert claim.disposition == TERMINAL_WRITE_ONLY

    def test_terminal_but_pending_means_the_late_facts_pass(self, tmp_path):
        write_round_summary(tmp_path, labels=["a"])

        with _claim(tmp_path, terminal=True) as claim:
            assert claim.disposition == LATE_FACTS

    def test_neither_means_the_full_path(self, tmp_path):
        write_round_summary(tmp_path, labels=["a"])

        with _claim(tmp_path, terminal=False) as claim:
            assert claim.disposition == FULL_PATH

    @pytest.mark.parametrize("terminal", [True, False])
    def test_a_missing_summary_is_never_read_as_complete(self, tmp_path, terminal):
        """The only safe reading of "no round.json" is that the round was not
        published. Treating it as complete would skip a quiescence sweep that
        has demonstrably never run."""
        with _claim(tmp_path, terminal=terminal) as claim:
            assert claim.round_state is None
            assert claim.disposition == (LATE_FACTS if terminal else FULL_PATH)

    @pytest.mark.parametrize("terminal", [True, False])
    def test_an_unreadable_summary_is_never_read_as_complete(self, tmp_path, terminal):
        write_round_summary(tmp_path, labels=["a"])
        flip_round_complete(tmp_path, result="completed", legs_succeeded=1)
        (tmp_path / "round.json").write_text("{ not json")

        with _claim(tmp_path, terminal=terminal) as claim:
            assert claim.round_state is None
            assert claim.disposition == (LATE_FACTS if terminal else FULL_PATH)

    def test_a_non_string_round_state_is_not_a_state(self, tmp_path):
        (tmp_path / "round.json").write_text(json.dumps({"round_state": ["complete"]}))

        with _claim(tmp_path, terminal=True) as claim:
            assert claim.round_state is None
            assert claim.disposition == LATE_FACTS


class TestTheReReadHappensUnderTheClaim:
    def test_the_terminal_read_runs_after_the_lock_is_held(self, tmp_path):
        """The whole reason this module takes readers instead of values: a
        claimant that decided from what it saw on the way in would be acting on
        a world the previous holder has since finished changing. The reader
        proves the ordering by observing the lock file's own state when called.
        """
        observed: list[bool] = []

        def read_terminal() -> bool:
            # By the time this runs the claim file must exist, and this process
            # must already hold the lock — a second acquire from another
            # process would fail. Recorded rather than asserted here so the
            # failure shows up as a value, not an exception inside a callback.
            observed.append(claim_path(tmp_path).exists() and _held_elsewhere(tmp_path))
            return False

        with claim_finalization(
            tmp_path,
            role=CLAIM_ROLE_RUNNER,
            job_marker="job-1",
            read_run_is_terminal=read_terminal,
        ) as claim:
            assert claim is not None

        assert observed == [True]

    def test_the_summary_read_sees_a_write_made_just_before_acquisition(self, tmp_path):
        """A holder that finishes and flips the round between the claimant's
        motivating observation and its acquire is the exact race this ordering
        exists for. The claimant's disposition must reflect the flip, not the
        state that sent it to the lock."""
        write_round_summary(tmp_path, labels=["a"])

        def read_terminal() -> bool:
            # Stand-in for the previous holder finishing in the gap.
            flip_round_complete(tmp_path, result="completed", legs_succeeded=1)
            return True

        with claim_finalization(
            tmp_path,
            role=CLAIM_ROLE_KILL_REAPER,
            job_marker="job-1",
            read_run_is_terminal=read_terminal,
        ) as claim:
            assert claim.disposition == NOTHING_OWED


def _held_elsewhere(run_dir) -> bool:
    """True when a separate process cannot take the claim on *run_dir*."""
    script = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, {repo!r})
        from lionagi.cli.orchestrate._finalize import claim_finalization
        c = claim_finalization({run_dir!r}, role="orphan-reaper", job_marker="probe",
                               read_run_is_terminal=lambda: False)
        print("held" if c is not None else "refused", flush=True)
        """
    ).format(repo=_repo_root(), run_dir=str(run_dir))
    out = subprocess.run(  # noqa: S603 - fixed argv, generated script
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    return out.stdout.strip() == "refused"


class TestTheClaimBodyIsObservabilityOnly:
    def test_the_holder_records_who_it_is(self, tmp_path):
        write_round_summary(tmp_path, labels=["a"])

        with _claim(tmp_path, role=CLAIM_ROLE_KILL_REAPER, marker="job-77") as claim:
            body = json.loads(claim_path(tmp_path).read_text())

        assert body["role"] == CLAIM_ROLE_KILL_REAPER
        assert body["job_marker"] == "job-77"
        assert body["disposition"] == FULL_PATH
        assert body["pid"] == claim.pid

    def test_a_pre_existing_body_does_not_grant_or_deny_a_claim(self, tmp_path):
        """The lock is the mechanism and the file's content is not. A leftover
        body from a dead holder describes a claim that no longer exists, so
        reading it as authority would invent a holder out of a stale file."""
        tmp_path.mkdir(parents=True, exist_ok=True)
        claim_path(tmp_path).write_text(
            json.dumps({"role": "runner", "pid": 999999, "job_marker": "old"}) + "\n"
        )

        claim = _claim(tmp_path)
        try:
            assert claim is not None
            body = json.loads(claim_path(tmp_path).read_text())
            # Rewritten, not appended: a body holding both accounts would leave
            # a reader to guess which holder is current.
            assert body["pid"] == claim.pid
            assert body["job_marker"] == "job-1"
        finally:
            claim.release()

    def test_a_body_that_cannot_be_written_still_yields_a_valid_claim(self, tmp_path, monkeypatch):
        """The claim is the lock. A failure to write the description of it is a
        loss of observability, and reporting that as a failure to claim would
        send a finalizer away from a round it does in fact own."""
        import lionagi.cli.orchestrate._finalize as finalize_mod

        real_write = os.write

        def failing_write(fd, data):
            raise OSError("disk full")

        monkeypatch.setattr(finalize_mod.os, "write", failing_write)
        claim = _claim(tmp_path)
        monkeypatch.setattr(finalize_mod.os, "write", real_write)
        try:
            assert claim is not None
            assert claim.disposition == FULL_PATH
        finally:
            claim.release()
