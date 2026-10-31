# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Machine-wide admission for CLI agent subprocesses.

Every CLI provider starts its child through ``ndjson_from_cli``, which takes a
lease here first. A lease is one slot in each pool that has a cap; a slot is a
file under ``LIONAGI_HOME/leases/subagents/<pool>/<index>.lock`` held with an
exclusive ``flock`` by the spawning process. Every lionagi process that shares
``LIONAGI_HOME`` therefore counts against the same pools, and a slot frees
itself when its holder exits, however it exits.

Pools, each capped by an environment variable read at every admission (unset,
empty or ``0`` means no cap):

- ``all``: every CLI agent, ``LIONAGI_MAX_SUBAGENTS``;
- ``heavy``: agents whose workload is ``heavy``, ``LIONAGI_MAX_SUBAGENTS_HEAVY``;
- ``<engine>``: one engine's agents, ``LIONAGI_MAX_SUBAGENTS_<ENGINE>``.

The lock belongs to the spawning process, never to the child: Python opens the
file non-inheritable, so a grandchild that daemonizes cannot keep a slot after
the agent ends. Because the holder can die while its child, which leads its own
session, keeps running, the child's pid and start time are written into the slot
and a slot whose recorded child is still the same running process counts as
held even when nobody holds its lock. A child whose start time could not be read
takes no slot, since a bare pid could name a reused one; under a cap its spawn is refused
and the child is killed, while any processes it had started are left running, since
nothing shows its process group is still its own.

Counting a pool and taking its slot happen under that pool's admission lock
(``<pool>/.admission``), and an agent takes the admission locks of all its pools,
in one order, before it counts any: it is admitted to every pool or to none, so
two admissions never read each other's half-taken slots as holders, and an agent
waiting on one pool holds no slot in another. Each slot records the cap its holder
read, and a pool is full at the smallest cap among the agent's own and those its
holders recorded, so a process reading a larger cap cannot run past a smaller one.

When a pool is full the admission waits, re-trying every two seconds, for up to
``LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS`` (600 by default, ``0`` refuses at once),
then raises :class:`SubagentAdmissionRefusedError` naming the pool, its cap and
the pids holding it. A pool that is full only while a holder is between clearing its
record and unlocking is re-read within milliseconds, up to 25 times, before any
wait. A cap or wait that does not read as a whole number, an engine name that cannot
name a pool, or a pool directory that cannot be created or written, refuses the same
way rather than letting the agent start uncounted.

A CLI agent lionagi started cannot start another through lionagi: the spawning
process of such an agent would hold its slots while it waited on the child, and
nothing would bound how deep agents start agents. :func:`refuse_nested_agent` is
that check; the spawn calls it before any admission, so a nested agent is refused at
once rather than after the wait. An agent started at depth 0 may start others, and it
holds its slots while its own children wait too, so an admission that finds a pool full
of slots held for its own ancestors is refused at once as well.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import random
import re
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from lionagi._provider_errors import NestedAgentRefusedError, SubagentAdmissionRefusedError
from lionagi.ln._proc import process_create_time, start_time_matches
from lionagi.ln.concurrency import sleep as _sleep

# A slot is an flock; without one (Windows) an uncapped agent runs as before and a
# configured cap refuses, since nothing could hold the slot it would count.
if sys.platform == "win32":  # pragma: no cover - POSIX is what CI runs
    _fcntl = None
else:
    import fcntl as _fcntl

__all__ = (
    "ADMIT_WAIT_ENV",
    "AGENT_DEPTH_ENV",
    "DEFAULT_ADMIT_WAIT_SECONDS",
    "DEFAULT_WORKLOAD",
    "HEAVY_CAP_ENV",
    "TOTAL_CAP_ENV",
    "WORKLOADS",
    "NestedAgentRefusedError",
    "SubagentAdmissionRefusedError",
    "SubagentLease",
    "SubagentWorkload",
    "admit_subagent",
    "child_agent_depth",
    "engine_cap_env",
    "refuse_nested_agent",
    "set_child_agent_depth",
    "subagent_admission_status",
)

TOTAL_CAP_ENV = "LIONAGI_MAX_SUBAGENTS"
HEAVY_CAP_ENV = "LIONAGI_MAX_SUBAGENTS_HEAVY"
ADMIT_WAIT_ENV = "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS"
DEFAULT_ADMIT_WAIT_SECONDS = 600.0
# How many lionagi-started agents deep a process runs. `li agent` and the flow, fan-out and
# play workers stamp it, and every CLI spawn sets it on the child over any inherited value: 1,
# or 0 for a profile in LIONAGI_SEAT_PROFILES. A process carrying 1 or more is inside an agent.
AGENT_DEPTH_ENV = "LIONAGI_AGENT_DEPTH"


def _parse_agent_depth(raw: str | None) -> int:
    try:
        value = int(raw) if raw is not None else 0
    except ValueError:
        return 0
    return max(value, 0)


# Read once, at import: `li agent` sets its own copy to 1 before it starts its child, so a
# live read at the spawn would refuse every `li agent` as nested inside itself.
_INHERITED_AGENT_DEPTH = _parse_agent_depth(os.environ.get(AGENT_DEPTH_ENV))


def _ancestor_agent_depth() -> tuple[int, int | None]:
    """The deepest agent depth any ancestor of this process started with, and that
    ancestor's pid (None when no ancestor carries one).

    Read from each ancestor's environment as it was when it started, not from this
    process's own, because a CLI agent may start its tools with an environment of its own
    choosing: codex starts an MCP server with only the variables its config names, so a
    `li agent` that server runs inherits no depth, while the codex process above it does.
    An ancestor whose environment cannot be read (another user's, one that exited, or on
    macOS a platform binary such as /bin/sh, whose environment the kernel withholds) is
    passed over. The agent a CLI provider starts is not such a binary, so the process that
    carries the depth stays readable; the machine-wide caps bound the machine regardless.
    """
    import psutil

    deepest, at = 0, None
    try:
        process = psutil.Process().parent()
    except psutil.Error:
        return deepest, at
    seen: set[int] = set()
    while process is not None and process.pid > 1 and process.pid not in seen:
        seen.add(process.pid)
        try:
            depth = _parse_agent_depth(process.environ().get(AGENT_DEPTH_ENV))
        except psutil.Error:
            depth = 0
        if depth > deepest:
            deepest, at = depth, process.pid
        try:
            process = process.parent()
        except psutil.Error:
            break
    return deepest, at


def _ancestor_pids() -> frozenset[int]:
    """The pids of this process's ancestors below pid 1; the walk ends at one it cannot read."""
    import psutil

    pids: set[int] = set()
    try:
        process = psutil.Process().parent()
    except psutil.Error:
        return frozenset()
    while process is not None and process.pid > 1 and process.pid not in pids:
        pids.add(process.pid)
        try:
            process = process.parent()
        except psutil.Error:
            break
    return frozenset(pids)


def _held_for_ancestor(record: dict[str, Any], ancestors: frozenset[int]) -> bool:
    # The recorded child counts only while it is still the process recorded: a pid an
    # ancestor took over after that child ended does not make the slot the ancestor's.
    pids = [record.get("holder_pid")]
    if _recorded_child_runs(record):
        pids.append(record.get("child_pid"))
    return any(
        isinstance(pid, int) and not isinstance(pid, bool) and pid in ancestors for pid in pids
    )


def refuse_nested_agent(engine: str | None = None) -> None:
    """Refuse when this process runs inside a CLI agent lionagi started.

    Inside means this process inherited an agent depth of 1 or more, or one of its
    ancestors started with one. The machine-wide caps bound how many agents run; this
    bounds how deep they start each other, and it applies whether or not a cap is set.
    """
    depth, at = _INHERITED_AGENT_DEPTH, None
    if depth < 1:
        depth, at = _ancestor_agent_depth()
    if depth >= 1:
        what = f"a {engine} agent" if engine else "another agent"
        where = f"pid {at}" if at is not None else "this process"
        raise NestedAgentRefusedError(
            f"nested agent refused: this process runs inside an agent lionagi started "
            f"({where} has {AGENT_DEPTH_ENV}={depth}), and such an agent cannot start "
            f"{what} through lionagi; start it from the session that started that agent"
        )


# The depth every CLI agent this process starts is handed: 1, unless `li agent` launched a
# LIONAGI_SEAT_PROFILES profile here and set 0, so that agent may start others. Never taken from
# the environment this process inherited: an interactive shell can carry 0, and an agent a
# program in that shell starts through the Python API must still refuse to start another.
_child_agent_depth = 1


def set_child_agent_depth(depth: int) -> None:
    """Hand ``depth`` to every CLI agent this process starts from here on, and set it in
    this process's environment for anything else it starts."""
    global _child_agent_depth
    _child_agent_depth = depth
    os.environ[AGENT_DEPTH_ENV] = str(depth)


def child_agent_depth() -> int:
    """The depth the next CLI agent this process starts is handed."""
    return _child_agent_depth


SubagentWorkload = Literal["light", "heavy"]
WORKLOADS: tuple[str, ...] = ("light", "heavy")
DEFAULT_WORKLOAD = "heavy"

_POLL_SECONDS = 2.0
# An admission lock is held only while one process counts and takes a pool's slots, or
# a status read counts them; one held past this is refused rather than waited on.
_ADMISSION_LOCK_SECONDS = 5.0
_TOTAL_POOL = "all"
_HEAVY_POOL = "heavy"
_SLOT_SUFFIX = ".lock"
_ADMISSION_LOCK = ".admission"
# How many quick re-reads a pool gets while a holder is between clearing and unlocking.
_CONTENTION_RETRIES = 25
# A slot record is a few hundred bytes; one larger than this is not read as a record.
_RECORD_BYTES = 65536
_ENGINE_POOL_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")


# Told when an agent started in this context begins waiting for slots and when it is
# admitted; see _on_subagent_admission.
_ON_ADMISSION: ContextVar[Callable[[bool], None] | None] = ContextVar(
    "lionagi_subagent_on_admission", default=None
)


@contextlib.contextmanager
def _on_subagent_admission(callback: Callable[[bool], None]) -> Iterator[None]:
    """Call ``callback(False)`` when a CLI agent started in this context starts waiting for
    its slots, and ``callback(True)`` once it is admitted.

    A caller timing the agent's first output stops its clock for the wait and starts it
    at admission, so the time spent waiting for a slot is not read as a worker that
    never spoke.
    """
    token = _ON_ADMISSION.set(callback)
    try:
        yield
    finally:
        _ON_ADMISSION.reset(token)


def _tell_admission(admitted: bool) -> None:
    callback = _ON_ADMISSION.get()
    if callback is not None:
        callback(admitted)


def _admitted(lease: SubagentLease) -> SubagentLease:
    _tell_admission(True)
    return lease


def _engine_pool(engine: str) -> str | None:
    """The pool an engine counts in (``gemini-cli`` -> ``gemini_cli``), or None when the
    name cannot be one: a pool name is also a directory and the suffix of a variable."""
    pool = engine.lower().replace("-", "_")
    if not _ENGINE_POOL_RE.fullmatch(pool) or pool in (_TOTAL_POOL, _HEAVY_POOL):
        return None
    return pool


def engine_cap_env(engine: str) -> str:
    """The variable that caps one engine's pool, e.g. ``LIONAGI_MAX_SUBAGENTS_CODEX``."""
    return f"{TOTAL_CAP_ENV}_{engine.upper().replace('-', '_')}"


def _pools_root() -> Path:
    from lionagi import _paths

    return _paths.LIONAGI_HOME / "leases" / "subagents"


def _read_whole_number(environ: Mapping[str, str], name: str) -> int:
    raw = environ.get(name, "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value < 0:
        raise SubagentAdmissionRefusedError(
            f"subagent admission refused: {name}={raw!r} is not a whole number "
            "(0 or unset means no cap)"
        )
    return value


def _read_wait(environ: Mapping[str, str]) -> float:
    raw = environ.get(ADMIT_WAIT_ENV, "").strip()
    if not raw:
        return DEFAULT_ADMIT_WAIT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        value = -1.0
    if not value >= 0 or value == float("inf"):
        raise SubagentAdmissionRefusedError(
            f"subagent admission refused: {ADMIT_WAIT_ENV}={raw!r} is not a number of "
            "seconds (0 refuses without waiting)"
        )
    return value


def _caps(
    environ: Mapping[str, str], engine: str | None, workload: str
) -> list[tuple[str, str, int]]:
    """``(pool, variable, cap)`` for every capped pool this agent counts against, sorted by
    pool name: every process takes the admission locks in that one order."""
    pools: list[tuple[str, str, int]] = []
    if engine:
        pool = _engine_pool(engine)
        if pool is None:
            raise SubagentAdmissionRefusedError(
                f"subagent admission refused: engine {engine!r} cannot name a pool "
                "(a letter, then up to 31 letters, digits, '_' or '-'; not 'all' or 'heavy')"
            )
        name = engine_cap_env(engine)
        pools.append((pool, name, _read_whole_number(environ, name)))
    if workload == _HEAVY_POOL:
        pools.append((_HEAVY_POOL, HEAVY_CAP_ENV, _read_whole_number(environ, HEAVY_CAP_ENV)))
    pools.append((_TOTAL_POOL, TOTAL_CAP_ENV, _read_whole_number(environ, TOTAL_CAP_ENV)))
    return sorted(p for p in pools if p[2] > 0)


@dataclass
class _Slot:
    pool: str
    path: Path
    fd: int
    record: dict[str, Any]
    # Whether the file holds this child's whole record with its start time: only then
    # does the record keep the slot held without its lock.
    holds_child: bool = False

    def write(self) -> None:
        data = json.dumps(self.record, sort_keys=True).encode()
        # Written, then cut to length: a reader never finds the file empty while it is held.
        written = os.pwrite(self.fd, data, 0)
        if written != len(data):
            # What the file then holds may not parse, may parse to a record naming
            # another child or this child with another start time, or may by chance
            # parse to this child with its exact start time. Only the last keeps the
            # slot held for this child once its holder exits, so every short write
            # is refused.
            raise OSError(errno.ENOSPC, f"wrote {written} of the record's {len(data)} bytes")
        os.ftruncate(self.fd, len(data))

    def release(self) -> None:
        if self.fd < 0:
            return
        fd, self.fd = self.fd, -1
        try:
            # Cleared before unlocking, so a released slot never names a child.
            with contextlib.suppress(OSError):
                os.ftruncate(fd, 0)
            with contextlib.suppress(OSError):
                _fcntl.flock(fd, _fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def unlock_keeping_record(self) -> None:
        if self.fd < 0:
            return
        fd, self.fd = self.fd, -1
        try:
            with contextlib.suppress(OSError):
                _fcntl.flock(fd, _fcntl.LOCK_UN)
        finally:
            os.close(fd)


@dataclass
class SubagentLease:
    """The slots one CLI agent holds; release them once its process group has ended."""

    slots: list[_Slot] = field(default_factory=list)

    def record_child(self, pid: int, create_time: float | None) -> None:
        """Name the child in every slot, so a slot outlives a holder that dies first.

        A slot that cannot be written raises :class:`SubagentAdmissionRefusedError`: it would
        read free once its holder exited while the child still ran.
        """
        for slot in self.slots:
            if slot.fd < 0:
                continue
            slot.record["child_pid"] = pid
            slot.record["child_started"] = create_time
            slot.holds_child = False
            try:
                slot.write()
            except OSError as exc:
                raise SubagentAdmissionRefusedError(
                    f"subagent admission refused: the {slot.pool!r} slot could not record "
                    f"the agent ({exc}), so it would read free once this process exited; "
                    "the agent was ended",
                    pool=slot.pool,
                ) from exc
            slot.holds_child = create_time is not None

    def release(self) -> None:
        slots, self.slots = self.slots, []
        for slot in reversed(slots):
            slot.release()

    def keep_for_child(self) -> bool:
        """Give up the locks of a child whose group is not known to have ended, keeping
        each slot's record, so the slots read held while that child runs and free once it
        has gone, as after this process exits.

        Returns False, keeping the locks until this process exits, when any slot lacks the
        child's whole record with its start time: that record would not hold it.
        """
        if not all(slot.holds_child for slot in self.slots if slot.fd >= 0):
            return False
        slots, self.slots = self.slots, []
        for slot in reversed(slots):
            slot.unlock_keeping_record()
        return True


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            data = handle.read(_RECORD_BYTES + 1)
        # The extra byte only tells a file past the limit from one exactly at it.
        loaded = json.loads(data or b"null") if len(data) <= _RECORD_BYTES else None
    except (OSError, ValueError, RecursionError):
        # RecursionError: a file of nested brackets within the limit. The writer never
        # produces one; it reads as no record, like any other text that is not one.
        return None
    return loaded if isinstance(loaded, dict) else None


def _recorded_child_runs(record: dict[str, Any] | None) -> bool:
    """Whether the slot's recorded child is still the process that was recorded.

    A record without the child's start time does not count: the pid alone could name a
    process that reused it. An unreadable process table counts as running: a slot is
    never handed out on a probe that could not look.
    """
    if not record:
        return False
    pid = record.get("child_pid")
    started = record.get("child_started")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if not isinstance(started, (int, float)) or isinstance(started, bool):
        return False
    state, observed = process_create_time(pid)
    if state == "unknown":
        return True
    if state == "gone" or observed is None:
        return False
    return start_time_matches(observed, float(started))


def _recorded_cap(record: dict[str, Any]) -> int | None:
    cap = record.get("cap")
    if isinstance(cap, int) and not isinstance(cap, bool) and cap > 0:
        return cap
    return None


def _open_slot_file(path: Path) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)


def _lock_now(fd: int) -> bool:
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _admission_lock_held_too_long(pool: str) -> SubagentAdmissionRefusedError:
    return SubagentAdmissionRefusedError(
        f"subagent admission refused: the admission lock of pool '{pool}' stayed held for "
        f"{_ADMISSION_LOCK_SECONDS:g}s"
    )


async def _lock_admission(pool_dir: Path) -> int:
    """Hold *pool_dir*'s admission lock; the fd to close when the pool is counted."""
    fd = _open_slot_file(pool_dir / _ADMISSION_LOCK)
    try:
        deadline = time.monotonic() + _ADMISSION_LOCK_SECONDS
        while not _lock_now(fd):
            if time.monotonic() >= deadline:
                raise _admission_lock_held_too_long(pool_dir.name)
            await _sleep(random.uniform(0.005, 0.05))  # noqa: S311 (retry spacing)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _lock_admission_blocking(pool_dir: Path) -> int:
    """:func:`_lock_admission` for a synchronous status read."""
    fd = _open_slot_file(pool_dir / _ADMISSION_LOCK)
    try:
        deadline = time.monotonic() + _ADMISSION_LOCK_SECONDS
        while not _lock_now(fd):
            if time.monotonic() >= deadline:
                raise _admission_lock_held_too_long(pool_dir.name)
            time.sleep(0.01)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _unlock_admission(fd: int) -> None:
    # Unlocked, then closed, as a slot is released; the file stays, so the next
    # admission locks the same one.
    try:
        with contextlib.suppress(OSError):
            _fcntl.flock(fd, _fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_pool(pool_dir: Path) -> tuple[list[dict[str, Any]], set[str], int]:
    """Under the pool's admission lock: the records of its held slots, the names of its
    slot files that are free, and how many held slots had no record. Writes nothing.

    A slot is held while its holder has it locked, or while the child its record names
    still runs; a dead holder's record in a free slot is not counted. Every process that
    locks a slot to count or take it holds the admission lock, so a locked slot with no
    record is one whose holder has cleared it and not yet unlocked: still counted, and
    reported so the admission can look again in a moment.
    """
    held: list[dict[str, Any]] = []
    free: set[str] = set()
    unrecorded = 0
    for path in sorted(pool_dir.glob(f"*{_SLOT_SUFFIX}")):
        fd = _open_slot_file(path)
        try:
            if not _lock_now(fd):
                record = _read_record(path)
                if not record:
                    unrecorded += 1
                held.append(record or {})
                continue
            record = _read_record(path)
            if _recorded_child_runs(record):
                held.append(record or {})
            else:
                # Whatever record a free slot still has is overwritten when it is taken.
                free.add(path.name)
        finally:
            # Closing drops the lock taken above; the slot is taken later, if at all,
            # through a fresh fd.
            os.close(fd)
    return held, free, unrecorded


def _effective_cap(cap: int, held: list[dict[str, Any]]) -> int:
    recorded = [c for c in (_recorded_cap(r) for r in held) if c is not None]
    return min([cap, *recorded])


def _take_slot(pool: str, pool_dir: Path, free: set[str], holder: dict[str, Any]) -> _Slot | None:
    """Under the pool's admission lock, with room counted: lock the lowest-numbered slot
    no holder has and write this holder's record into it. None when that slot turned out
    to be locked, which only a process taking slots without the admission lock does."""
    index = 0
    while True:
        name = f"{index}{_SLOT_SUFFIX}"
        path = pool_dir / name
        if name in free or not path.exists():
            break
        index += 1
    fd = _open_slot_file(path)
    if not _lock_now(fd):
        os.close(fd)
        return None
    slot = _Slot(pool, path, fd, dict(holder))
    try:
        slot.write()
    except BaseException:
        slot.release()
        raise
    return slot


def _holder_text(records: list[dict[str, Any]]) -> str:
    parts = []
    for record in records:
        pid = record.get("holder_pid")
        child = record.get("child_pid")
        text = str(pid) if isinstance(pid, int) else "unknown"
        if isinstance(child, int):
            text += f" (child {child})"
        parts.append(text)
    return ", ".join(parts) or "unreadable"


def _full_text(cap: int, effective: int, variable: str) -> str:
    if effective < cap:
        return f"at a cap of {effective} a holder read ({variable}={cap} here)"
    return f"at its cap of {cap} ({variable})"


async def admit_subagent(
    *,
    engine: str | None,
    workload: str = DEFAULT_WORKLOAD,
    environ: Mapping[str, str] | None = None,
) -> SubagentLease:
    """Take one slot in every capped pool this agent counts against, waiting for room.

    Returns an empty lease when no pool is capped. Raises
    :class:`SubagentAdmissionRefusedError` when a pool stays full for the whole wait
    or when the caps, the wait or the pool directory cannot be read.
    """
    if workload not in WORKLOADS:
        raise SubagentAdmissionRefusedError(
            f"subagent admission refused: workload {workload!r} is not one of {', '.join(WORKLOADS)}"
        )
    environ = os.environ if environ is None else environ
    caps = _caps(environ, engine, workload)
    if not caps:
        return _admitted(SubagentLease())
    if _fcntl is None:
        raise SubagentAdmissionRefusedError(
            f"subagent admission refused: {', '.join(v for _, v, _ in caps)} set, but this "
            "platform has no flock to hold a slot with; unset the cap to run uncounted"
        )
    wait = _read_wait(environ)
    deadline = time.monotonic() + wait
    _tell_admission(False)
    holder = {
        "holder_pid": os.getpid(),
        "engine": engine,
        "workload": workload,
        "since": time.time(),
        "child_pid": None,
        "child_started": None,
    }
    quick_rereads = 0
    slept = False
    ancestors: frozenset[int] | None = None
    while True:
        lease = SubagentLease()
        locks: list[int] = []
        full: tuple[str, str, int, int, list[dict[str, Any]]] | None = None
        # The pool was full only because of holders between clearing a record and unlocking.
        clearing = False
        try:
            counted: list[tuple[str, str, Path, int, int, list[dict[str, Any]], set[str]]] = []
            for pool, _variable, _cap in caps:
                pool_dir = _pools_root() / pool
                pool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                locks.append(await _lock_admission(pool_dir))
            # Every admission lock is held: count every pool, and take a slot in each only
            # once all of them have room.
            for pool, variable, cap in caps:
                pool_dir = _pools_root() / pool
                held, free, unrecorded = _read_pool(pool_dir)
                effective = _effective_cap(cap, held)
                if len(held) >= effective:
                    # Slots held for this process's own ancestors free only when that
                    # ancestor ends, and it waits on this agent: no wait can free them.
                    if ancestors is None:
                        ancestors = _ancestor_pids()
                    own = [r for r in held if _held_for_ancestor(r, ancestors)]
                    if len(own) >= effective:
                        raise SubagentAdmissionRefusedError(
                            f"subagent admission refused: pool '{pool}' is "
                            f"{_full_text(cap, effective, variable)}, and the agent that "
                            f"started this one holds it (pid {_holder_text(own)}), which "
                            "frees it only once this agent ends; raise the cap or start "
                            "this agent from outside that one",
                            pool=pool,
                            cap=effective,
                            holders=tuple(r.get("holder_pid") for r in held),
                        )
                    full = (pool, variable, cap, effective, held)
                    # Worth a quick look again only if the recorded holders leave room.
                    clearing = unrecorded > 0 and len(held) - unrecorded < effective
                    break
                counted.append((pool, variable, pool_dir, cap, effective, held, free))
            if full is None:
                for pool, variable, pool_dir, cap, effective, held, free in counted:
                    slot = _take_slot(pool, pool_dir, free, {**holder, "cap": cap})
                    if slot is None:
                        # Counted free, found locked: looked at again like a clearing holder.
                        full = (pool, variable, cap, effective, held)
                        clearing = True
                        break
                    lease.slots.append(slot)
        except SubagentAdmissionRefusedError:
            lease.release()
            raise
        except OSError as exc:
            lease.release()
            raise SubagentAdmissionRefusedError(
                f"subagent admission refused: the slot directory {_pools_root()} "
                f"cannot be used ({type(exc).__name__}: {exc.strerror or exc})"
            ) from exc
        except BaseException:
            lease.release()
            raise
        finally:
            for fd in reversed(locks):
                _unlock_admission(fd)
        if full is None:
            return _admitted(lease)
        # All or none: a slot taken before a pool turned out full goes back.
        lease.release()
        # Quick re-reads stay inside the wait once it has slept, so even a wait of 0 gets one look.
        if (
            clearing
            and quick_rereads < _CONTENTION_RETRIES
            and (not slept or time.monotonic() < deadline)
        ):
            quick_rereads += 1
            await _sleep(random.uniform(0.001, 0.005))  # noqa: S311 (retry spacing)
            continue
        pool, variable, cap, effective, holders = full
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            what = f"{engine} " if engine else ""
            raise SubagentAdmissionRefusedError(
                f"subagent admission refused: {workload} {what}agent waited {wait:g}s; "
                f"pool '{pool}' is {_full_text(cap, effective, variable)}, held by pid "
                f"{_holder_text(holders)}",
                pool=pool,
                cap=effective,
                holders=tuple(r.get("holder_pid") for r in holders),
            )
        # Jittered, so two processes that collide on the last slot do not retry in step.
        jitter = random.uniform(0.75, 1.25)  # noqa: S311 (retry spacing, not a secret)
        await _sleep(min(remaining, _POLL_SECONDS * jitter))
        slept = True
        # A holder met after this sleep is a new one, with its own quick re-reads.
        quick_rereads = 0


def subagent_admission_status(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The caps read from *environ* and every pool's cap and held slots, for a status display.

    Writes no slot: each pool is counted under its admission lock, as an admission counts
    it, so the read and an admission never see each other's half-done work. A cap of 0
    means the pool has none; a variable whose suffix cannot name an engine's pool is
    reported under ``caps`` and counts in no pool.
    """
    environ = os.environ if environ is None else environ
    root = _pools_root()
    caps: dict[str, int | str] = {}
    pool_caps: dict[str, int | str] = {}
    engine_prefix = f"{TOTAL_CAP_ENV}_"
    names = {TOTAL_CAP_ENV, HEAVY_CAP_ENV} | {n for n in environ if n.startswith(engine_prefix)}
    for name in sorted(names):
        try:
            caps[name] = _read_whole_number(environ, name)
        except SubagentAdmissionRefusedError:
            caps[name] = "invalid"
        if name == TOTAL_CAP_ENV:
            pool = _TOTAL_POOL
        elif name == HEAVY_CAP_ENV:
            pool = _HEAVY_POOL
        else:
            pool = _engine_pool(name[len(engine_prefix) :])
            if pool is None:
                continue
        pool_caps[pool] = caps[name]
    if _fcntl is None:
        status: dict[str, Any] = {"root": str(root), "caps": caps, "pools": {}}
        if any(isinstance(v, str) or v > 0 for v in caps.values()):
            status["error"] = "no flock on this platform"
        return status
    on_disk = {p.name for p in root.iterdir() if p.is_dir()} if root.is_dir() else set()
    pools: dict[str, Any] = {}
    for pool in sorted(set(pool_caps) | on_disk):
        entry: dict[str, Any] = {"cap": pool_caps.get(pool, 0)}
        pools[pool] = entry
        held: list[dict[str, Any]] = []
        pool_dir = root / pool
        try:
            if pool in on_disk:
                lock = _lock_admission_blocking(pool_dir)
                try:
                    held, _free, _unrecorded = _read_pool(pool_dir)
                finally:
                    _unlock_admission(lock)
            elif pool_dir.exists() or pool_dir.is_symlink():
                # Something other than a directory sits where the pool's slots belong; an
                # admission to it is refused, so it is not reported as an empty pool.
                raise NotADirectoryError(str(pool_dir))
        except (OSError, SubagentAdmissionRefusedError) as exc:
            entry["error"] = type(exc).__name__
            continue
        entry["held"] = len(held)
        entry["holders"] = [
            {k: r.get(k) for k in ("holder_pid", "child_pid", "engine", "workload", "since", "cap")}
            for r in held
        ]
    return {"root": str(root), "caps": caps, "pools": pools}
