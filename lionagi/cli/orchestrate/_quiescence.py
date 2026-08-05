# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Proving that nothing a manifest round started is still running.

``round_state: complete`` is a claim that the round's work is over, and it is
published only after this sweep has observed every recorded control group
empty. The domain is read from the run directory rather than from a live
process's memory, so a reaper that shared nothing with a dead runner sweeps
exactly what the runner would have swept.

Three things about the predicate are load-bearing, and all three are the sort
that fail toward reassuring if left implicit:

- **Where the observer sits.** A reaper belongs to no recorded group, so its
  predicate is absolute emptiness. A cooperative finalizer is a member of the
  runner's own group, so its predicate exempts exactly itself and nothing else.
  A predicate that forgets its own observer fails toward unsatisfiable; one
  that assumes it is outside when it is inside certifies a group holding the
  observer as empty.
- **Who joins the domain.** The groups come from each leg record's spawn-time
  capture, written on the record's first write. That is the mechanism that
  populates the set, and it is named here because a sweep over a domain nobody
  joined is indistinguishable from a clean sweep. So an incomplete domain is
  never quiet, however quiet its groups are.
- **An unfinished measurement is not an answer.** A scan that could not read
  the process table, or a member whose identity could not be pinned, leaves the
  scan incomplete. That is reported as its own verdict and never as emptiness.

What this cannot close, stated rather than papered over: a descendant that
leaves its leg's recorded process GROUP keeps running outside every group
here, and leaving the group is all it takes — ``setpgid(0, 0)`` is enough and
stays in the same session, so this is not confined to the descendants that
call ``setsid``, which is the narrower class an earlier wording of this
paragraph named. Membership is the only thing read (``getpgid(pid) != pgid``),
so nothing group-based can see a process that has left, and no change here
would fix it. A member that forks during the sweep can likewise leave a child
the verification pass never saw; and identification and signal are two
syscalls, so a group observed empty was empty when it was read. The round
record is what a consumer reads, and writes that land after the sweep simply
miss the round.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from lionagi.ln._proc import kill_group_now, live_group_members

from ._round_records import ControlGroupDomain, control_group_domain

__all__ = (
    "QUIET",
    "BUSY",
    "UNPROVEN",
    "NO_DOMAIN",
    "GroupObservation",
    "Quiescence",
    "QuietEnforcement",
    "sweep_quiet",
    "enforce_quiet",
)

# Every recorded group was observed to hold nothing the predicate admits.
QUIET = "quiet"
# At least one recorded group holds a live member. A positive finding: something
# the round started is still running.
BUSY = "busy"
# The sweep did not come off. Either a group scan was incomplete, or the domain
# itself was short of what the run recorded. Never read as either of the above.
UNPROVEN = "unproven"
# The run recorded no sweepable group at all. Kept apart from QUIET because a
# sweep with nothing to sweep is the reassuring shape of a sweep that found
# nothing, and a caller must not publish on it.
NO_DOMAIN = "no_domain"


@dataclass(frozen=True)
class GroupObservation:
    """One recorded group, as one scan saw it."""

    pgid: int
    verdict: str
    members: tuple[int, ...]
    scan_complete: bool


@dataclass(frozen=True)
class Quiescence:
    """The sweep's verdict and everything it rests on.

    ``verdict`` is the only thing a caller should branch on, and the fields
    beside it are what makes a refusal diagnosable rather than merely negative.
    """

    verdict: str
    groups: tuple[GroupObservation, ...]
    domain: ControlGroupDomain
    observer_pid: int
    exempt_pgid: int | None

    @property
    def quiet(self) -> bool:
        return self.verdict == QUIET

    def describe(self) -> dict[str, object]:
        return {
            "verdict": self.verdict,
            "observer_pid": self.observer_pid,
            "exempt_pgid": self.exempt_pgid,
            "domain_groups": len(self.domain.groups),
            "domain_records": self.domain.records,
            "domain_unpinned": self.domain.unpinned,
            "domain_unreadable": self.domain.unreadable,
            "busy": [g.pgid for g in self.groups if g.verdict == BUSY],
            "unproven": [g.pgid for g in self.groups if g.verdict == UNPROVEN],
        }


def sweep_quiet(
    run_dir: Path | str,
    *,
    marker_var: str,
    exempt_pgid: int | None = None,
    observer_pid: int | None = None,
) -> Quiescence:
    """Observe every recorded control group and say whether the round is quiet.

    ``exempt_pgid`` names the one group the observer belongs to, which is the
    cooperative finalizer's case: it is running inside the runner's group, so
    that group holds the observer and the predicate there admits the observer's
    own pid and nothing else. A reaper passes None, belongs to no recorded
    group, and gets absolute emptiness everywhere.

    Passing an ``exempt_pgid`` the observer is NOT in would exempt a pid from a
    group it does not lead, so the exemption applies only to the observer's own
    pid inside that one group; every other pid in it is a member.
    """
    observer_pid = os.getpid() if observer_pid is None else observer_pid
    domain = control_group_domain(run_dir)

    observations: list[GroupObservation] = []
    for pgid in domain.groups:
        members, scan_complete = live_group_members(pgid, marker_var=marker_var)
        pids = tuple(
            pid
            for pid, _created, _marker, _marker_read in members
            if not (pgid == exempt_pgid and pid == observer_pid)
        )
        if not scan_complete:
            # An unread member is indistinguishable from an absent one, so this
            # is neither emptiness nor a finding of activity. Reported before
            # the member check so a partial scan that happened to see nothing
            # cannot pass as quiet.
            verdict = UNPROVEN
        elif pids:
            verdict = BUSY
        else:
            verdict = QUIET
        observations.append(
            GroupObservation(
                pgid=pgid,
                verdict=verdict,
                members=pids,
                scan_complete=scan_complete,
            )
        )

    return Quiescence(
        verdict=_overall(domain, observations),
        groups=tuple(observations),
        domain=domain,
        observer_pid=observer_pid,
        exempt_pgid=exempt_pgid,
    )


@dataclass(frozen=True)
class QuietEnforcement:
    """What a round close did to make itself quiet, and whether it worked.

    ``before`` and ``after`` are two separate sweeps. The second one is the
    answer: signalling is not ending, and a caller that published on the
    strength of having sent a signal would publish on its own intent.
    """

    before: Quiescence
    after: Quiescence
    groups_killed: tuple[int, ...]
    pids_signalled: tuple[int, ...]

    @property
    def quiet(self) -> bool:
        return self.after.quiet

    @property
    def already_quiet(self) -> bool:
        """Nothing was signalled, because nothing needed to be."""
        return self.before.quiet and not self.groups_killed and not self.pids_signalled

    def describe(self) -> dict[str, object]:
        return {
            "before": self.before.describe(),
            "after": self.after.describe(),
            "groups_killed": list(self.groups_killed),
            "pids_signalled": list(self.pids_signalled),
            "already_quiet": self.already_quiet,
        }


def enforce_quiet(
    run_dir: Path | str,
    *,
    marker_var: str,
    exempt_pgid: int | None = None,
    observer_pid: int | None = None,
    settle: float = 2.0,
) -> QuietEnforcement:
    """End what a round's recorded groups still hold, then re-observe.

    The close-time counterpart to :func:`sweep_quiet`. A leg that ended normally
    leaves its group empty and this changes nothing; a straggler still inside one
    is ended at round close rather than tolerated into the harvest window, where
    it could still be writing the files about to be collected.

    Four properties, each of which fails toward reassuring if left implicit.

    **It signals only on positive evidence.** A group is ended only where the
    sweep pinned a live member in it. That evidence is also what makes the group
    id safe to use: a process group id is not reissued while the group still has
    members, so a group that answers with members is the one whose id was
    recorded. On the other side, an incomplete scan says an unread member may
    exist, never who is there — signalling on that reaches whatever now holds a
    recycled id. So UNPROVEN is left alone and reported, not swept.

    **The mechanism follows the observer's position, and the rule does not
    change with it.** For a group the observer is outside, the whole group is
    killed, which also reaches a member that appeared after the scan. The
    observer's own group cannot be killed that way without ending the observer,
    which still has to publish, so there its pinned members are signalled
    individually and it is skipped. Same predicate, two mechanisms.

    **Sending a signal is not ending a process.** The second sweep is the
    verdict. ``settle`` bounds how long delivery is given; a group still holding
    a member after it is BUSY, and the round is not quiet.

    **Making it quiet and finding it quiet are different facts**, and
    ``already_quiet`` keeps them apart. A close that had to kill something is
    reporting that the round leaked, which a reader should be able to see even
    though the outcome is the same word.
    """
    observer_pid = os.getpid() if observer_pid is None else observer_pid
    before = sweep_quiet(
        run_dir,
        marker_var=marker_var,
        exempt_pgid=exempt_pgid,
        observer_pid=observer_pid,
    )

    killed: list[int] = []
    signalled: list[int] = []
    for group in before.groups:
        # BUSY, never merely non-empty: the verdict already folded in the scan
        # completeness and the observer exemption, and re-deriving either here
        # would be a second predicate to keep in step with the first.
        if group.verdict != BUSY:
            continue
        if group.pgid == exempt_pgid:
            for pid in group.members:
                # The sweep already dropped the observer from this group's
                # members, so this is belt and braces on the one pid whose
                # death would strand the round mid-publish.
                if pid == observer_pid:
                    continue
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.kill(pid, signal.SIGKILL)
                    signalled.append(pid)
        elif kill_group_now(group.pgid):
            # kill_group_now refuses pid<=1 and the caller's own group, and
            # reports whether it signalled. Re-deriving that judgement here
            # would be a second predicate to keep in step with the first.
            killed.append(group.pgid)

    _wait_for_delivery(
        {*killed, *([exempt_pgid] if signalled and exempt_pgid is not None else [])},
        marker_var=marker_var,
        exempt_pgid=exempt_pgid,
        observer_pid=observer_pid,
        settle=settle,
    )

    after = sweep_quiet(
        run_dir,
        marker_var=marker_var,
        exempt_pgid=exempt_pgid,
        observer_pid=observer_pid,
    )
    return QuietEnforcement(
        before=before,
        after=after,
        groups_killed=tuple(killed),
        pids_signalled=tuple(signalled),
    )


def _wait_for_delivery(
    pgids: set[int],
    *,
    marker_var: str,
    exempt_pgid: int | None,
    observer_pid: int,
    settle: float,
) -> None:
    """Give the signals time to land, and stop as soon as they have.

    A courtesy, not the verdict: the sweep that follows is what decides, and
    this only spares it from being taken while a killed process is still on its
    way out. It watches the groups that were actually signalled rather than
    re-reading the whole domain, because a leg record landing mid-wait would
    change the set the wait is waiting on and leave its exit condition talking
    about a different round than the sweep that motivated it.
    """
    if not pgids or settle <= 0:
        return
    deadline = time.monotonic() + settle
    while time.monotonic() < deadline:
        if not any(
            _holds_a_member(
                pgid, marker_var=marker_var, exempt_pgid=exempt_pgid, observer_pid=observer_pid
            )
            for pgid in pgids
        ):
            return
        time.sleep(0.05)


def _holds_a_member(
    pgid: int, *, marker_var: str, exempt_pgid: int | None, observer_pid: int
) -> bool:
    """Whether *pgid* still holds a member the quiet predicate would count.

    An incomplete scan answers True, so the wait continues rather than releasing
    on a reading that could not see everyone. The benefit is confined to a
    transiently unreadable process table, where waiting lets the next poll
    reach a readable one and converge to QUIET instead of settling for
    UNPROVEN. Where the table stays unreadable this only spends the settle
    budget: the sweep that follows answers UNPROVEN either way, because it is
    the verdict and this is not.

    That confinement is also why no test here pins this direction. A mutant
    that releases early survives the suite, which is a gap in the tests rather
    than a defence of the mutant: constructing the transient case means
    controlling how many reads fail, and a test whose apparatus decides the
    timing decides the outcome.
    """
    members, complete = live_group_members(pgid, marker_var=marker_var)
    if not complete:
        return True
    return any(not (pgid == exempt_pgid and pid == observer_pid) for pid, _c, _m, _r in members)


def _overall(domain: ControlGroupDomain, observations: list[GroupObservation]) -> str:
    """The sweep's verdict from the domain and the per-group ones.

    BUSY outranks UNPROVEN: a group observed to hold a live member is a fact
    about the round, and it stays the answer whatever a different group's scan
    managed. An incomplete domain outranks a clean sweep in the other
    direction, because the groups it is missing were never looked at.
    """
    if any(g.verdict == BUSY for g in observations):
        return BUSY
    if not domain.complete:
        return UNPROVEN
    if not domain.groups:
        return NO_DOMAIN
    if any(g.verdict == UNPROVEN for g in observations):
        return UNPROVEN
    return QUIET
