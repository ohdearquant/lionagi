# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import contextlib
import os
import signal
from typing import Any

from .concurrency import move_on_after

# Two reads of a kernel tick clock for one process can differ in the last
# place, so a recorded start time is compared to a live one within this. Two
# LIVE reads of the same process must still compare exactly equal.
CREATE_TIME_TOLERANCE = 0.1


def process_create_time(pid: int) -> tuple[str, float | None]:
    """When the process at *pid* started: ``("found", t)``, ``("gone", None)``
    or ``("unknown", None)``.

    "unknown" means the probe errored — the process may still be there — never
    read it as death or license to signal. A zombie is "gone": it has exited but
    holds its pid until reaped, so it can't be a recycled pid meanwhile.
    """
    import psutil

    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return "gone", None
        return "found", proc.create_time()
    except psutil.NoSuchProcess:
        return "gone", None
    except (psutil.Error, OSError):
        return "unknown", None


def start_time_matches(observed: float, recorded: float) -> bool:
    """Whether a start time read now is the one recorded for this run at spawn.

    Only for a recorded value against a live one; two live reads of the same
    process must compare exactly equal.
    """
    return abs(observed - recorded) <= CREATE_TIME_TOLERANCE


def process_marker(pid: int, marker_var: str) -> tuple[str, str | None]:
    """The marker *marker_var* carried in the environment of the process at *pid*.

    ``("found", value_or_None)`` when the environment was read, ``("unknown",
    None)`` on a failed probe. A None value must never be read as "this process
    lacks the marker" — macOS returns an empty environment without raising for a
    protected system binary, so a declined disclosure and a genuinely absent
    marker are indistinguishable; absence of a marker may withhold ownership but
    can never assert a group is NOT a given run's.
    """
    import psutil

    try:
        return "found", psutil.Process(pid).environ().get(marker_var)
    except (psutil.Error, OSError, UnicodeDecodeError):
        return "unknown", None


def pinned_member(
    pid: int, pgid: int, *, marker_var: str
) -> tuple[str, tuple[int, float, str | None, bool] | None]:
    """Everything *pid* has to say as a member of *pgid*, read as one observation.

    ``("found", (pid, create_time, marker, marker_read))`` when a single process
    answered all of it, ``("gone", None)`` when the pid holds no live member of
    this group, and ``("unknown", None)`` when the reads could not be tied to one
    process. Bracketed by a start-time re-read to rule out pid reuse mid-read;
    see docs/internals/ln-primitives.md#process-group-identity for why.
    """
    state, created = process_create_time(pid)
    if state == "gone":
        return "gone", None
    if state != "found" or created is None:
        return "unknown", None
    try:
        in_group = os.getpgid(pid) == pgid
    except ProcessLookupError:
        return "gone", None
    except OSError:
        return "unknown", None
    marker_state, marker = process_marker(pid, marker_var)
    again, created_again = process_create_time(pid)
    if again != "found" or created_again != created:
        return "unknown", None
    if not in_group:
        return "gone", None
    return "found", (pid, created, marker, marker_state == "found")


def live_group_members(
    pgid: int, *, marker_var: str
) -> tuple[list[tuple[int, float, str | None, bool]], bool]:
    """Live members of process group *pgid*, and whether the scan was complete.

    Returns ``(members, complete)`` where each member is ``(pid, create_time,
    marker, marker_read)`` from :func:`pinned_member`. ``complete`` is False
    when a process's group/identity couldn't be read (the group may hold an
    unseen member) rather than silently dropping it; zombies are excluded, but
    a member whose marker alone couldn't be read still counts as seen.
    """
    import psutil

    members: list[tuple[int, float, str | None, bool]] = []
    complete = True
    try:
        pids = psutil.pids()
    except (psutil.Error, OSError):
        return [], False

    for pid in pids:
        if pid <= 1:
            continue
        try:
            if os.getpgid(pid) != pgid:
                continue
        except ProcessLookupError:
            continue
        except OSError:
            complete = False
            continue
        state, member = pinned_member(pid, pgid, marker_var=marker_var)
        if state == "found" and member is not None:
            members.append(member)
        elif state == "unknown":
            complete = False
    return members, complete


def group_member_pids(pgid: int) -> tuple[list[int], bool]:
    """Pids currently in group *pgid*, and whether the scan was complete.

    The marker-free membership read, for a caller asking only whether a group
    is empty. Not a cheaper :func:`live_group_members` with a field dropped —
    see docs/internals/ln-primitives.md#process-group-identity for why the
    marker has to be read inside the same bracket. An incomplete scan is never
    reported as emptiness.
    """
    import psutil

    members: list[int] = []
    complete = True
    try:
        pids = psutil.pids()
    except (psutil.Error, OSError):
        return [], False

    for pid in pids:
        if pid <= 1:
            continue
        state, created = process_create_time(pid)
        if state == "gone":
            continue
        if state != "found" or created is None:
            complete = False
            continue
        try:
            in_group = os.getpgid(pid) == pgid
        except ProcessLookupError:
            continue
        except OSError:
            complete = False
            continue
        again, created_again = process_create_time(pid)
        if again != "found" or created_again != created:
            complete = False
            continue
        if in_group:
            members.append(pid)
    return members, complete


def safe_pgid_value(pgid: Any) -> int | None:
    """The group id it is safe to signal, or None.

    Refuses pid<=1 (init, and the value a bad process double reports) and this
    process's own group, which is the difference between ending a child's group
    and ending the caller.
    """
    if not (hasattr(os, "killpg") and hasattr(os, "getpgrp") and isinstance(pgid, int)):
        return None
    if pgid <= 1:
        return None
    try:
        if pgid == os.getpgrp():
            return None
    except OSError:
        return None
    return pgid


def kill_group_now(pgid: Any) -> bool:
    """SIGKILL a process group, synchronously. Returns whether it was signalled.

    No await anywhere in here, which is the point: it is the backstop for paths
    whose graceful cleanup can itself be cancelled, and a backstop that can be
    interrupted is not one.
    """
    resolved = safe_pgid_value(pgid)
    if resolved is None:
        return False
    try:
        os.killpg(resolved, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _safe_pgid(proc: Any) -> int | None:
    """Return the process-group id to signal, or None when unsafe."""
    pid = getattr(proc, "pid", None)
    # pid==1 is init/session leader (would SIGKILL the harness itself; also
    # catches MagicMock.pid==1) — never signal it or our own group.
    if not (hasattr(os, "killpg") and hasattr(os, "getpgrp") and isinstance(pid, int) and pid > 1):
        return None
    try:
        if pid == os.getpgrp():
            return None
    except OSError:
        return None
    return pid


def terminate_process_group(
    proc: Any,
    *,
    grace: float | None = None,
    sig_first: signal.Signals = signal.SIGTERM,
) -> None:
    """Send sig_first to the process group AND the direct child; grace=None sends
    SIGKILL immediately (see aterminate_process_group for the full escalate cycle)."""
    pgid = _safe_pgid(proc)
    if grace is None:
        # Signal group AND direct child: proc.kill() is normally a no-op (child is
        # in the killed group) but prevents orphaning it when killpg is unavailable.
        if pgid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        return
    # sig_first only; caller drives the wait + SIGKILL escalation.
    if pgid is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, sig_first)
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()


async def aterminate_process_group(
    proc: Any,
    *,
    grace: float | None = None,
    sig_first: signal.Signals = signal.SIGTERM,
) -> None:
    """Async: signal the process group AND the direct child, wait up to grace, then
    SIGKILL; grace=None sends SIGKILL immediately with no prior signal."""
    pgid = _safe_pgid(proc)
    if grace is None:
        # No prior SIGTERM/wait: signal group AND direct child directly.
        if pgid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        return
    if pgid is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, sig_first)
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    # Bound the grace wait with an anyio cancel scope, not asyncio.wait_for:
    # wait_for raises "no running event loop" on an AnyIO/Trio task before the
    # timeout policy can apply, so the forced-kill escalation never fires.
    with move_on_after(grace) as scope:
        await proc.wait()
    if scope.cancelled_caught:
        if pgid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
