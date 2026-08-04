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
leaves its leg's recorded session keeps running outside every group here; a
member that forks during the sweep can leave a child the verification pass
never saw; and identification and signal are two syscalls, so a group observed
empty was empty when it was read. The round record is what a consumer reads,
and writes that land after the sweep simply miss the round.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from lionagi.ln._proc import live_group_members

from ._round_records import ControlGroupDomain, control_group_domain

__all__ = (
    "QUIET",
    "BUSY",
    "UNPROVEN",
    "NO_DOMAIN",
    "GroupObservation",
    "Quiescence",
    "sweep_quiet",
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
