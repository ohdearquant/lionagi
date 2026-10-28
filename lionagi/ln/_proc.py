# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

from anyio.lowlevel import checkpoint_if_cancelled

from .concurrency import move_on_after, shield, sleep

# Two reads of a kernel tick clock for one process can differ in the last
# place, so a recorded start time is compared to a live one within this. Two
# LIVE reads of the same process must still compare exactly equal.
CREATE_TIME_TOLERANCE = 0.1
_PROCESS_GROUP_MARKER = "LIONAGI_INTERNAL_PROCESS_GROUP_TOKEN"


@dataclass(frozen=True)
class _GroupWitnesses:
    """Identity witnesses plus whether the group scan had an unreadable gap."""

    members: tuple[tuple[int, float], ...]
    group_anchor: bool = False
    # The handle whose liveness is the anchor's entire justification. Carried
    # so the claim can be re-checked when the anchor is USED, which is a
    # different moment from when it was captured: a leader that was live at
    # capture can be gone by the time something asks to signal the group, and
    # that is exactly when the PGID becomes free to reissue. Only set when
    # ``group_anchor`` is true; nothing else consults it.
    leader: Any = None

    def __iter__(self):
        return iter(self.members)

    def __len__(self) -> int:
        return len(self.members)

    def __bool__(self) -> bool:
        return bool(self.members)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, _GroupWitnesses):
            return (self.members, self.group_anchor) == (other.members, other.group_anchor)
        if isinstance(other, tuple):
            return self.members == other
        return NotImplemented


def _marker_matches(value: str | None, expected: str) -> bool:
    """Whether a process disclosed the marker assigned to this spawn."""
    return isinstance(value, str) and secrets.compare_digest(value, expected)


# Where a lionagi process finds its state store. A configured command (a scheduled command, a notify
# command, a tool hook) often runs a lionagi of its own, so it never inherits this from its parent:
# a lionagi on a newer store schema would open the parent's store through it and migrate the file.
STATE_DB_URL_ENV = "LIONAGI_STATE_DB_URL"


def env_without_state_db_url(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """*env* (default: this process's environment) without the state-store URL in any letter case,
    since the settings that read it match names case-insensitively."""
    source = os.environ if env is None else env
    return {k: v for k, v in source.items() if k.upper() != STATE_DB_URL_ENV}


def prepare_process_group_env(
    env: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], str]:
    """Return a child environment carrying a fresh, private spawn marker."""
    marker = secrets.token_urlsafe(32)
    prepared = dict(os.environ if env is None else env)
    prepared[_PROCESS_GROUP_MARKER] = marker
    return prepared, marker


def _select_raw_create_time_reader():
    """Choose, once, how to read a psutil ``Process``'s unadjusted start time.

    psutil >= 7.1 adjusts ``Process.create_time()`` on macOS by ``INIT_BOOT_TIME
    - boot_time()`` (``psutil._psosx.adjust_proc_create_time``), where
    ``INIT_BOOT_TIME`` is frozen at psutil's own import time. ``kern.boottime``
    moves under NTP clock slew, so that offset grows while a process runs: a
    value recorded at spawn stops matching a value read later for the SAME live
    process, and two different processes started at different times read as
    the same age apart rather than their real one. Passing ``monotonic=True``
    skips the adjustment, but the *public* ``Process.create_time()`` does not
    forward that keyword in any released psutil version — only the internal
    per-platform implementation accepts it. psutil's own PID-reuse detection
    (``Process._get_ident``) reaches that implementation directly
    (``self._proc.create_time(monotonic=True)``) for exactly this reason, so
    this does the same, falling back to the adjusted public call only if
    neither path is available.

    That skip is taken ONLY on a platform that actually applies the adjustment,
    because ``monotonic=True`` does not mean the same thing everywhere. On Linux
    (``psutil._pslinux``) ``create_time()`` is the process's ``starttime`` in
    jiffies plus ``boot_time()``, and ``monotonic`` merely omits that addition,
    yielding SECONDS SINCE BOOT rather than seconds since the epoch. Recording
    that would silently change what every stored ``pid_create_time`` means: live
    processes would stop matching their own stored records, a reused pid at a
    similar uptime after a reboot would start matching a stranger, and
    ``datetime.fromtimestamp`` would date a daemon to 1970.

    The plain epoch call is not stable on Linux either. ``boot_time()`` re-reads
    ``btime`` from ``/proc/stat`` on every call, and the kernel derives that
    whole-second value from the wall clock minus uptime, so a clock step or a
    slew across a second boundary moves it. The same live process then reads a
    second apart between its spawn record and a later check, far outside
    :data:`CREATE_TIME_TOLERANCE`. So on Linux the reader adds a boot time read
    ONCE per interpreter to the fixed boot-relative start: the value stays in
    seconds since the epoch, and every read of one process in this interpreter
    is equal. Two interpreters can still disagree by the drift between their
    first reads, as the plain call always could. A handle whose backend is not
    psutil's own Linux process keeps the public call, since nothing says its
    ``monotonic`` answer counts from boot.

    The adjustment is the thing being avoided there, so its presence is what
    selects that reader: ``adjust_proc_create_time`` exists only on the backends
    that apply it (macOS, and NetBSD inside psutil's BSD backend). Any other
    platform keeps the plain public call.
    """
    import inspect

    import psutil

    if getattr(psutil, "LINUX", False):
        try:
            platform_process = psutil._psplatform.Process
            if "monotonic" in inspect.signature(platform_process.create_time).parameters:
                boot = float(psutil._psplatform.boot_time())

                def read_linux(proc):
                    # Only psutil's own Linux backend counts from boot; any other
                    # handle (a stand-in, an adapter) answers in epoch seconds.
                    backend = getattr(proc, "_proc", None)
                    if isinstance(backend, platform_process):
                        return backend.create_time(monotonic=True) + boot
                    return proc.create_time()

                return read_linux
        except (TypeError, ValueError, AttributeError, OSError, RuntimeError):
            pass
        return lambda proc: proc.create_time()

    if not hasattr(psutil._psplatform, "adjust_proc_create_time"):
        return lambda proc: proc.create_time()

    try:
        if "monotonic" in inspect.signature(psutil.Process.create_time).parameters:
            return lambda proc: proc.create_time(monotonic=True)
    except (TypeError, ValueError):
        pass

    try:
        platform_process = psutil._psplatform.Process
        if "monotonic" in inspect.signature(platform_process.create_time).parameters:
            return lambda proc: proc._proc.create_time(monotonic=True)
    except (TypeError, ValueError, AttributeError):
        pass

    return lambda proc: proc.create_time()


_read_raw_create_time = None


def raw_create_time(proc) -> float:
    """The kernel-reported start time of *proc*, unadjusted for clock slew.

    Use this everywhere a start time is recorded or compared for process
    identity; see :func:`_select_raw_create_time_reader` for why the plain
    ``proc.create_time()`` is not comparable across processes or over time.

    The reader is selected on the first read rather than at import, so that
    importing this module — which the package does eagerly, on many paths that
    never read a start time — does not pay for importing psutil and inspecting
    two signatures.
    """
    global _read_raw_create_time

    if _read_raw_create_time is None:
        _read_raw_create_time = _select_raw_create_time_reader()
    return _read_raw_create_time(proc)


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
        return "found", raw_create_time(proc)
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


def observe_process_group_leader(pid: Any, *, expected_marker: str) -> float | None:
    """Bind a new session leader to a marker injected before its spawn.

    Start time alone cannot distinguish a child reaped before the first probe
    from a stranger that already reused its pid. The private marker supplies
    that pre-spawn fact; an unreadable or mismatched marker is no authority.
    """
    if (
        type(pid) is not int
        or pid <= 1
        or not isinstance(expected_marker, str)
        or not expected_marker
        or not hasattr(os, "getpgid")
    ):
        return None
    state, member = pinned_member(pid, pid, marker_var=_PROCESS_GROUP_MARKER)
    if state != "found" or member is None:
        return None
    _, created, marker, marker_read = member
    if (
        not marker_read
        or not isinstance(marker, str)
        or not secrets.compare_digest(marker, expected_marker)
    ):
        return None
    return created


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


def _pinned_group_identity(pid: int, pgid: int) -> tuple[str, tuple[int, float] | None]:
    """Pin *pid* to one start time while proving membership in *pgid*."""
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
    again, created_again = process_create_time(pid)
    if again != "found" or created_again != created:
        return "unknown", None
    if not in_group:
        return "gone", None
    return "found", (pid, created)


def _group_member_identities(
    pgid: int,
) -> tuple[list[tuple[int, float]], bool]:
    """Start-time-pinned identities currently in *pgid*, plus scan completeness."""
    import psutil

    members: list[tuple[int, float]] = []
    complete = True
    try:
        pids = psutil.pids()
    except (psutil.Error, OSError):
        return [], False

    for pid in pids:
        if pid <= 1:
            continue
        state, member = _pinned_group_identity(pid, pgid)
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
    members, complete = _group_member_identities(pgid)
    return [pid for pid, _ in members], complete


def _child_is_reaped(proc: Any) -> bool:
    """Whether a subprocess-shaped handle has recorded a terminal return code."""
    return isinstance(getattr(proc, "returncode", None), int)


def _capture_group_witnesses(
    proc: Any,
    pgid: int,
    *,
    expected_leader_created: float | None = None,
) -> tuple[tuple[int, float], ...]:
    """Capture group members while *proc* proves this is still its group.

    The leader brackets the process-table scan. If it disappears, changes
    identity, or leaves the group during that window, none of the scan can be
    tied to the child and no witness is returned.
    """
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int) or pid != pgid:
        return ()
    if _child_is_reaped(proc):
        return ()
    state, leader = _pinned_group_identity(pid, pgid)
    if state != "found" or leader is None:
        return ()
    if expected_leader_created is not None and not start_time_matches(
        leader[1], expected_leader_created
    ):
        return ()
    members, complete = _group_member_identities(pgid)
    if _child_is_reaped(proc):
        return ()
    again, leader_again = _pinned_group_identity(pid, pgid)
    if again != "found" or leader_again != leader:
        return ()
    return _GroupWitnesses(
        tuple(sorted({leader, *members})),
        group_anchor=not complete,
    )


def _group_has_witness(pgid: int, witnesses: tuple[tuple[int, float], ...]) -> bool:
    """Whether a member captured before signalling still pins *pgid*."""
    for pid, created in witnesses:
        state, member = _pinned_group_identity(pid, pgid)
        if state == "found" and member == (pid, created):
            return True
    return False


def capture_group_witnesses(
    proc: Any,
    *,
    expected_leader_created: float,
    expected_marker: str | None = None,
) -> tuple[tuple[int, float], ...]:
    """Freeze identities while the exact spawn-time leader still matches.

    The required ``expected_leader_created`` prevents a teardown-time caller
    from turning a recycled pid into fresh authority. Obtain it from
    :func:`observe_process_group_leader` after injecting the environment from
    :func:`prepare_process_group_env` before spawn. When ``expected_marker`` is
    supplied, a live descendant carrying that inherited marker can also pin the
    group after the leader has exited; the marker is what distinguishes that
    survivor from a later occupant of the numeric PGID.
    """
    pgid = _safe_pgid(proc)
    if pgid is None or not isinstance(expected_leader_created, (int, float)):
        return ()
    if isinstance(expected_marker, str) and expected_marker:
        members, _ = live_group_members(pgid, marker_var=_PROCESS_GROUP_MARKER)
        marked = [
            (pid, created)
            for pid, created, marker, marker_read in members
            if marker_read and _marker_matches(marker, expected_marker)
        ]
        if not marked:
            # Some Darwin environments deny a process-table listing even for
            # processes owned by this user. The verified spawn marker still
            # anchors the dedicated PGID; keep the leader identity as a
            # fallback witness and require a live group probe immediately
            # before signalling. A foreign or empty group therefore remains
            # outside this path.
            #
            # That argument holds only while the leader is still live, because
            # a live leader is what keeps the PGID from being handed out again.
            # Once it has been reaped the group can be empty and the number
            # reused, and the liveness probe would then answer for whoever
            # holds it now -- authorizing a SIGKILL against an unrelated group.
            # Nothing is left to witness ownership here, so claim nothing.
            # Checked again where the anchor is used, not only here. This test
            # settles whether an anchor may be MINTED; it cannot settle whether
            # one may be SPENT, because the leader can exit in between.
            if _child_is_reaped(proc):
                return ()
            return _GroupWitnesses(
                ((pgid, expected_leader_created),),
                group_anchor=True,
                leader=proc,
            )
        # While the leader is still live, its start time must also match the
        # spawn observation. After it has exited, the inherited marker on a
        # surviving group member is the remaining proof that this PGID is ours.
        if not _child_is_reaped(proc):
            leader = next(((pid, created) for pid, created in marked if pid == pgid), None)
            if leader is None or not start_time_matches(leader[1], expected_leader_created):
                return ()
        return _GroupWitnesses(tuple(sorted(set(marked))))
    return _capture_group_witnesses(
        proc,
        pgid,
        expected_leader_created=expected_leader_created,
    )


def signal_group_if_witnessed(
    pgid: Any,
    witnesses: tuple[tuple[int, float], ...],
    sig: signal.Signals,
) -> bool:
    """Signal *pgid* only when a frozen member identity still matches.

    The marker is checked where the witnesses are captured, so it is not taken
    again here: by this point the witness set either proves the group is ours,
    or there is nothing left that can prove it and nothing is signalled.
    """
    resolved = safe_pgid_value(pgid)
    if resolved is None or not witnesses:
        return False
    witnessed = _group_has_witness(resolved, witnesses)
    if not witnessed and getattr(witnesses, "group_anchor", False):
        # The live leader identity anchored this group when the scan turned out
        # to be unreadable. That anchor is worth something only for as long as
        # the leader is still live, because a live leader is the one thing
        # keeping the kernel from handing this PGID to somebody else. Once it
        # has been reaped the group can be empty and the number reissued, and a
        # liveness probe would then answer for whoever holds it now.
        #
        # So the anchor is re-checked HERE, at the moment it is spent. The
        # capture-time check cannot stand in for this one: an anchor exists
        # precisely to be used later, and "later" is when the leader exits.
        # Without this, killpg(pgid, 0) succeeding on a recycled PGID reads as
        # ownership and the SIGKILL below lands on an unrelated group.
        leader = getattr(witnesses, "leader", None)
        if leader is None or _child_is_reaped(leader):
            return False
        # Still ours by identity; a positive liveness probe is the remaining
        # requirement before using the numeric PGID.
        try:
            os.killpg(resolved, 0)
        except (ProcessLookupError, PermissionError, OSError):
            return False
        witnessed = True
    # A second liveness-probe escalation used to sit here, justified by process
    # enumeration being unavailable. It could not reach that case: witnesses
    # carrying group_anchor are handled above, and every other witness set was
    # built from an enumeration that succeeded. So it ran only when real,
    # marker-verified members had all stopped matching -- the group gone, its
    # PGID free to be reissued -- and turned a bare liveness probe on that
    # number into authority to SIGKILL whoever now holds it.
    if not witnessed:
        return False
    try:
        os.killpg(resolved, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


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
    group_witnesses: tuple[tuple[int, float], ...] | None = None,
    expected_marker: str | None = None,
) -> None:
    """End a child and, with spawn-bound witnesses, its process group.

    Omitting ``group_witnesses`` deliberately withholds group-signal authority:
    a witness captured only at teardown cannot distinguish the original child
    from a process that has since reused its PID. ``grace=None`` sends SIGKILL
    immediately; see :func:`aterminate_process_group` for the escalate cycle.

    ``expected_marker`` is accepted for call-site compatibility and is not
    read. The marker is verified where the witnesses are captured, so by the
    time it reaches here the witness set either proves the group is ours or
    nothing does; re-checking a name at signal time would add no authority.
    """
    pgid = _safe_pgid(proc)
    witnesses = group_witnesses or ()
    if grace is None:
        # Signal group AND direct child: proc.kill() is normally a no-op (child is
        # in the killed group) but prevents orphaning it when killpg is unavailable.
        # The child's own signal does not depend on the group signal succeeding.
        try:
            if pgid is not None:
                signal_group_if_witnessed(pgid, witnesses, signal.SIGKILL)
        finally:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
        return
    # sig_first only; caller drives the wait + SIGKILL escalation.
    try:
        signal_group_if_witnessed(pgid, witnesses, sig_first)
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()


# How often the polls below re-read state. The first look is fast, so
# terminating a cooperative child still looks instant; the interval then backs
# off, so waiting out a child that is taking its time costs little.
_POLL_MIN_SECONDS = 0.0005
_POLL_MAX_SECONDS = 0.02


def _group_has_members(pgid: int) -> bool:
    """Whether any process may remain in the group. Signal 0 tests delivery only.

    Only ``ProcessLookupError`` is evidence of an empty group. Everything else is
    a probe that failed to answer, including the permission case where something
    is plainly there but not ours to signal. A failed probe reported as "empty"
    would end grace early and skip the forced kill, and that is the one direction
    this must not fail in: the caller asked for the group to be gone, so an
    unreadable answer keeps the grace wait rather than ending it early. This is
    never identity evidence and must not authorize a group-wide signal.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


async def _await_child_exit(proc: Any) -> None:
    """Wait for the child itself to exit, not for everything holding its pipes.

    ``proc.wait()`` cannot be used for this. On some interpreters it completes
    only once every inherited pipe has closed, so a descendant that escaped with
    the child's stderr decides when the wait returns: the caller trying to give
    up on a process ends up bounded by a process it never started, and by the
    time it looks, evidence it was about to report as missing has been resolved
    behind its back. ``returncode`` is set when the child is reaped, whatever
    still holds a pipe.

    Objects that do not model ``returncode`` as a subprocess does (None until
    exit, then an int) fall back to ``wait()``, which is the only thing they
    offer.
    """
    rc = getattr(proc, "returncode", object())
    if not (rc is None or isinstance(rc, int)):
        await proc.wait()
        return
    delay = _POLL_MIN_SECONDS
    while proc.returncode is None:
        await sleep(delay)
        delay = min(delay * 2, _POLL_MAX_SECONDS)


async def aterminate_process_group(
    proc: Any,
    *,
    grace: float | None = None,
    sig_first: signal.Signals = signal.SIGTERM,
    group_witnesses: tuple[tuple[int, float], ...] | None = None,
    expected_marker: str | None = None,
) -> None:
    """End a child and, with spawn-bound witnesses, its process group.

    Omitting ``group_witnesses`` is fail-closed for group signalling and still
    cleans up the direct child. With witnesses, wait up to ``grace`` after the
    first signal and then escalate to SIGKILL; ``grace=None`` skips that wait.

    ``expected_marker`` is accepted for call-site compatibility and is not
    read. The marker is verified where the witnesses are captured, so by the
    time it reaches here the witness set either proves the group is ours or
    nothing does; re-checking a name at signal time would add no authority.
    """
    pgid = _safe_pgid(proc)
    witnesses = group_witnesses or ()
    if grace is None:
        # No prior SIGTERM/wait: signal group AND direct child directly. The
        # child's own signal does not depend on the group signal succeeding.
        try:
            if pgid is not None:
                signal_group_if_witnessed(pgid, witnesses, signal.SIGKILL)
        finally:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
        return
    # A group signal that raises ends the group's part of the cycle: the group is
    # neither waited on nor signalled again. The child still gets its grace and its
    # forced kill, and the error is raised once the child is dealt with.
    group_error: Exception | None = None
    try:
        signal_group_if_witnessed(pgid, witnesses, sig_first)
    except Exception as exc:
        group_error = exc
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
    group = pgid if group_error is None and witnesses else None
    # Bound the grace wait with an anyio cancel scope, not asyncio.wait_for:
    # wait_for raises "no running event loop" on an AnyIO/Trio task before the
    # timeout policy can apply, so the forced-kill escalation never fires.
    with move_on_after(grace) as scope:
        await _await_child_exit(proc)
        # The child is gone; the rest of grace belongs to whatever shared its
        # process group. Waiting on the child alone is what keeps a pipe holder
        # from deciding when the child's own status is known, but the forced
        # kill below exists for group members that ignored the first signal, and
        # reaching it by way of a stalled wait was incidental: that happened
        # only where wait() is pipe-bound, and only when a survivor happened to
        # be holding a pipe. Timing out here is what drives the escalation.
        while group is not None and _group_has_members(group):
            await sleep(_POLL_MAX_SECONDS)
    if scope.cancelled_caught:
        try:
            if group is not None:
                # A member captured while the live child anchored the group still
                # matches immediately before the signal. The final identity-read to
                # killpg gap cannot be closed with portable process-group APIs; see
                # docs/internals/ln-primitives.md#signalling-a-group.
                signal_group_if_witnessed(group, witnesses, signal.SIGKILL)
        finally:
            # The child's own kill does not depend on the group signal succeeding.
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
        # Bounded too: a caller that has already escalated to SIGKILL is done
        # negotiating, and the same pipe holder that can stall the wait above
        # can stall this one, with no timeout left to catch it.
        with move_on_after(grace), contextlib.suppress(Exception):
            await _await_child_exit(proc)
    if group_error is not None:
        raise group_error


async def _wait_proc(proc: Any, grace: float = 2.0) -> None:
    """Wait at most *grace* seconds for a killed child to exit; never raises an error."""
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=grace)


def _spawn_witnesses(
    proc: Any, leader_created: float | None, group_marker: str
) -> tuple[tuple[int, float], ...]:
    """Members that prove a spawned child's group is still the one it started.

    The spawn marker lets a surviving member stand in for a leader that has already
    exited, which is the usual case when the child returned while a process it started
    still holds its pipes.
    """
    if leader_created is None:
        return ()
    return capture_group_witnesses(
        proc,
        expected_leader_created=leader_created,
        expected_marker=group_marker,
    )


async def _end_spawned_group(proc: Any, leader_created: float | None, group_marker: str) -> None:
    """Kill a spawned child and the process group it leads, then wait for the child.

    The kill is sent before the first suspension point, so it reaches the group even
    when the caller's task is being cancelled. The wait is shielded from a cancelled
    scope; a second native cancellation can cut it short, and the event loop's child
    watcher still reaps the killed child.
    """
    group_witnesses = _spawn_witnesses(proc, leader_created, group_marker)
    await aterminate_process_group(proc, grace=None, group_witnesses=group_witnesses)
    await shield(_wait_proc, proc)


async def _spawn_in_group(
    argv: Sequence[str], env: dict[str, str], group_marker: str
) -> tuple[asyncio.subprocess.Process, float | None]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        env=env,
    )
    return proc, observe_process_group_leader(
        getattr(proc, "pid", None),
        expected_marker=group_marker,
    )


def _end_abandoned_spawn(group_marker: str, spawn: asyncio.Future) -> None:
    """Kill the group of a child whose caller was cancelled while it was being spawned.

    Runs as the spawn task's done-callback, after the caller has already unwound, so
    the kill is synchronous and the loop's child watcher reaps the child.
    """
    if spawn.cancelled() or spawn.exception() is not None:
        return
    proc, leader_created = spawn.result()
    terminate_process_group(
        proc,
        grace=None,
        group_witnesses=_spawn_witnesses(proc, leader_created, group_marker),
    )


async def communicate_in_process_group(
    argv: Sequence[str], stdin: bytes, *, timeout: float
) -> tuple[int | None, bytes, bytes]:
    """Run *argv* in a session of its own, feed it *stdin*, and return its exit and output.

    The child gets this process's environment without the state-store URL (``env_without_state_db_url``).

    Whatever ends the exchange early -- the timeout, a failed exchange, a cancelled
    caller, including one cancelled while the child is being started -- kills the
    child's whole process group before the exception leaves, so neither the command nor
    anything it started keeps running once it is given up on. The timeout raises the
    event loop's timeout error, ``asyncio.TimeoutError`` before Python 3.11.
    """
    child_env, group_marker = prepare_process_group_env(env_without_state_db_url())
    proc = None
    leader_created = None
    try:
        # Shielded: the OS has started the child before the spawn call returns, so a
        # cancellation landing inside it would otherwise lose the only handle to that
        # child's group. The done-callback kills the group once the handle arrives.
        spawn = asyncio.ensure_future(_spawn_in_group(argv, child_env, group_marker))
        try:
            proc, leader_created = await asyncio.shield(spawn)
        except BaseException:
            spawn.add_done_callback(partial(_end_abandoned_spawn, group_marker))
            raise
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(stdin), timeout=timeout
        )
    except BaseException:
        if proc is not None:
            await _end_spawned_group(proc, leader_created, group_marker)
            # A scope cancelled during the shielded wait is delivered here, so it is
            # not replaced by the error the caller turns this exception into.
            await checkpoint_if_cancelled()
        raise
    return proc.returncode, stdout_bytes, stderr_bytes
