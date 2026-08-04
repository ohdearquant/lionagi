# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The exclusive claim a finalizer holds while it closes out a manifest round.

Three different processes can arrive at the same round wanting to finish it: the
runner itself at teardown, a reaper acting on a kill request, and a reaper
sweeping up an orphan. Only one may act, and the primitive that decides is an
``flock`` on ``{run_dir}/finalize.lock``, taken non-blocking. The kernel ties the
lock's lifetime to the holding process, so a dead holder's claim vanishes with it
and takeover IS acquisition — there is no stale-lock repair path for two
claimants to race on, because there is no repair path at all.

Two properties are the whole point of this module:

- **The descriptor must not reach a spawned leg.** A lock a leg inherited would
  keep a dead runner's claim alive from inside a living child, and the claim
  would outlive the only process that knew what it was for. What actually
  provides this is the interpreter: since PEP 446 every descriptor Python opens
  is non-inheritable, so ``O_CLOEXEC`` below is a statement of the requirement
  rather than the thing that meets it. The way to lose the property is to mark
  the descriptor inheritable, which is what the test pins.
- **The claim decides nothing.** Every claimant arrives because of something it
  observed BEFORE acquiring, and the gap between observing and acquiring is
  exactly where a live holder finishes, publishes, exits, and releases the lock
  whose availability the claimant is about to read as confirmation. So the first
  act under the claim is a re-read, and the disposition comes only from that.
  This module takes readers rather than state for that reason: there is no
  parameter through which a caller could hand in what it saw earlier.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lionagi.cli.orchestrate._round_records import (
    ROUND_STATE_COMPLETE,
    read_round_summary,
)

__all__ = (
    "CLAIM_ROLE_RUNNER",
    "CLAIM_ROLE_KILL_REAPER",
    "CLAIM_ROLE_ORPHAN_REAPER",
    "NOTHING_OWED",
    "TERMINAL_WRITE_ONLY",
    "LATE_FACTS",
    "FULL_PATH",
    "FinalizeClaim",
    "claim_path",
    "claim_finalization",
)

CLAIM_ROLE_RUNNER = "runner"
CLAIM_ROLE_KILL_REAPER = "kill-reaper"
CLAIM_ROLE_ORPHAN_REAPER = "orphan-reaper"

# The four dispositions the post-acquisition re-read admits, shared by every
# claimant. They are exhaustive over (run terminal?, round complete?).
#
# Terminal and complete: nothing is owed, release.
NOTHING_OWED = "nothing_owed"
# Not terminal but complete: a dead holder finished everything except the
# parent's terminal write. The claimant makes that one write from the recorded
# facts and touches nothing else — no kill and no harvest, because `complete` is
# published only after a proved-quiet sweep, so it has already happened.
TERMINAL_WRITE_ONLY = "terminal_write_only"
# Terminal but not complete: the late-facts pass, which is the unfinalized path
# minus its last step — sweep, harvest, records, flip — with no second terminal
# write and no second notice, since the run already has its terminal facts.
LATE_FACTS = "late_facts"
# Neither: the claimant's full path runs, quiescence first wherever it is
# destructive.
FULL_PATH = "full_path"


def claim_path(run_dir: Path | str) -> Path:
    return Path(run_dir) / "finalize.lock"


@dataclass
class FinalizeClaim:
    """A held claim. Closing the descriptor is what releases it.

    ``disposition`` was computed under this claim, from reads performed after
    the lock was held. It is the only account of the round a holder may act on.
    """

    run_dir: Path
    role: str
    pid: int
    fd: int
    disposition: str
    run_is_terminal: bool
    round_state: str | None

    def __enter__(self) -> FinalizeClaim:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    def release(self) -> None:
        """Drop the claim. Closing the descriptor is what the kernel acts on."""
        if self.fd < 0:
            return
        fd, self.fd = self.fd, -1
        os.close(fd)

    def describe(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "pid": self.pid,
            "disposition": self.disposition,
            "run_is_terminal": self.run_is_terminal,
            "round_state": self.round_state,
        }


def _disposition(run_is_terminal: bool, round_state: str | None) -> str:
    """The four dispositions, from the two facts read under the claim.

    A missing or unreadable summary is never `complete`: the only safe reading
    of "no round.json" is that the round was not published, since treating it as
    complete would skip a sweep that never ran.
    """
    complete = round_state == ROUND_STATE_COMPLETE
    if run_is_terminal and complete:
        return NOTHING_OWED
    if complete:
        return TERMINAL_WRITE_ONLY
    if run_is_terminal:
        return LATE_FACTS
    return FULL_PATH


def claim_finalization(
    run_dir: Path | str,
    *,
    role: str,
    job_marker: str,
    read_run_is_terminal: Callable[[], bool],
    pid: int | None = None,
) -> FinalizeClaim | None:
    """Take the exclusive finalization claim, or return None if one is held.

    ``read_run_is_terminal`` is called AFTER the lock is held, never before.
    That ordering is the contract: a claimant that decided from what it saw on
    the way in would be acting on a world the previous holder has since
    finished changing.

    None means a live owner exists. A caller that gets None re-checks later and
    touches no scratch directory and no record meanwhile — the owner is midway
    through the very files it would be reading.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    pid = os.getpid() if pid is None else pid

    # O_CLOEXEC is redundant against Python's own default and kept anyway, so
    # the requirement is visible where the descriptor is created rather than
    # resting on a language default a reader has to know about.
    fd = os.open(claim_path(run_dir), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Held by someone alive. Not an error, and nothing here is owed a
        # cleanup beyond the descriptor: the file itself belongs to the holder.
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise

    try:
        run_is_terminal = bool(read_run_is_terminal())
        summary = read_round_summary(run_dir)
        round_state = summary.get("round_state") if summary else None
        if not isinstance(round_state, str):
            round_state = None
        claim = FinalizeClaim(
            run_dir=run_dir,
            role=role,
            pid=pid,
            fd=fd,
            disposition=_disposition(run_is_terminal, round_state),
            run_is_terminal=run_is_terminal,
            round_state=round_state,
        )
        _write_claim_body(fd, claim, job_marker)
    except BaseException:
        os.close(fd)
        raise
    return claim


def _write_claim_body(fd: int, claim: FinalizeClaim, job_marker: str) -> None:
    """Record who holds the claim, for a human reading the directory later.

    Written after acquiring and never consulted by anything here. The lock is
    the mechanism; this is observability, and a claimant that could not write it
    would still hold a perfectly valid claim — so a failure to write it must not
    look like a failure to claim.
    """
    body = json.dumps(
        {
            "role": claim.role,
            "pid": claim.pid,
            "job_marker": job_marker,
            "disposition": claim.disposition,
        },
        sort_keys=True,
    )
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, body.encode() + b"\n")
    except OSError:
        pass
