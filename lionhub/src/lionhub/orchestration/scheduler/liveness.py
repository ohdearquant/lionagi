# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any, Literal, NamedTuple

from lionagi.ln.concurrency import run_sync as _run_sync
from lionagi.ln.concurrency.errors import shield_quietly
from lionagi.state.reasons import SessionReasons
from lionagi.state.session_naming import resolve_display_name
from lionhub.orchestration.job import (
    BOOT_TIME_TOLERANCE,
    recorded_identity_mode,
    recorded_pid_is_foreign,
)
from lionhub.orchestration.job import (
    pid_alive as _pid_is_live,
)
from lionhub.orchestration.state_store import open_db as _open_db
from lionhub.orchestration.state_store import require_file_store, store_exists, store_path

_log = logging.getLogger("lionagi.studio.services.admin")

PhantomReason = Literal["process_dead", "missing_artifacts", "stale_lock"]


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
    # Start order as a counter rather than as a clock reading. Publication has
    # to know which of two captures STARTED later, and a timestamp cannot
    # answer that at this granularity: time.monotonic() resolves to about 42ns
    # here, and roughly 15% of back-to-back reads return the same value. Two
    # captures starting inside one tick would compare equal, the `>` test would
    # not hold, and the earlier one would publish over the later one's evidence.
    # A counter handed out under its own lock is strictly increasing by
    # construction and carries no clock dependency at all. `stored_at` is kept
    # because the TTL is a real duration; it just no longer decides ordering,
    # which is why it is read at publication rather than at scan start.
    sequence: int


_PS_SNAPSHOT_CACHE: _PsSnapshotCache | None = None

# In-flight captures, keyed by the loop that owns each one. A Task belongs to
# the loop that created it, so a caller on a different loop cannot await this
# one -- see cached_ps_snapshot. One slot per loop rather than one slot in
# total: with a single slot, a third loop's entry evicted the second's, and the
# evicted loop's next caller started a duplicate capture instead of joining the
# one already running for it. The cached value above carries no such affinity
# and is shared by every caller.
_PS_SNAPSHOT_INFLIGHT: dict[asyncio.AbstractEventLoop, asyncio.Task[str]] = {}

_PS_SNAPSHOT_METRICS: dict[str, int | float | None] | None = None

# Guards publishing the cache and bumping the counters above. Those are
# read-modify-write sequences, and the loops that run them live on different
# OS threads, so comparing timestamps is not enough on its own: two captures
# can both read the cache before either writes, and then the older one wins by
# writing last. An asyncio lock would only order coroutines within one loop.
# Held for a few statements with no I/O and no await inside.
_PS_SNAPSHOT_PUBLISH_LOCK = threading.Lock()

# Start order for captures, handed out at scan start. See _PsSnapshotCache.sequence.
# Its own lock rather than the publish lock: the two guard different things, and
# sharing one would make a capture that is mid-publish block unrelated captures
# from even starting.
_PS_SNAPSHOT_SEQUENCE_LOCK = threading.Lock()

_PS_SNAPSHOT_SEQUENCE = 0


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
    """Capture once off-loop and publish the cache before waking waiters.

    Captures on different loops overlap, and they do not finish in the order
    they started: a scan that began earlier can return later. Publishing by
    arrival would let that older scan replace newer evidence, so a process
    that appears only in the newer snapshot resolves as absent. So a capture
    claims a start-order number up front, and publishes only if nothing that
    started later has published already.

    The TTL clock is read at publication instead, because that is the only
    thing ``stored_at`` decides now. Reading it at the start would charge the
    scan's own duration against a one-second lifetime: a scan slower than
    that would publish an entry already expired, every later caller would
    miss, and each would launch a scan of its own. That happens exactly under
    the load that makes scans slow, which is when the cache has to work.
    Nothing is lost by moving it, because the start-order number, not this
    timestamp, is what keeps an older scan from replacing a newer one.
    """
    global _PS_SNAPSHOT_CACHE

    started = time.perf_counter()
    # Claimed before the scan, so it orders captures by when they STARTED,
    # which is the ordering this cache publishes by. Taking it at publish time
    # instead would order by arrival and reintroduce exactly the staleness the
    # comparison below exists to prevent.
    with _PS_SNAPSHOT_SEQUENCE_LOCK:
        global _PS_SNAPSHOT_SEQUENCE
        _PS_SNAPSHOT_SEQUENCE += 1
        sequence = _PS_SNAPSHOT_SEQUENCE
    value = await _run_sync(_ps_snapshot)
    duration_ms = (time.perf_counter() - started) * 1000

    # Compare and publish under the lock as one step. Reading the cache,
    # deciding, and then writing is three steps, and captures run on different
    # OS threads: without the lock two of them can both read the old value
    # before either writes, at which point the one that started earlier
    # publishes last and its older evidence replaces the newer.
    with _PS_SNAPSHOT_PUBLISH_LOCK:
        metrics = _ps_snapshot_metrics_state()
        metrics["captures"] = int(metrics["captures"] or 0) + 1
        metrics["last_scan_duration_ms"] = round(duration_ms, 3)

        current = _PS_SNAPSHOT_CACHE
        if current is not None and current.sequence > sequence:
            # A scan that started later has already published. It is the
            # better evidence, so it stays in the cache and is what this
            # caller gets too.
            return current.value

        _PS_SNAPSHOT_CACHE = _PsSnapshotCache(
            value=value,
            # Read here, past the ordering guard above, so the TTL measures how
            # long this value has been available rather than how long ago its
            # scan set off. See the docstring.
            stored_at=time.monotonic(),
            duration_ms=duration_ms,
            sequence=sequence,
        )
    return value


async def cached_ps_snapshot() -> str:
    """Return a short-TTL process snapshot with async singleflight refresh."""
    cached = _PS_SNAPSHOT_CACHE
    if cached is not None and time.monotonic() - cached.stored_at < PS_SNAPSHOT_TTL_SECONDS:
        metrics = _ps_snapshot_metrics_state()
        metrics["cache_hits"] = int(metrics["cache_hits"] or 0) + 1
        return cached.value

    loop = asyncio.get_running_loop()
    # Singleflight is per event loop, deliberately. Awaiting a Task owned by
    # another loop raises rather than sharing its result, so a caller on a
    # second loop starts its own capture instead of failing. Two loops in one
    # process is the exception (a legacy caller running its own loop beside
    # the serving one), and one extra `ps` there beats a RuntimeError; within
    # a loop this collapses to a single capture however many loops are live.
    pending = _PS_SNAPSHOT_INFLIGHT.get(loop)
    if pending is not None and not pending.done():
        task = pending
        metrics = _ps_snapshot_metrics_state()
        metrics["singleflight_hits"] = int(metrics["singleflight_hits"] or 0) + 1
    else:
        task = asyncio.create_task(_capture_ps_snapshot())
        _PS_SNAPSHOT_INFLIGHT[loop] = task

    try:
        # One cancelled request must not cancel the shared capture underneath
        # other viewers that are already awaiting it.
        return await shield_quietly(task)
    finally:
        # Clear only our own entry: a capture started on this loop after ours
        # must not be dropped by this one's cleanup.
        if _PS_SNAPSHOT_INFLIGHT.get(loop) is task and task.done():
            del _PS_SNAPSHOT_INFLIGHT[loop]


# Process start-time comparison tolerance (clock-tick rounding).
_PID_CREATE_TIME_TOLERANCE = 1.0


def process_identity_is_foreign(session: dict[str, Any]) -> bool:
    """True if this machine can't observe the run's process at all — foreign host or unknown identity mode — since the staleness grace only protects momentary, not permanent, blind spots."""
    meta = session.get("node_metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return False
    if not isinstance(meta, dict):
        return False

    mode = recorded_identity_mode(meta)
    if mode is not None and mode not in ("local", "in_process"):
        return True

    # Checked on host alone, not "host + readable pid" — a row from another machine is that
    # machine's business even with an unparseable pid, and the pid-less fallback would misread it.
    return recorded_pid_is_foreign(meta)


def process_liveness(
    session: dict[str, Any],
    artifacts_path: Path | None,
    ps_snapshot: str | None = None,
) -> bool | None:
    """Tri-state process liveness: True = observed alive, False = confirmed dead, None = unknown (no recorded pid/no process match)."""
    pid: int | None = None
    create_time: float | None = None
    pid_host: str | None = None
    pid_boot_time: float | None = None
    identity_mode: str | None = None

    meta = session.get("node_metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = None
    if isinstance(meta, dict):
        identity_mode = recorded_identity_mode(meta)
        # An in-process run stores its host's pid under separate keys, not "pid", so the kill
        # path can't mistake the host for the run itself — but the host still bounds its liveness.
        in_process = identity_mode == "in_process"
        raw_pid = meta.get("host_pid" if in_process else "pid")
        if raw_pid is not None:
            try:
                pid = int(raw_pid)
            except (TypeError, ValueError):
                pid = None
        raw_ct = meta.get("host_pid_create_time" if in_process else "pid_create_time")
        if isinstance(raw_ct, int | float):
            create_time = float(raw_ct)
        raw_host = meta.get("pid_host")
        if isinstance(raw_host, str):
            pid_host = raw_host
        raw_boot = meta.get("pid_boot_time")
        if isinstance(raw_boot, int | float):
            pid_boot_time = float(raw_boot)

    if identity_mode not in (None, "local", "in_process"):
        return None
    if pid is not None and pid_host is not None and pid_host != socket.gethostname():
        return None
    if pid is not None and pid_boot_time is not None:
        rebooted_since = False
        try:
            import psutil

            # Boot time is re-derived from the clock each read, so NTP steps or suspend/resume
            # can drift it more than the create-time tolerance allows; it needs its own tolerance.
            rebooted_since = abs(psutil.boot_time() - pid_boot_time) > BOOT_TIME_TOLERANCE
        except Exception:
            # A failed boot-time read leaves this one check unevaluated, not the run unknowable
            # — answering "unknown" would let reapers treat every live session here as stale.
            _log.debug("boot-time comparison for pid %s failed", pid, exc_info=True)
        if rebooted_since:
            return False

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
                from lionagi.ln._proc import raw_create_time

                actual = raw_create_time(proc)
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


def _locks_beside_artifacts(root: Path) -> list[Path]:
    """The runtime-lock candidates that are siblings of an artifact root.

    Both locks lionagi writes for a run live *beside* the directory a session
    records, never inside it, so a walk that descends from that directory
    cannot reach either:

    * ``{run_dir}/finalize.lock`` -- ``run_dir`` is the artifact root's parent
      when the root is a run's ``artifacts`` child.
    * ``{JOBS_DIR}/{run_id}/job.lock`` -- the MCP server keeps its own record
      tree under ``LIONAGI_HOME/mcp/jobs``, keyed by the run id, which is also
      the artifact root's parent name.

    Read here explicitly rather than by rooting a wider walk at the run
    directory: the narrow check keeps the walk's cost and its skip list exactly
    as they were.
    """
    candidates: list[Path] = []
    if root.name == "artifacts":
        run_dir = root.parent
        candidates.append(run_dir / "finalize.lock")
        from lionhub.serve.mcp import config as mcp_config

        candidates.append(mcp_config.JOBS_DIR / run_dir.name / "job.lock")
    return candidates


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


def _stale_lock_beside(
    candidates: list[Path], *, cutoff: float, budget: _ScanBudget | None = None
) -> _ScanResult:
    """The first stale lock among candidates that are *not* under the walk root.

    Same rule as the walk applies to a lock's mtime, and the same budget: an
    exhausted budget is ``truncated``, which a caller must read as "did not
    finish looking" rather than "found nothing".
    """
    # At most two candidates, so the budget is read once rather than at the walk's interval.
    if candidates and budget is not None and budget.expired():
        return _ScanResult(None, True)
    for candidate in candidates:
        try:
            if candidate.stat().st_mtime < cutoff:
                return _ScanResult(candidate, False)
        except OSError:
            pass
    return _ScanResult(None, False)


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
    beside = _stale_lock_beside(_locks_beside_artifacts(root), cutoff=cutoff, budget=budget)
    if beside.lock is not None or beside.truncated:
        found = beside
    else:
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
    # Nor when the process isn't this machine's to observe — the staleness grace can't
    # rescue a row whose liveness is permanently invisible here.
    if process_identity_is_foreign(session):
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
    # A truncated scan lands here too, deliberately: the session is already established as a
    # phantom, so truncation costs the reason's precision, not the verdict's safety.
    return "process_dead"


async def list_phantom_sessions(*, stale_hours: float = 1.0) -> list[dict[str, Any]]:
    from lionhub.orchestration.state_store import _user_label_selection

    require_file_store()
    if not store_exists():
        return []
    now = time.time()
    stale_seconds = stale_hours * 3600
    phantoms: list[dict[str, Any]] = []
    async with _open_db(store_path()) as db:
        user_label = await _user_label_selection(db)
        cur = await db.execute(
            f"""
            SELECT id, name, {user_label}, playbook_name, agent_name, show_play_name,
                   started_at, updated_at, artifacts_path,
                   status, node_metadata
            FROM sessions
            WHERE status = 'running'
            ORDER BY updated_at DESC
            """  # noqa: S608 — selection is a fixed column name or NULL
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
        reason = await _run_sync(
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
                    "playbook": resolve_display_name(dict(row)),
                    "started_at": row["started_at"],
                    "updated_at": row["updated_at"] or 0.0,
                    "artifacts_path": row["artifacts_path"],
                    "reason": reason,
                }
            )
    return phantoms


_PHANTOM_REASON_CODES: dict[str, str] = {
    "process_dead": SessionReasons.HEALTH_PHANTOM_PROCESS_DEAD,
    "missing_artifacts": SessionReasons.HEALTH_PHANTOM_MISSING_ARTIFACTS,
    "stale_lock": SessionReasons.HEALTH_ZOMBIE_STALE_LOCKS,
}
