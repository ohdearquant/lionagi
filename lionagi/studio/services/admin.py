# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import time
import uuid
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any, Literal, NamedTuple

import anyio
from fastapi import HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, bindparam, text
from sqlalchemy.exc import OperationalError as _SAOperationalError

from lionagi.cli._util import pid_alive as _pid_is_live
from lionagi.ln import now_utc
from lionagi.state.db import ADMIN_TRANSITION_TARGETS as _ADMIN_TRANSITION_TARGETS
from lionagi.state.db import state_db_known_absent
from lionagi.state.reasons import RunReasons, SessionReasons, validate_reason_code
from lionagi.state.session_naming import resolve_display_name

from ..registry import studio_route
from ._db import open_db as _open_db
from ._db import require_file_store, store_exists, store_path
from ._path_safety import public_path

_log = logging.getLogger(__name__)

# Fallback mapping for deprecated 'reason' field without reason_code.
_LEGACY_ADMIN_REASON_CODES: dict[str, str] = {
    "failed": RunReasons.FAILED_EXCEPTION,
    "aborted": RunReasons.ABORTED_USER,
    "cancelled": RunReasons.CANCELLED_SYSTEM,
}

PhantomReason = Literal["process_dead", "missing_artifacts", "stale_lock"]


class MaintenanceBody(BaseModel):
    """Request body for POST /api/admin/maintenance."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["vacuum", "checkpoint", "prune"] = Field(
        ...,
        description="DB maintenance action: 'vacuum', 'checkpoint', or 'prune'.",
    )


class PruneBody(BaseModel):
    session_ids: list[str] | None = None
    all_phantom: bool = False


class PruneOldDataBody(BaseModel):
    keep_days: int | None = Field(
        default=None, ge=1, description="Retain sessions newer than this many days"
    )


class TransitionBody(BaseModel):
    """Admin session transition; reason_code is preferred over deprecated reason."""

    session_ids: list[str] = Field(..., min_length=1)
    target_status: Literal["failed", "aborted", "cancelled"]
    reason_code: str | None = None
    reason_summary: str = ""
    evidence_refs: list[dict] = Field(default_factory=list)
    # Deprecated; kept for backwards compatibility.
    reason: str | None = Field(default=None, max_length=500)
    actor: str = Field(default="admin", max_length=64)


def db_health() -> dict[str, int | bool]:
    from .db_maintenance import get_db_size_alert

    db_path = Path(store_path())
    size_bytes = db_path.stat().st_size if db_path.exists() else 0
    wal_path = db_path.parent / (db_path.name + "-wal")
    wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
    # The same threshold /api/stats applies, read through the same helper. A
    # health payload that reports the size but not whether it is over the
    # limit leaves every reader to re-derive the limit, and the health view
    # that consumed this had no way to say "unhealthy" at all.
    size_alert, size_threshold_bytes = get_db_size_alert(size_bytes)
    return {
        "size_bytes": size_bytes,
        "wal_bytes": wal_bytes,
        "size_alert": size_alert,
        "size_threshold_bytes": size_threshold_bytes,
    }


# How long the store probe waits before calling the store slow. Well under any
# sensible caller timeout, so a slow verdict arrives as an answer rather than
# as the caller's own timeout.
STORE_PROBE_TIMEOUT_MS = 1000

# One row off an index the store already maintains (idx_sessions_updated). The
# probe must not be able to cause the failure it detects, so it reads no
# content, joins nothing, and its cost does not grow with the store.
_STORE_PROBE_SQL = "SELECT id FROM sessions ORDER BY updated_at DESC LIMIT 1"


async def store_probe(*, timeout_ms: int = STORE_PROBE_TIMEOUT_MS) -> dict[str, Any]:
    """Run a bounded indexed read against the store and report which of three
    things is true: it answered, it did not answer in time, or it could not be
    reached at all. Collapsing those into one boolean is what let a stalled
    daemon keep reporting healthy."""
    import aiosqlite

    from lionagi.ln import move_on_after
    from lionagi.ln.concurrency import CancelScope

    result: dict[str, Any] = {
        "status": "unavailable",
        "detail": "",
        "latency_ms": None,
        "timeout_ms": timeout_ms,
        "store_present": store_exists(),
        "checked_at": now_utc().isoformat(),
    }
    if not result["store_present"]:
        result["detail"] = f"no store at {public_path(Path(store_path()))}"
        return result

    started = time.perf_counter()
    timed_out = True
    # Closing is deliberately not left to `async with`. This connection runs a
    # worker thread, and closing it is itself an await; inside a scope that has
    # just been cancelled that await is cancelled too, so the thread survives
    # holding an open database and then tries to complete against an event loop
    # that has since closed. The probe exists to be cancelled — that is what a
    # slow verdict is — so its cleanup is the one part that must not be.
    conn = None
    try:
        # Connecting is shielded and sits outside the deadline. Opening a SQLite
        # file takes no database lock — the first statement does — so the connect
        # is not what a slow store makes slow, and bounding it buys nothing. What
        # bounding it costs is the only way out of the leak: a connect cancelled
        # midway leaves the driver's worker thread running while the connection
        # object it would be closed through is discarded, so the close below
        # returns at once and the thread outlives the probe unreachable. The
        # remaining way for this to hang is a filesystem that will not answer,
        # which the `store_present` check above already stands in front of.
        with CancelScope(shield=True):
            conn = aiosqlite.connect(store_path())
            db = await conn
        with move_on_after(timeout_ms / 1000) as scope:
            # The shared busy timeout is longer than this probe's own deadline,
            # so waiting it out would outlast the answer; the probe would rather
            # report "slow" than sit in SQLite's retry loop.
            await db.execute(f"PRAGMA busy_timeout = {timeout_ms}")
            cur = await db.execute(_STORE_PROBE_SQL)
            await cur.fetchone()
            timed_out = False
        if scope.cancelled_caught:
            timed_out = True
    except Exception as exc:  # noqa: BLE001
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        result["detail"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        if conn is not None:
            # Shielded because closing is itself an await, and this one can run
            # with a cancellation already active around it — a caller that gave
            # up on the probe, a request the client abandoned, a shutting-down
            # daemon. Unshielded it is cancelled before it reaches the
            # connection, and the worker thread lives on holding an open
            # database until the loop closes underneath it. The wait is bounded
            # by the busy timeout already set above.
            with CancelScope(shield=True):
                await conn.close()

    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    if timed_out:
        result["status"] = "slow"
        result["detail"] = f"store did not answer a single indexed read within {timeout_ms} ms"
    else:
        result["status"] = "healthy"
        result["detail"] = "store answered a bounded indexed read"
    return result


def _find_pid_file(root: Path) -> int | None:
    for name in ("session.pid", "run.pid", ".pid"):
        p = root / name
        if p.exists():
            try:
                return int(p.read_text().strip())
            except (OSError, ValueError):
                pass
    for p in root.glob("*.pid"):
        try:
            return int(p.read_text().strip())
        except (OSError, ValueError):
            pass
    return None


def _ps_snapshot() -> str:
    """One ``ps`` capture, shareable across rows; empty string when unavailable."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return result.stdout
    except Exception:
        return ""


# The command-table fallback exists for imported and legacy sessions that do
# not carry a targeted process identity.  It is host-wide work, so nearby
# requests share a very short-lived answer.  The value is deliberately short:
# this cache is a display/diagnostic optimization, never evidence used to send
# a signal to a process.
PS_SNAPSHOT_TTL_SECONDS = 1.0


class _PsSnapshotCache(NamedTuple):
    value: str
    stored_at: float
    duration_ms: float


_PS_SNAPSHOT_CACHE: _PsSnapshotCache | None = None
# The in-flight capture is stored with the loop that owns it. A Task belongs to
# the loop that created it, so a caller on a different loop cannot await this
# one -- see cached_ps_snapshot. The cached value above carries no such
# affinity and is shared by every caller.
_PS_SNAPSHOT_INFLIGHT: tuple[asyncio.AbstractEventLoop, asyncio.Task[str]] | None = None
_PS_SNAPSHOT_METRICS: dict[str, int | float | None] | None = None


def _ps_snapshot_metrics_state() -> dict[str, int | float | None]:
    global _PS_SNAPSHOT_METRICS
    if _PS_SNAPSHOT_METRICS is None:
        _PS_SNAPSHOT_METRICS = {
            "captures": 0,
            "cache_hits": 0,
            "singleflight_hits": 0,
            "identity_resolved": 0,
            "fallback_checks": 0,
            "last_scan_duration_ms": None,
        }
    return _PS_SNAPSHOT_METRICS


async def _capture_ps_snapshot() -> str:
    """Capture once off-loop and publish the cache before waking waiters."""
    global _PS_SNAPSHOT_CACHE

    started = time.perf_counter()
    value = await anyio.to_thread.run_sync(_ps_snapshot)
    duration_ms = (time.perf_counter() - started) * 1000
    _PS_SNAPSHOT_CACHE = _PsSnapshotCache(
        value=value,
        stored_at=time.monotonic(),
        duration_ms=duration_ms,
    )
    metrics = _ps_snapshot_metrics_state()
    metrics["captures"] = int(metrics["captures"] or 0) + 1
    metrics["last_scan_duration_ms"] = round(duration_ms, 3)
    return value


async def cached_ps_snapshot() -> str:
    """Return a short-TTL process snapshot with async singleflight refresh."""
    global _PS_SNAPSHOT_INFLIGHT

    cached = _PS_SNAPSHOT_CACHE
    if cached is not None and time.monotonic() - cached.stored_at < PS_SNAPSHOT_TTL_SECONDS:
        metrics = _ps_snapshot_metrics_state()
        metrics["cache_hits"] = int(metrics["cache_hits"] or 0) + 1
        return cached.value

    loop = asyncio.get_running_loop()
    inflight = _PS_SNAPSHOT_INFLIGHT
    task: asyncio.Task[str] | None = None
    if inflight is not None:
        owner, pending = inflight
        # Singleflight is per event loop, deliberately. Awaiting a Task owned by
        # another loop raises rather than sharing its result, so a caller on a
        # second loop starts its own capture instead of failing. Two loops in
        # one process is the exception (a legacy caller running its own loop
        # beside the serving one), and one extra `ps` there beats a
        # RuntimeError; within a loop this still collapses to a single capture.
        if owner is loop and not pending.done():
            task = pending
            metrics = _ps_snapshot_metrics_state()
            metrics["singleflight_hits"] = int(metrics["singleflight_hits"] or 0) + 1

    if task is None:
        task = asyncio.create_task(_capture_ps_snapshot())
        _PS_SNAPSHOT_INFLIGHT = (loop, task)

    try:
        # One cancelled request must not cancel the shared capture underneath
        # other viewers that are already awaiting it.
        return await asyncio.shield(task)
    finally:
        current = _PS_SNAPSHOT_INFLIGHT
        # Clear only our own entry: a capture started by another loop after
        # ours must not be dropped by this one's cleanup.
        if current is not None and current[1] is task and task.done():
            _PS_SNAPSHOT_INFLIGHT = None


# Process start-time comparison tolerance (clock-tick rounding).
_PID_CREATE_TIME_TOLERANCE = 1.0

# Sessions the health report classifies per call, newest first. Bounds the
# filesystem and process work the diagnostic does; the response discloses
# how much of the store the number actually covers.
HEALTH_SCAN_LIMIT = 500


def process_liveness(
    session: dict[str, Any],
    artifacts_path: Path | None,
    ps_snapshot: str | None = None,
) -> bool | None:
    """Tri-state process liveness: True = observed alive, False = confirmed
    dead, None = unknown (no recorded pid/no process match)."""
    pid: int | None = None
    create_time: float | None = None

    meta = session.get("node_metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = None
    if isinstance(meta, dict):
        raw_pid = meta.get("pid")
        if raw_pid is not None:
            try:
                pid = int(raw_pid)
            except (TypeError, ValueError):
                pid = None
        raw_ct = meta.get("pid_create_time")
        if isinstance(raw_ct, int | float):
            create_time = float(raw_ct)

    if pid is None and artifacts_path is not None and artifacts_path.exists():
        pid = _find_pid_file(artifacts_path)

    if pid is not None:
        if not _pid_is_live(pid):
            return False
        try:
            import psutil

            proc = psutil.Process(pid)
            # A zombie has exited but not been reaped; not a live worker
            # even though _pid_is_live() still reports it as present.
            if proc.status() == psutil.STATUS_ZOMBIE:
                return False
            if create_time is not None:
                actual = proc.create_time()
                if abs(actual - create_time) > _PID_CREATE_TIME_TOLERANCE:
                    return False  # pid recycled; the recorded process is gone
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return False
        except Exception:
            # Best-effort check: the pid-is-live test above already
            # passed, so an unreadable status/start time reads as alive.
            _log.debug("pid %s status/start-time check failed", pid, exc_info=True)
        return True

    session_id = session.get("id") or ""
    snapshot = ps_snapshot if ps_snapshot is not None else _ps_snapshot()
    if session_id and session_id in snapshot:
        return True
    return None


async def _resolve_process_liveness_probe_with_snapshot(
    probe: Callable[[str], bool | None],
) -> tuple[bool | None, str]:
    """Resolve liveness and return the fallback evidence used for sync classifiers."""
    targeted = probe("")
    metrics = _ps_snapshot_metrics_state()
    if targeted is not None:
        metrics["identity_resolved"] = int(metrics["identity_resolved"] or 0) + 1
        return targeted, ""

    metrics["fallback_checks"] = int(metrics["fallback_checks"] or 0) + 1
    snapshot = await cached_ps_snapshot()
    return probe(snapshot), snapshot


async def resolve_process_liveness_probe(
    probe: Callable[[str], bool | None],
) -> bool | None:
    """Apply targeted-first fallback semantics through a caller-owned probe."""
    resolved, _snapshot = await _resolve_process_liveness_probe_with_snapshot(probe)
    return resolved


async def _resolve_process_liveness_with_snapshot(
    session: dict[str, Any],
    artifacts_path: Path | None,
) -> tuple[bool | None, str]:
    return await _resolve_process_liveness_probe_with_snapshot(
        lambda snapshot: process_liveness(session, artifacts_path, ps_snapshot=snapshot)
    )


async def resolve_process_liveness(
    session: dict[str, Any],
    artifacts_path: Path | None,
) -> bool | None:
    """Prefer targeted identity; share an off-loop host scan only for legacy rows."""
    resolved, _snapshot = await _resolve_process_liveness_with_snapshot(session, artifacts_path)
    return resolved


def process_snapshot_diagnostics() -> dict[str, int | float | None]:
    """Observable coverage and capture cost for the legacy liveness fallback."""
    metrics = dict(_ps_snapshot_metrics_state())
    cached = _PS_SNAPSHOT_CACHE
    metrics["cache_age_ms"] = (
        round((time.monotonic() - cached.stored_at) * 1000, 3) if cached is not None else None
    )
    return metrics


def _artifacts_path(row: Any) -> Path | None:
    ap = row["artifacts_path"] if "artifacts_path" in row.keys() else None
    if ap:
        return Path(ap)
    return None


# The lock files lionagi itself creates under a run's artifact tree. A stale one
# of these means a process died still holding a claim, which is the only thing
# this evidence is meant to detect.
#
# Matching by the ``.lock`` suffix instead reads dependency lockfiles -- uv.lock,
# poetry.lock, Cargo.lock -- as dead runs. Since a run's artifacts_path is
# routinely a repository root, and the search below is recursive, a single
# checked-in uv.lock marked every completed session in that repository a zombie:
# the classifier's most severe level, fired by a file that says nothing about any
# process. Match the names we write, not the extension anyone may use.
#
# The resume lock (``{digest}.lock``) is deliberately absent: it lives in
# ``resume-locks/`` beside the state DB, never under an artifact root, so it is
# unreachable from here by construction.
_RUNTIME_LOCK_NAMES: frozenset[str] = frozenset({"job.lock", "finalize.lock"})

# Directory names never on the path to a run directory, and routinely holding
# more files than everything else in the tree combined.
#
# The cost of skipping them is what makes matching by name affordable. Matching
# by suffix was fast for the wrong reason: a repository root almost always has a
# dependency lockfile near the top, so the search hit one immediately and
# stopped. Searching for names we write means the common answer is "not here",
# and reaching that answer costs a complete traversal. Measured on one machine,
# an unpruned walk of a projects directory took 100 seconds, against three
# milliseconds for the suffix match it replaced.
_UNSEARCHED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "site-packages",
        "target",
        "dist",
        "build",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".next",
        ".turbo",
        ".gradle",
        "DerivedData",
    }
)


# How long all the walks in one scan may take, in total. Pruning lowered the
# per-tree constant but nothing bounds the work: a session's artifacts_path is
# routinely a whole project directory, and the tree under it is whatever the
# user happens to keep there. Measured against the live store, a full pass over
# the 3780 artifact roots that exist on disk took 76 seconds, of which four
# roots -- each a top-level directory -- accounted for 70.
#
# A ceiling on the scan rather than on each root, because the cost is
# concentrated: a per-root limit generous enough for a normal tree still admits
# a hundred slow roots, while one shared ceiling is the number that actually
# bounds the endpoint.
_SCAN_BUDGET_SECONDS = 5.0

# Directories walked between budget checks. The clock read is cheap but not
# free, and checking every directory would spend a measurable fraction of the
# budget measuring the budget.
_BUDGET_CHECK_INTERVAL = 64


class _ScanBudget:
    """Wall-clock ceiling shared by every walk in a single scan.

    Monotonic on purpose: a system clock adjustment mid-scan must not extend or
    collapse the ceiling, and this deadline is never compared against the
    ``cutoff`` timestamps, which are wall-clock by necessity.

    The ceiling is read when a budget is built rather than defaulted in the
    signature, so that retuning the module constant retunes the next scan. A
    default argument would have bound the value at import and left the constant
    looking like a knob that does nothing.
    """

    def __init__(self, seconds: float | None = None) -> None:
        ceiling = _SCAN_BUDGET_SECONDS if seconds is None else seconds
        self._deadline = time.monotonic() + ceiling
        self.exhausted = False

    def expired(self) -> bool:
        if not self.exhausted and time.monotonic() >= self._deadline:
            self.exhausted = True
        return self.exhausted


class _ScanResult(NamedTuple):
    """What a lock scan found, and whether it finished looking.

    ``truncated`` exists so that "did not finish" cannot be read as "found
    nothing". Both states carry ``lock=None``, and collapsing them would make a
    scan that ran out of budget report a clean bill of health -- the failure
    biased toward looking safe, which is the direction that gets believed.
    """

    lock: Path | None
    truncated: bool


def _find_stale_lock(
    root: Path,
    *,
    cutoff: float,
    cache: dict[tuple[str, float], _ScanResult] | None = None,
    budget: _ScanBudget | None = None,
) -> _ScanResult:
    """Find a stale runtime lock under *root*.

    Pass *cache* to share results across one scan. Sessions repeat their
    artifact roots heavily -- one root accounted for 152 of 500 recent sessions
    on one machine -- and without a cache each of those repeats re-walks the
    same tree to reach the same answer. The cache is deliberately caller-owned
    and per-scan rather than module-level: a scan is a snapshot taken at one
    ``cutoff``, so results are consistent within it, while a process-lifetime
    cache would keep answering with a filesystem that has since moved on.

    Pass *budget* to bound the whole scan. A truncated result is cached like any
    other: within one scan the answer for a root does not improve by asking
    again, and re-walking it would spend budget that is already gone.
    """
    key = (str(root), cutoff)
    if cache is not None and key in cache:
        return cache[key]
    found = _walk_for_stale_lock(root, cutoff=cutoff, budget=budget)
    if cache is not None:
        cache[key] = found
    return found


def _walk_for_stale_lock(
    root: Path, *, cutoff: float, budget: _ScanBudget | None = None
) -> _ScanResult:
    seen = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            # Prune in place, which is what stops os.walk descending. Assigning
            # a new list to the name instead would be a no-op it cannot report.
            dirnames[:] = [d for d in dirnames if d not in _UNSEARCHED_DIRS]
            seen += 1
            if budget is not None and seen % _BUDGET_CHECK_INTERVAL == 0 and budget.expired():
                return _ScanResult(None, True)
            # One traversal answers for every name. A pass per name walks the
            # whole tree once per name, and the miss case walks all of them.
            for name in _RUNTIME_LOCK_NAMES & set(filenames):
                lock = Path(dirpath) / name
                try:
                    if lock.stat().st_mtime < cutoff:
                        return _ScanResult(lock, False)
                except OSError:
                    pass
    except OSError:
        pass
    # An OSError partway through is a tree we could not finish reading, but the
    # caller already treats an unreadable root as its own condition, so it stays
    # a completed scan here rather than acquiring a second meaning.
    return _ScanResult(None, False)


def _classify_phantom(
    row: Any,
    *,
    now: float,
    stale_seconds: float,
    ps_snapshot: str | None = None,
    lock_cache: dict[tuple[str, float], _ScanResult] | None = None,
    lock_budget: _ScanBudget | None = None,
) -> PhantomReason | None:
    ap = _artifacts_path(row)
    node_metadata = row["node_metadata"] if "node_metadata" in row.keys() else None
    session = {"id": row["id"], "node_metadata": node_metadata}
    # A running session is never a phantom while its process is observably alive.
    if process_liveness(session, ap, ps_snapshot) is True:
        return None
    # Not yet stale: it may simply not have written artifacts yet, so give it
    # the benefit of the doubt rather than reap a fresh/quiet session.
    updated_at = row["updated_at"] or 0.0
    if now - updated_at < stale_seconds:
        return None
    if ap and not ap.exists():
        return "missing_artifacts"
    if (
        ap
        and ap.exists()
        and _find_stale_lock(
            ap, cutoff=now - stale_seconds, cache=lock_cache, budget=lock_budget
        ).lock
        is not None
    ):
        return "stale_lock"
    # A truncated scan lands here too, and that is deliberate rather than an
    # oversight: every path to this point has already established the session is
    # a phantom, so the unfinished walk costs the *reason* its precision, not the
    # verdict its safety. Where truncation would change a verdict -- the health
    # classifier, where "no stale lock" is what keeps a session out of ZOMBIE --
    # it is reported instead of absorbed.
    return "process_dead"


async def list_phantom_sessions(*, stale_hours: float = 1.0) -> list[dict[str, Any]]:
    require_file_store()
    if not store_exists():
        return []
    now = time.time()
    stale_seconds = stale_hours * 3600
    phantoms: list[dict[str, Any]] = []
    async with _open_db(store_path()) as db:
        cur = await db.execute(
            """
            SELECT id, name, playbook_name, started_at, updated_at, artifacts_path,
                   status, node_metadata
            FROM sessions
            WHERE status = 'running'
            ORDER BY updated_at DESC
            """
        )
        rows = await cur.fetchall()
    # One scan, one answer per artifact root: sessions repeat their roots
    # heavily, and the walk is the expensive part.
    lock_cache: dict[tuple[str, float], _ScanResult] = {}
    lock_budget = _ScanBudget()
    for row in rows:
        artifacts = _artifacts_path(row)
        session = {"id": row["id"], "node_metadata": row["node_metadata"]}
        _process_alive, snapshot = await _resolve_process_liveness_with_snapshot(session, artifacts)
        # Classification stats an artifact tree and may walk it. Both are
        # synchronous filesystem work, and this coroutine is the one serving
        # every other request while it runs.
        reason = await anyio.to_thread.run_sync(
            partial(
                _classify_phantom,
                row,
                now=now,
                stale_seconds=stale_seconds,
                ps_snapshot=snapshot,
                lock_cache=lock_cache,
                lock_budget=lock_budget,
            )
        )
        if reason is not None:
            phantoms.append(
                {
                    "session_id": row["id"],
                    "playbook": row["playbook_name"] or row["name"],
                    "started_at": row["started_at"],
                    "updated_at": row["updated_at"] or 0.0,
                    "artifacts_path": row["artifacts_path"],
                    "reason": reason,
                }
            )
    return phantoms


async def doctor(*, stale_hours: float = 1.0) -> dict[str, Any]:
    return {
        "phantom_sessions": await list_phantom_sessions(stale_hours=stale_hours),
        "db_health": db_health(),
        "diagnostic_run_at": now_utc().isoformat(),
    }


async def _code_identity_report() -> dict[str, Any]:
    """Which code this daemon is actually running, and whether it has fallen behind.

    The daemon imports lionagi once, at start. With an editable install that
    import resolves to a working tree, so which code is running is a property of
    whatever commit that checkout sits on — a property nothing else in this
    report can see. The version string cannot distinguish them: a stale tree and
    a current one report the same one.

    Read fresh on every call rather than cached at start, because the answer this
    is asked for is "is the tree still current", and a value captured at start
    can only ever say it was current then. The read shells out to git, so it runs
    off the event loop and its own budget bounds it; a daemon must not stall on
    its own health check.
    """
    try:
        from lionagi.cli._code_identity import code_identity

        return await anyio.to_thread.run_sync(code_identity)
    except Exception as exc:  # noqa: BLE001 — an unanswerable check is unknown, never absent
        # Reported rather than omitted: a missing key reads as "not checked",
        # which is the same shape as "checked and current" to anything scanning
        # this response.
        return {
            "drift": {
                "status": "unknown",
                "reasons": [],
                "unknown": [f"could not establish code identity: {type(exc).__name__}: {exc}"],
            }
        }


async def health_report() -> dict[str, Any]:
    """Composite session health snapshot for the admin console."""
    from collections import Counter

    from lionagi.state.health import (
        SessionHealth,
        classify_session_health,
    )
    from lionagi.studio.config import scheduler_timezone_report

    require_file_store()
    if not store_exists():
        return {
            "sessions": {"total": 0, "by_status": {}, "by_health": {}, "unhealthy": []},
            "db": db_health(),
            "process_snapshot": process_snapshot_diagnostics(),
            "scheduler_timezone": scheduler_timezone_report(),
            "code_identity": await _code_identity_report(),
            "diagnostic_run_at": now_utc().isoformat(),
        }

    now = time.time()
    async with _open_db(store_path()) as db:
        total_cur = await db.execute("SELECT COUNT(*) AS n FROM sessions")
        total_row = await total_cur.fetchone()
        total_sessions = int(total_row["n"]) if total_row else 0
        # Classifying a session stats its artifact tree and can shell out to
        # `ps`, so the pass is bounded to the most recent window and the
        # response says how much of the store it covered. Reporting on every
        # session would make this diagnostic the most expensive read in the
        # daemon -- and a health check that can hurt a sick store is worse
        # than no health check.
        cur = await db.execute(
            """
            WITH page AS (
                SELECT id AS page_id FROM sessions ORDER BY updated_at DESC LIMIT ?
            )
            -- show_play_name sits ABOVE playbook_name in the display-name
            -- chain, so omitting it here does not fall back gracefully: the
            -- resolver reads None and answers with the tier below, and a play
            -- session is named one way here and another way in the API and
            -- UI. Selecting a column short of what the resolver reads is the
            -- same two-names-for-one-session defect, one layer down.
            SELECT s.id, s.name, s.status, s.invocation_kind, s.agent_name,
                   s.playbook_name, s.show_play_name, s.started_at, s.ended_at,
                   s.updated_at,
                   s.last_message_at, s.artifacts_path, s.node_metadata,
                   COALESCE(SUM(json_array_length(p.collection)), 0) AS message_count
            FROM page
            JOIN sessions s ON s.id = page.page_id
            LEFT JOIN branches b ON b.session_id = s.id
            LEFT JOIN progressions p ON p.id = b.progression_id
            GROUP BY s.id
            ORDER BY s.updated_at DESC
            """,
            (HEALTH_SCAN_LIMIT,),
        )
        rows = await cur.fetchall()

    by_status: Counter[str] = Counter()
    by_health: Counter[str] = Counter()
    unhealthy: list[dict[str, Any]] = []
    # One scan, one answer per artifact root: sessions repeat their roots
    # heavily, and the walk is the expensive part.
    lock_cache: dict[tuple[str, float], _ScanResult] = {}
    lock_budget = _ScanBudget()
    lock_scan_truncated = 0

    for row in rows:
        sess = {k: row[k] for k in row.keys()}
        status = sess.get("status") or "completed"

        artifacts = _artifacts_path(row)
        has_artifacts = artifacts is not None and artifacts.exists()
        has_stale_locks = False
        if artifacts is not None and artifacts.exists():
            cutoff = now - 3600
            key = (str(artifacts), cutoff)
            if key in lock_cache:
                # A repeat of a root already answered: no walk, so nothing to
                # move off the loop. Most rows take this path.
                scan = lock_cache[key]
            else:
                # The walk is synchronous filesystem work and this coroutine is
                # the one serving every other request. Left inline it blocks the
                # loop for as long as the traversal takes, which is how a static
                # /health probe that touches nothing ended up timing out.
                scan = await anyio.to_thread.run_sync(
                    partial(
                        _find_stale_lock,
                        artifacts,
                        cutoff=cutoff,
                        cache=lock_cache,
                        budget=lock_budget,
                    )
                )
            has_stale_locks = scan.lock is not None
            if scan.truncated:
                lock_scan_truncated += 1

        if status == "running":
            process_alive = await resolve_process_liveness(sess, artifacts)
        else:
            process_alive = False

        health = classify_session_health(
            sess,
            now=now,
            process_alive=process_alive,
            has_artifacts=has_artifacts,
            has_stale_locks=has_stale_locks,
        )
        by_health[health.value] += 1

        # A "running" row whose process is confirmed dead isn't actually
        # running; bucket it under its health verdict instead.
        status_bucket = status
        if status == "running" and health in (
            SessionHealth.STALE,
            SessionHealth.ORPHANED,
            SessionHealth.ZOMBIE,
        ):
            status_bucket = health.value
        by_status[status_bucket] += 1

        if health not in (SessionHealth.HEALTHY, SessionHealth.IDLE):
            last_activity = (
                sess.get("last_message_at") or sess.get("updated_at") or sess.get("started_at") or 0
            )
            unhealthy.append(
                {
                    "session_id": row["id"],
                    # The same resolver every other surface reads through. The
                    # stored `name` column can hold a raw prompt body, which
                    # resolve_display_name demotes below the play, playbook and
                    # agent-role tiers; reading the column directly published
                    # those prompts here while the API and UI showed a clean
                    # label for the very same session.
                    "name": resolve_display_name(sess),
                    "health": health.value,
                    "status": status,
                    "invocation_kind": sess.get("invocation_kind"),
                    "agent_name": sess.get("agent_name"),
                    "playbook_name": sess.get("playbook_name"),
                    "last_message_at": sess.get("last_message_at"),
                    "idle_seconds": now - last_activity if last_activity else None,
                    "process_alive": process_alive,
                    "message_count": sess.get("message_count") or 0,
                }
            )

    scanned = sum(by_status.values())
    return {
        "sessions": {
            "total": total_sessions,
            "scanned": scanned,
            "truncated": scanned < total_sessions,
            # Sessions whose artifact tree was too large to finish searching
            # within the scan's shared budget. Named apart from "truncated"
            # above, which is about how many rows were looked at rather than
            # how completely each one was examined. Non-zero means some of the
            # health verdicts below rest on an unfinished search: a session can
            # only be counted ZOMBIE when a stale lock is FOUND, so an
            # unfinished search can understate that bucket, never inflate it.
            "lock_scan_truncated": lock_scan_truncated,
            "by_status": dict(by_status),
            "by_health": dict(by_health),
            "unhealthy": unhealthy,
        },
        "db": db_health(),
        "process_snapshot": process_snapshot_diagnostics(),
        # The zone this daemon interprets cron expressions in, as resolved at
        # its own start. Reported alongside the other daemon state because the
        # value is frozen per process: nothing in the source tree or the host's
        # configuration can be read back from a running scheduler otherwise.
        "scheduler_timezone": scheduler_timezone_report(),
        # The commit this daemon's code came from, and whether that checkout has
        # since fallen behind the ref it tracks. Every scheduled run this daemon
        # spawns executes the tree named here.
        "code_identity": await _code_identity_report(),
        "diagnostic_run_at": now_utc().isoformat(),
    }


_PHANTOM_REASON_CODES: dict[str, str] = {
    "process_dead": SessionReasons.HEALTH_PHANTOM_PROCESS_DEAD,
    "missing_artifacts": SessionReasons.HEALTH_PHANTOM_MISSING_ARTIFACTS,
    "stale_lock": SessionReasons.HEALTH_ZOMBIE_STALE_LOCKS,
}


def _resolve_session_health_reason_code(
    *,
    phantom_reason: str | None,
    health,  # SessionHealth enum from lionagi.state.health
) -> str | None:
    """Return the most-specific health-derived reason code, or None."""
    if phantom_reason is not None:
        return _PHANTOM_REASON_CODES.get(phantom_reason)
    from lionagi.state.health import SessionHealth

    if health == SessionHealth.STALE:
        return SessionReasons.HEALTH_STALE_NO_HEARTBEAT
    if health == SessionHealth.ORPHANED:
        return SessionReasons.HEALTH_ORPHANED_NO_PROCESS
    if health == SessionHealth.ZOMBIE:
        return SessionReasons.HEALTH_ZOMBIE_STALE_LOCKS
    return None


async def transition_sessions(
    session_ids: list[str],
    *,
    target_status: str,
    reason_code: str,
    reason_summary: str = "",
    evidence_refs: list[dict[str, Any]] | None = None,
    actor: str = "admin",
    legacy_reason: str | None = None,
) -> dict[str, Any]:
    """Transition running sessions to a terminal status with an audit-log entry."""
    from lionagi.state.reasons import validate_reason_code

    if target_status not in _ADMIN_TRANSITION_TARGETS:
        raise ValueError(
            f"target_status must be one of {sorted(_ADMIN_TRANSITION_TARGETS)}; "
            f"got {target_status!r}"
        )
    validate_reason_code(reason_code)
    if reason_summary is None:
        reason_summary = ""
    evidence_refs = list(evidence_refs or [])
    if not session_ids:
        return {"transitioned": [], "skipped": [], "event_id": None}
    if state_db_known_absent():
        return {"transitioned": [], "skipped": session_ids, "event_id": None}

    from lionagi.state.db import StateDB
    from lionagi.state.health import SessionHealth, classify_session_health

    transitioned: list[str] = []
    skipped: list[dict[str, str]] = []
    now = time.time()
    # One scan, one answer per artifact root: sessions repeat their roots
    # heavily, and the walk is the expensive part.
    lock_cache: dict[tuple[str, float], _ScanResult] = {}
    lock_budget = _ScanBudget()

    async with StateDB() as db:
        for sid in session_ids:
            current = await db.get_session(sid)
            if current is None:
                skipped.append({"session_id": sid, "reason": "not_found"})
                continue
            if current.get("status") != "running":
                skipped.append(
                    {"session_id": sid, "reason": f"not_running:{current.get('status')}"}
                )
                continue
            _snap_last_msg = current.get("last_message_at")
            _snap_updated = current.get("updated_at")
            artifacts = _artifacts_path(current)
            has_artifacts = artifacts is not None and artifacts.exists()
            has_stale_locks = False
            if artifacts is not None and artifacts.exists():
                # Offloaded for the same reason the other two scans are: this is
                # a synchronous walk inside a coroutine that is also the daemon's
                # only thread of service. The list of sessions is the caller's
                # rather than the whole table, which bounds how many walks run
                # but not how long any one of them holds the loop.
                scan = await anyio.to_thread.run_sync(
                    partial(
                        _find_stale_lock,
                        artifacts,
                        cutoff=now - 3600,
                        cache=lock_cache,
                        budget=lock_budget,
                    )
                )
                has_stale_locks = scan.lock is not None
            process_alive, liveness_snapshot = await _resolve_process_liveness_with_snapshot(
                current, artifacts
            )
            health = classify_session_health(
                current,
                now=now,
                process_alive=process_alive,
                has_artifacts=has_artifacts,
                has_stale_locks=has_stale_locks,
            )
            if health in (SessionHealth.HEALTHY, SessionHealth.IDLE):
                raise ValueError(
                    f"Session {sid!r} is {health.value} — transition refused. "
                    "Only unhealthy sessions may be force-transitioned."
                )

            # The second walk this session can provoke, and the one that used to
            # escape every bound: without the cache it repeated the traversal
            # just done above at the same cutoff, and without the budget it was
            # not bounded at all. Passing both makes it a cache hit in the
            # ordinary case; offloading covers the case where it is not.
            phantom_reason = await anyio.to_thread.run_sync(
                partial(
                    _classify_phantom,
                    current,
                    now=now,
                    stale_seconds=3600,
                    ps_snapshot=liveness_snapshot,
                    lock_cache=lock_cache,
                    lock_budget=lock_budget,
                )
            )
            classifier_code = _resolve_session_health_reason_code(
                phantom_reason=phantom_reason,
                health=health,
            )
            effective_reason_code = reason_code
            effective_reason_summary = reason_summary
            effective_evidence_refs: list[dict[str, Any]] = list(evidence_refs)
            if classifier_code is not None:
                effective_reason_code = classifier_code
                if not reason_summary:
                    cause = phantom_reason or health.value
                    effective_reason_summary = (
                        f"Operator transitioned session after classifier: {cause}."
                    )
                if phantom_reason is not None:
                    effective_evidence_refs.append(
                        {
                            "kind": "phantom_classification",
                            "reason": phantom_reason,
                            "session_id": sid,
                        }
                    )
                else:
                    effective_evidence_refs.append(
                        {
                            "kind": "session_health",
                            "health": health.value,
                            "session_id": sid,
                        }
                    )

            # Intentional specialized CAS (not a bypass of update_status()):
            # WHERE status='running' only allows a legal forward transition,
            # and the last_message_at/updated_at equality guards stop this
            # from clobbering a session that went active again mid-check.
            _started_at = current.get("started_at")
            _duration_ms = (
                max(0.0, (now - _started_at) * 1000)
                if isinstance(_started_at, int | float)
                else None
            )
            async with db.transaction() as conn:
                result = await conn.execute(
                    text(
                        "UPDATE sessions SET status=:status, ended_at=:now, updated_at=:now, "
                        "  duration_ms=:duration_ms, "
                        "  status_reason_code=:rcode, status_reason_summary=:rsummary, "
                        "  status_evidence_refs=:erefs "
                        "WHERE id=:sid AND status='running'"
                        "  AND (last_message_at IS :slast OR last_message_at = :slast)"
                        "  AND (updated_at      IS :supd  OR updated_at      = :supd)"
                    ).bindparams(bindparam("erefs", type_=JSON)),
                    {
                        "status": target_status,
                        "now": now,
                        "duration_ms": _duration_ms,
                        "rcode": effective_reason_code,
                        "rsummary": effective_reason_summary,
                        "erefs": effective_evidence_refs,
                        "sid": sid,
                        "slast": _snap_last_msg,
                        "supd": _snap_updated,
                    },
                )
                cas_hit = result.rowcount != 0
                if cas_hit:
                    await conn.execute(
                        text(
                            "INSERT INTO status_transitions "
                            "(id, entity_type, entity_id, previous_status, status, "
                            " reason_code, reason_summary, evidence_refs, "
                            " source, actor, created_at, metadata) "
                            "VALUES (:id, :etype, :eid, :prev, :status, "
                            " :rcode, :rsummary, :erefs, :source, :actor, :now, :meta)"
                        ).bindparams(bindparam("erefs", type_=JSON), bindparam("meta", type_=JSON)),
                        {
                            "id": uuid.uuid4().hex,
                            "etype": "session",
                            "eid": sid,
                            "prev": "running",
                            "status": target_status,
                            "rcode": effective_reason_code,
                            "rsummary": effective_reason_summary,
                            "erefs": effective_evidence_refs,
                            "source": "admin",
                            "actor": actor,
                            "now": now,
                            "meta": {
                                "legacy_reason": legacy_reason,
                                "health": health.value,
                                "process_alive": process_alive,
                            },
                        },
                    )
            if not cas_hit:
                existing = await db.get_session(sid)
                if existing is None:
                    skipped.append({"session_id": sid, "reason": "not_found"})
                elif existing.get("status") == "running":
                    skipped.append({"session_id": sid, "reason": "changed_since_snapshot"})
                else:
                    skipped.append(
                        {"session_id": sid, "reason": f"not_running:{existing.get('status')}"}
                    )
                continue
            transitioned.append(sid)

        event_id = await db.insert_admin_event(
            action="transition",
            target_id=None,
            actor=actor,
            details={
                "target_status": target_status,
                "reason_code": reason_code,
                "reason_summary": reason_summary,
                "evidence_refs": evidence_refs,
                "reason": legacy_reason,
                "transitioned": transitioned,
                "skipped": skipped,
            },
        )

    return {
        "transitioned": transitioned,
        "skipped": skipped,
        "event_id": event_id,
    }


async def list_admin_events(
    *,
    action: str | None = None,
    target_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if state_db_known_absent():
        return []
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        return await db.list_admin_events(action=action, target_id=target_id, limit=limit)


async def prune_sessions(session_ids: list[str]) -> int:
    """Delete sessions by explicit ID list."""
    require_file_store()
    seen: dict[str, None] = {}
    for sid in session_ids:
        seen[sid] = None
    unique_ids = list(seen)
    if not unique_ids or not store_exists():
        return 0
    placeholders = ",".join("?" * len(unique_ids))
    async with _open_db(store_path()) as db:
        cur = await db.execute(
            f"DELETE FROM sessions WHERE id IN ({placeholders})",  # noqa: S608
            unique_ids,
        )
        await db.commit()
        pruned = cur.rowcount or 0
        await db.execute(
            """
            DELETE FROM messages
            WHERE id NOT IN (
              SELECT value FROM progressions, json_each(progressions.collection)
            )
            """
        )
        await db.commit()
    # Recorded after the aiosqlite transaction above commits — insert_admin_event
    # opens its own StateDB (SQLAlchemy) write transaction; nesting it inside
    # the aiosqlite block would self-deadlock on the sqlite write lock.
    from lionagi.state.db import StateDB

    async with StateDB() as sdb:
        await sdb.insert_admin_event(
            action="prune_sessions",
            details={"requested_session_ids": unique_ids, "pruned": pruned},
            actor="admin",
        )
    return pruned


async def prune_phantom_sessions(*, stale_hours: float = 1.0) -> int:
    """Transition phantom sessions to 'failed' via the sanctioned status path;
    rows are preserved so reason history and artifacts stay inspectable."""
    from lionagi.state.db import StateDB
    from lionagi.studio.services.lifecycle import reap_phantom_sessions_detailed

    count, session_ids = await reap_phantom_sessions_detailed(
        stale_hours=stale_hours, actor="admin_prune"
    )
    async with StateDB() as db:
        await db.insert_admin_event(
            action="prune_phantoms",
            details={"count": count, "session_ids": session_ids, "stale_hours": stale_hours},
            actor="admin_prune",
        )
    return count


# ---------------------------------------------------------------------------
# Route handlers — admin area
# ---------------------------------------------------------------------------


@studio_route("/admin/doctor", method="GET", area="admin", name="doctor")
async def doctor_route(
    stale_hours: float = Query(default=1.0, gt=0),
) -> dict[str, Any]:
    return await doctor(stale_hours=stale_hours)


@studio_route("/admin/health", method="GET", area="admin", name="health")
async def health_route() -> dict[str, Any]:
    """ADR-0057 D6: composite session health report."""
    return await health_report()


@studio_route("/admin/readiness", method="GET", area="admin", name="readiness")
async def readiness_route(
    timeout_ms: int = Query(
        default=STORE_PROBE_TIMEOUT_MS,
        ge=50,
        le=10_000,
        description="How long the probe waits for the store before reporting slow",
    ),
) -> dict[str, Any]:
    """Whether a query against the store will actually return.

    Distinct from ``/health``, which answers only whether the process is up:
    that stays true while every database-backed endpoint is unresponsive, which
    is exactly the failure this reports. Never 5xx -- the verdict is in the
    body, so a caller can tell "store unreachable" from "store slow" from
    "healthy" instead of getting one boolean for all three.
    """
    return await store_probe(timeout_ms=timeout_ms)


@studio_route("/admin/transition", method="POST", area="admin", name="transition")
async def transition_route(body: TransitionBody) -> dict[str, Any]:
    """Mark running sessions terminal with a reason code."""
    reason_code = body.reason_code
    reason_summary = body.reason_summary

    if reason_code is not None:
        try:
            reason_code = validate_reason_code(reason_code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    elif body.reason:
        reason_code = _LEGACY_ADMIN_REASON_CODES[body.target_status]
        reason_summary = body.reason
        _log.warning(
            "Deprecated admin transition field 'reason' used without reason_code; "
            "mapped target_status=%s to reason_code=%s",
            body.target_status,
            reason_code,
        )
    else:
        raise HTTPException(status_code=400, detail="reason_code is required")

    try:
        return await transition_sessions(
            body.session_ids,
            target_status=body.target_status,
            reason_code=reason_code,
            reason_summary=reason_summary,
            evidence_refs=body.evidence_refs,
            actor=body.actor,
            legacy_reason=body.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@studio_route("/admin/events", method="GET", area="admin", name="admin_events")
async def admin_events_route(
    action: str | None = Query(default=None),
    target_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
) -> dict[str, Any]:
    events = await list_admin_events(action=action, target_id=target_id, limit=limit)
    return {"events": events}


@studio_route(
    "/admin/prune-old-data",
    method="POST",
    area="admin",
    name="prune_old_data",
)
async def prune_old_data_route(body: PruneOldDataBody) -> dict[str, int]:
    """Remove terminal sessions/runs older than keep_days (default from config)."""
    from ..services.db_maintenance import prune_old_data as _prune

    return await _prune(keep_days=body.keep_days, actor="admin")


@studio_route(
    "/admin/maintenance",
    method="POST",
    area="admin",
    name="run_maintenance",
)
async def run_maintenance_route(body: MaintenanceBody) -> dict[str, Any]:
    """Run a DB maintenance action (vacuum | checkpoint | prune). Returns 409,
    not 500, when SQLite can't acquire the write lock — a retryable signal."""
    from ..services.db_maintenance import (
        checkpoint_state_db,
        prune_old_data,
        vacuum_state_db,
    )

    try:
        if body.action == "vacuum":
            result = await vacuum_state_db(actor="admin")
            return {"action": "vacuum", **result}

        if body.action == "checkpoint":
            result = await checkpoint_state_db(actor="admin")
            return {"action": "checkpoint", **result}

        # action == "prune"
        result = await prune_old_data(actor="admin")
        return {"action": "prune", **result}

    except (sqlite3.OperationalError, _SAOperationalError) as exc:
        # Only genuine lock/busy contention is retryable; open/path failures
        # should surface as 500. Inspect .orig since SQLAlchemy's wrapper can omit it.
        msg = str(exc).lower()
        orig = getattr(exc, "orig", None)
        if orig is not None:
            msg = f"{msg} {str(orig).lower()}"
        if "locked" in msg or "in progress" in msg:
            raise HTTPException(
                status_code=409,
                detail="State database is busy — another writer holds the lock. Try again shortly.",
            ) from exc
        raise


@studio_route("/admin/prune", method="POST", area="admin", name="prune")
async def prune_route(body: PruneBody) -> dict[str, int]:
    has_ids = bool(body.session_ids)
    has_all = body.all_phantom
    if not has_ids and not has_all:
        raise HTTPException(status_code=422, detail="Provide session_ids or all_phantom")
    if has_ids and has_all:
        raise HTTPException(
            status_code=422,
            detail="Provide either session_ids or all_phantom, not both",
        )
    if has_all:
        count = await prune_phantom_sessions()
    else:
        count = await prune_sessions(body.session_ids or [])
    return {"pruned": count}
