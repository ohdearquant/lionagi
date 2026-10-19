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
held even when nobody holds its lock.

When a pool is full the admission waits, re-trying every two seconds, for up to
``LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS`` (600 by default, ``0`` refuses at once),
then raises :class:`SubagentAdmissionRefusedError` naming the pool, its cap and
the pids holding it. A cap or wait that does not read as a whole number, or a
pool directory that cannot be created or written, refuses the same way rather
than letting the agent start uncounted.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import anyio

from lionagi.ln._proc import process_create_time, start_time_matches

from ._provider_errors import SubagentAdmissionRefusedError

# A slot is an flock; without one (Windows) an uncapped agent runs as before and a
# configured cap refuses, since nothing could hold the slot it would count.
if sys.platform == "win32":  # pragma: no cover - POSIX is what CI runs
    _fcntl = None
else:
    import fcntl as _fcntl

__all__ = (
    "ADMIT_WAIT_ENV",
    "DEFAULT_ADMIT_WAIT_SECONDS",
    "DEFAULT_WORKLOAD",
    "HEAVY_CAP_ENV",
    "TOTAL_CAP_ENV",
    "WORKLOADS",
    "SubagentAdmissionRefusedError",
    "SubagentLease",
    "SubagentWorkload",
    "admit_subagent",
    "engine_cap_env",
    "subagent_admission_status",
)

TOTAL_CAP_ENV = "LIONAGI_MAX_SUBAGENTS"
HEAVY_CAP_ENV = "LIONAGI_MAX_SUBAGENTS_HEAVY"
ADMIT_WAIT_ENV = "LIONAGI_SUBAGENT_ADMIT_WAIT_SECONDS"
DEFAULT_ADMIT_WAIT_SECONDS = 600.0
SubagentWorkload = Literal["light", "heavy"]
WORKLOADS: tuple[str, ...] = ("light", "heavy")
DEFAULT_WORKLOAD = "heavy"

_POLL_SECONDS = 2.0
# A slot locked with no record is being taken or counted by another process right now;
# the pool is retried at once this many times before it is read as full.
_CONTENTION_RETRIES = 25
_TOTAL_POOL = "all"
_HEAVY_POOL = "heavy"
_SLOT_SUFFIX = ".lock"


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
    """``(pool, variable, cap)`` for every capped pool this agent counts against.

    Narrowest first, so every process takes pools in one order.
    """
    pools: list[tuple[str, str, int]] = []
    if engine:
        name = engine_cap_env(engine)
        pools.append((engine.lower(), name, _read_whole_number(environ, name)))
    if workload == _HEAVY_POOL:
        pools.append((_HEAVY_POOL, HEAVY_CAP_ENV, _read_whole_number(environ, HEAVY_CAP_ENV)))
    pools.append((_TOTAL_POOL, TOTAL_CAP_ENV, _read_whole_number(environ, TOTAL_CAP_ENV)))
    return [p for p in pools if p[2] > 0]


@dataclass
class _Slot:
    pool: str
    path: Path
    fd: int
    record: dict[str, Any]

    def write(self) -> None:
        data = json.dumps(self.record, sort_keys=True).encode()
        os.ftruncate(self.fd, 0)
        os.pwrite(self.fd, data, 0)

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


@dataclass
class SubagentLease:
    """The slots one CLI agent holds; release them once its process group has ended."""

    slots: list[_Slot] = field(default_factory=list)

    def record_child(self, pid: int, create_time: float | None) -> None:
        """Name the child in every slot, so a slot outlives a holder that dies first."""
        for slot in self.slots:
            if slot.fd < 0:
                continue
            slot.record["child_pid"] = pid
            slot.record["child_started"] = create_time
            with contextlib.suppress(OSError):
                slot.write()

    def release(self) -> None:
        slots, self.slots = self.slots, []
        for slot in reversed(slots):
            slot.release()


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as handle:
            loaded = json.loads(handle.read(4096) or b"null")
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _recorded_child_runs(record: dict[str, Any] | None) -> bool:
    """Whether the slot's recorded child is still the process that was recorded.

    An unreadable process table counts as running: a slot is never handed out on a
    probe that could not look.
    """
    if not record:
        return False
    pid = record.get("child_pid")
    started = record.get("child_started")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    state, observed = process_create_time(pid)
    if state == "unknown":
        return True
    if state == "gone" or observed is None:
        return False
    if not isinstance(started, (int, float)) or isinstance(started, bool):
        return True
    return start_time_matches(observed, float(started))


def _open_slot_file(path: Path) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)


def _try_slot(path: Path) -> tuple[int | None, dict[str, Any] | None]:
    """Lock *path* if it is free: ``(fd, previous record)``, or ``(None, holder record)``."""
    fd = _open_slot_file(path)
    try:
        _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None, _read_record(path)
    except BaseException:
        os.close(fd)
        raise
    record = _read_record(path)
    if _recorded_child_runs(record):
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        os.close(fd)
        return None, record
    return fd, record


def _held_by_others(pool_dir: Path, own: Path | None) -> list[dict[str, Any]]:
    """The records of every held slot in *pool_dir* other than *own*, at every index."""
    held: list[dict[str, Any]] = []
    for path in sorted(pool_dir.glob(f"*{_SLOT_SUFFIX}")):
        if own is not None and path == own:
            continue
        fd, record = _try_slot(path)
        if fd is None:
            held.append(record or {})
            continue
        _fcntl.flock(fd, _fcntl.LOCK_UN)
        os.close(fd)
    return held


def _take_pool_slot(
    pool: str, cap: int, holder: dict[str, Any]
) -> tuple[_Slot | None, list[dict[str, Any]]]:
    """A slot under *cap* in *pool*, or ``None`` and the holders that fill it."""
    pool_dir = _pools_root() / pool
    pool_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for index in range(cap):
        path = pool_dir / f"{index}{_SLOT_SUFFIX}"
        fd, _ = _try_slot(path)
        if fd is None:
            continue
        slot = _Slot(pool, path, fd, dict(holder))
        try:
            # Written first, so a process counting this pool meanwhile names this holder.
            slot.write()
            # Every held slot counts, not only those below this process's cap: another
            # process may read a larger cap from its own environment and hold a higher index.
            others = _held_by_others(pool_dir, path)
        except BaseException:
            slot.release()
            raise
        if len(others) >= cap:
            slot.release()
            return None, others
        return slot, []
    return None, _held_by_others(pool_dir, None)


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
        return SubagentLease()
    if _fcntl is None:
        raise SubagentAdmissionRefusedError(
            f"subagent admission refused: {', '.join(v for _, v, _ in caps)} set, but this "
            "platform has no flock to hold a slot with; unset the cap to run uncounted"
        )
    wait = _read_wait(environ)
    deadline = time.monotonic() + wait
    holder = {
        "holder_pid": os.getpid(),
        "engine": engine,
        "workload": workload,
        "since": time.time(),
        "child_pid": None,
        "child_started": None,
    }
    contended = 0
    while True:
        lease = SubagentLease()
        full: tuple[str, str, int, list[dict[str, Any]]] | None = None
        try:
            for pool, variable, cap in caps:
                slot, holders = _take_pool_slot(pool, cap, holder)
                if slot is None:
                    full = (pool, variable, cap, holders)
                    break
                lease.slots.append(slot)
        except OSError as exc:
            lease.release()
            raise SubagentAdmissionRefusedError(
                f"subagent admission refused: the slot directory {_pools_root()} "
                f"cannot be used ({type(exc).__name__}: {exc.strerror or exc})"
            ) from exc
        except BaseException:
            lease.release()
            raise
        if full is None:
            return lease
        lease.release()
        pool, variable, cap, holders = full
        # A holder with no record is a slot locked for an instant: a status read counting
        # the pool, or an admission between its lock and its write. Not a full pool, so it
        # is retried at once, whatever the wait, rather than refused or slept on.
        if contended < _CONTENTION_RETRIES and any(
            not isinstance(r.get("holder_pid"), int) for r in holders
        ):
            contended += 1
            await anyio.sleep(random.uniform(0.005, 0.05))  # noqa: S311 (retry spacing)
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            what = f"{engine} " if engine else ""
            raise SubagentAdmissionRefusedError(
                f"subagent admission refused: {workload} {what}agent waited {wait:g}s; "
                f"pool '{pool}' is at its cap of {cap} ({variable}), "
                f"held by pid {_holder_text(holders)}",
                pool=pool,
                cap=cap,
                holders=tuple(r.get("holder_pid") for r in holders),
            )
        # Jittered, so two processes that collide on the last slot do not retry in step.
        jitter = random.uniform(0.75, 1.25)  # noqa: S311 (retry spacing, not a secret)
        await anyio.sleep(min(remaining, _POLL_SECONDS * jitter))


def subagent_admission_status(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The caps read from *environ* and every pool's cap and held slots, for a status display.

    Read-only on the slots: a free slot is locked and unlocked to tell it from a held
    one, and no record is written. A cap of 0 means the pool has none.
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
            pool = name[len(engine_prefix) :].lower()
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
        if pool in on_disk:
            try:
                held = _held_by_others(root / pool, None)
            except OSError as exc:
                entry["error"] = type(exc).__name__
                continue
        entry["held"] = len(held)
        entry["holders"] = [
            {k: r.get(k) for k in ("holder_pid", "child_pid", "engine", "workload", "since")}
            for r in held
        ]
    return {"root": str(root), "caps": caps, "pools": pools}
