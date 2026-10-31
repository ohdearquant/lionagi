# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Run-scoped file layout: authoritative state in LIONAGI_HOME/runs/{run_id}/, artifacts in --save dir or state_root/artifacts/."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
import tempfile
import threading
import time
import uuid
import warnings
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from lionagi._paths import RUNS_ROOT, ensure_lionagi_dir
from lionagi.cli._util import AmbiguousIdError, mark_run_allocated
from lionagi.libs.path_safety import validate_path_component
from lionagi.ln import short_id
from lionagi.ln._json_dump import raise_if_non_finite
from lionagi.ln._utils import now_utc
from lionagi.ln.concurrency import CancelScope, cancelled_exc_classes, move_on_after
from lionagi.providers._provider_errors import ProviderError, _WorkerSpawnAdmissionError
from lionagi.utils import LIONAGI_HOME

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from lionagi import Branch
    from lionagi.cli._mcp_resolve import McpResolution
    from lionagi.state.db import StateDB

__all__ = (
    "LIONAGI_HOME",
    "RUNS_ROOT",
    "RunDir",
    "allocate_run",
    "find_branch",
    "load_last_branch",
    "save_last_branch_pointer",
    "list_runs",
    "current_run_id",
    "active_run_id",
    "resolve_run_reason",
    "setup_agent_persist",
    "find_incomplete_session_for_run",
    "teardown_persist",
    "teardown_agent_persist",
    "teardown_orchestration_persist",
)
_LEGACY_AGENTS_ROOT = LIONAGI_HOME / "logs" / "agents"
_LAST_BRANCH_POINTER = LIONAGI_HOME / "last_branch.json"
_RUN_ID_ENV_VAR = "LIONAGI_RUN_ID"
PERSISTENCE_DEGRADED_REASON_FIELD = "persistence_degraded_reason"
# The terminal status a run decided and its store did not take, with the reason code and the time
TERMINAL_WRITE_LOST_FIELD = "terminal_write_lost"
# When the run's signal persistence first stopped waiting on a store that refused the writer lock
SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD = "signal_persistence_degraded_at"
_SETUP_RECOVERY_TIMEOUT_SECONDS = 2.0


def _new_run_id() -> str:
    ts = now_utc().strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{short_id(6)}"


def current_run_id() -> str | None:
    """Return the run_id inherited from the environment (subprocess case)."""
    return os.environ.get(_RUN_ID_ENV_VAR) or None


# The run this process most recently allocated. Kept here rather than in the
# environment because allocate_run() reads the environment to *inherit* an id,
# so exporting one would make a second allocation in the same process silently
# reuse the first run's directory.
_ALLOCATED_RUN_ID: str | None = None


def active_run_id() -> str | None:
    """The run_id of the run this process is recording under, if any.

    The run this process allocated, falling back to one inherited from a
    parent. None when nothing has allocated a run yet — an embedded caller,
    or the window before ``allocate_run`` runs.
    """
    return _ALLOCATED_RUN_ID or current_run_id()


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Write *payload* to *path* so readers never see a partial file.

    The temp file is uniquely named and lives in the destination directory
    (os.replace is only atomic within a filesystem), so concurrent writers
    of the same target cannot corrupt each other's in-progress write.

    A non-finite float is refused before anything is written: json.dumps would
    emit the tokens ``NaN``/``Infinity``, which this process reads back happily
    and every strict reader rejects.
    """
    raise_if_non_finite(payload)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(payload, indent=2))
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


@contextlib.contextmanager
def _run_manifest_lock(state_root: Path, *, required: bool = True) -> Iterator[None]:
    """Hold the lock every write of the run.json in *state_root* takes.

    Invocations that share a run id (`LIONAGI_RUN_ID`) share its run.json. A writer that reads it
    to decide what to write holds this lock across the read and the write, so no other writer's
    record lands between them (ADR-0104 Amendment 2). It is POSIX advisory locking on a file beside
    run.json; where `fcntl` does not exist (Windows) nothing is held, and writers resolve
    last-writer-wins.

    A lock that cannot be taken (a filesystem without `flock`, a lock file that cannot be opened)
    raises the OSError when *required*; otherwise the body runs without it, with a warning, so a
    run's own writes go on as they did before the lock existed.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows fallback
        yield
        return

    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        fd = os.open(state_root / ".run.json.lock", flags, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        if required:
            raise
        _log.warning("run.json in %s written without its lock: %r", state_root, exc)
        fd = None
    if fd is None:
        yield
        return
    try:
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


@dataclass(frozen=True, slots=True)
class RunDir:
    """Resolved state and artifact paths for one CLI run."""

    run_id: str
    state_root: Path
    artifact_root: Path
    cwd: Path | None = None
    mcp_config_source: Path | None = None
    mcp_config_searched_from: Path | None = None
    mcp_config_explicit: bool | None = None
    mcp_config_reason: str | None = None

    # ── Path helpers ────────────────────────────────────────────────

    @property
    def manifest_path(self) -> Path:
        return self.state_root / "run.json"

    @property
    def checkpoint_path(self) -> Path:
        return self.state_root / "checkpoint.json"

    @property
    def branches_dir(self) -> Path:
        return self.state_root / "branches"

    @property
    def stream_dir(self) -> Path:
        return self.state_root / "stream"

    def branch_path(self, branch_id: str) -> Path:
        return self.branches_dir / f"{branch_id}.json"

    def stream_buffer_path(self, branch_id: str) -> Path:
        return self.stream_dir / f"{branch_id}.buffer.jsonl"

    def agent_artifact_dir(self, agent_id: str) -> Path:
        """Return artifact dir for agent_id, rejecting any id that resolves outside artifact_root (path-traversal guard)."""
        try:
            validate_path_component(agent_id, label="agent_id")
        except ValueError as exc:
            raise ValueError(f"agent_id {agent_id!r} is not a safe path component") from exc
        candidate = (self.artifact_root / agent_id).resolve()
        root = self.artifact_root.resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"agent_id {agent_id!r} resolves outside artifact_root {root}"
            ) from exc
        return self.artifact_root / agent_id

    @property
    def synthesis_path(self) -> Path:
        return self.artifact_root / "synthesis.md"

    @property
    def flow_log_path(self) -> Path:
        return self.artifact_root / "flow.log"

    @property
    def dag_image_path(self) -> Path:
        return self.artifact_root / "flow_dag.png"

    # ── Manifest I/O ────────────────────────────────────────────────

    def write_manifest(self, data: dict) -> None:
        """Replace run.json with *data* plus this run's identity fields.

        The write is atomic (a uniquely-named temp file in the same
        directory, then os.replace), so a concurrent reader observes either
        the previous manifest or the new one, never a truncated file. It is
        a whole-file replacement, not a merge: the caller owns the full
        lifecycle contents, and two writers racing on one run still resolve
        last-writer-wins. Each write takes the run's manifest lock
        (`_run_manifest_lock`), so a writer holding it across a read and its
        write sees no other write land between them. A run reconstructed from
        its path preserves any execution provenance already stored in the
        manifest, read under that lock; any error reading it other than a
        missing file or a ``JSONDecodeError`` (an ``OSError``, a
        ``UnicodeDecodeError``, or a ``ValueError`` the JSON parser raises for
        JSON it will not read, such as an integer over its digit limit) stops
        the write with that error.

        The file's signal persistence degradation time stays when *data* has
        none or a later one (ADR-0104 Amendment 3). A run bound to its
        execution provenance reads the file for that time alone, and writes
        without it when the file cannot be read.
        """
        ensure_lionagi_dir(self.state_root)
        with _run_manifest_lock(self.state_root, required=False):
            self._write_manifest_holding_lock(data)

    def _write_manifest_holding_lock(self, data: dict) -> None:
        """:meth:`write_manifest` for a caller already holding the manifest lock,
        which is not reentrant."""
        payload = {
            "run_id": self.run_id,
            "state_root": str(self.state_root),
            "artifact_root": str(self.artifact_root),
            **data,
        }
        if self.cwd is not None:
            # Execution context is allocation-owned provenance. Whole-file
            # writers replace lifecycle data throughout a live run, but must
            # not erase or redefine the directory and MCP discovery facts that
            # determined the run's tool surface.
            payload.update(
                {
                    "cwd": str(self.cwd),
                    "mcp_config_source": (
                        str(self.mcp_config_source) if self.mcp_config_source else None
                    ),
                    "mcp_config_searched_from": (
                        str(self.mcp_config_searched_from)
                        if self.mcp_config_searched_from
                        else None
                    ),
                    "mcp_config_explicit": self.mcp_config_explicit,
                    "mcp_config_reason": self.mcp_config_reason,
                }
            )
            try:
                existing = self.read_manifest()
            except (OSError, ValueError, RecursionError):
                # Only the degradation time below is read back on this path, and a file this
                # read cannot take yields none to keep.
                existing = {}
        else:
            # A manifest that is gone, or that is malformed JSON (JSONDecodeError), holds
            # nothing this code can read back, and one that is JSON but not an object holds
            # no provenance; any other error reading it leaves a file that may hold
            # provenance, so that error stops the write.
            try:
                existing = self.read_manifest()
            except (FileNotFoundError, json.JSONDecodeError):
                existing = {}
            if isinstance(existing, dict):
                for key in (
                    "cwd",
                    "mcp_config_source",
                    "mcp_config_searched_from",
                    "mcp_config_explicit",
                    "mcp_config_reason",
                ):
                    if key in existing:
                        payload[key] = existing[key]
        # The signal persistence degradation time is once per run (ADR-0104 Amendment 3): the
        # earliest on file stays, so neither the run's own setup write nor the whole-file write of
        # another invocation sharing its run id erases or moves it.
        if isinstance(existing, dict):
            _carry_earliest_time(payload, existing.get(SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD))
        _atomic_write_json(self.manifest_path, payload)

    def read_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {}
        return json.loads(self.manifest_path.read_text())

    def with_execution_provenance(
        self,
        *,
        cwd: str | os.PathLike,
        mcp_resolution: McpResolution,
        mcp_disabled: bool = False,
    ) -> RunDir:
        """Return this run with resolved execution context bound to future writes.

        ``allocate_run`` writes the running placeholder immediately. CLI call
        sites then bind the submit-time MCP result before their first live
        persistence write; rewrite that placeholder too so even an early
        failure leaves the same provenance as later lifecycle snapshots.
        """
        resolved_cwd = Path(cwd).expanduser().resolve()
        source = mcp_resolution.source
        updated = replace(
            self,
            cwd=resolved_cwd,
            mcp_config_source=source.expanduser().resolve() if source else None,
            mcp_config_searched_from=mcp_resolution.searched_from.expanduser().resolve(),
            mcp_config_explicit=mcp_resolution.explicit,
            mcp_config_reason=("mcp_disabled_by_caller" if mcp_disabled else mcp_resolution.reason),
        )
        if not self.manifest_path.exists():
            return updated
        # Read and rewrite under one hold of the lock, so a write by another
        # invocation sharing this run id cannot land between them and be undone.
        with _run_manifest_lock(self.state_root, required=False):
            manifest = self.read_manifest()
            if manifest:
                updated._write_manifest_holding_lock(manifest)
        return updated

    # ── Notify-outcome I/O (separate from the manifest; see notify_settings.py) ──

    @property
    def notify_outcome_path(self) -> Path:
        return self.state_root / "notify_outcome.json"

    def write_notify_outcome(self, data: dict) -> None:
        """Atomically replace notify_outcome.json; never merges with a prior
        outcome and never touches the manifest."""
        self.state_root.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(self.notify_outcome_path, data)

    @property
    def notify_stderr_path(self) -> Path:
        return self.state_root / "notify_stderr.log"

    def write_notify_stderr(self, text: str) -> Path:
        """Capture a notify adapter's stderr to an owner-only file (0600).

        Adapter output is free text that may carry a credential from any
        source, so it is written once, readable only by the user running
        the process, and referenced by path everywhere else instead of
        being copied into records or log lines.
        """
        self.state_root.mkdir(parents=True, exist_ok=True)
        path = self.notify_stderr_path
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        return path

    # ── Directory setup ─────────────────────────────────────────────

    def ensure_state_dirs(self) -> None:
        ensure_lionagi_dir(self.branches_dir)
        ensure_lionagi_dir(self.stream_dir)

    def ensure_artifact_root(self) -> None:
        ensure_lionagi_dir(self.artifact_root)


def _record_persistence_degraded(
    exc: BaseException,
    *,
    run: RunDir | None = None,
    run_id: str | None = None,
    run_manifest: dict[str, Any] | None = None,
) -> str:
    """Record why lifecycle persistence was disabled outside the failed store."""
    reason = repr(exc)
    if run_manifest is not None:
        run_manifest[PERSISTENCE_DEGRADED_REASON_FIELD] = reason
    resolved_id = run.run_id if run is not None else run_id
    manifest_path = (
        run.manifest_path
        if run is not None
        else (RUNS_ROOT / resolved_id / "run.json" if resolved_id else None)
    )
    if manifest_path is None:
        _log.warning("could not record persistence degradation: no active run id")
        return reason
    try:
        with _run_manifest_lock(manifest_path.parent, required=False):
            manifest = json.loads(manifest_path.read_text())
            if not isinstance(manifest, dict):
                raise TypeError("run manifest is not a JSON object")
            kept = manifest.get(SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD)
            if run_manifest is not None:
                manifest.update(run_manifest)
            _carry_earliest_time(manifest, kept)
            _drop_a_time_that_is_not_one(manifest)
            manifest[PERSISTENCE_DEGRADED_REASON_FIELD] = reason
            _atomic_write_json(manifest_path, manifest)
    except Exception as record_exc:  # noqa: BLE001 — degradation must not become a run failure
        _log.warning(
            "could not record persistence degradation for run %s: %r",
            resolved_id,
            record_exc,
            exc_info=True,
        )
    return reason


def lost_terminal_write(
    live: dict | None,
    *,
    requested: str,
    exception: BaseException | None,
    returned: str,
) -> dict[str, Any] | None:
    """The terminal status a run decided that its store did not take, or None.

    *returned* is what teardown handed back: the row's status when its terminal write failed
    (usually `running`), `unknown` when even that read failed. The record exists only when a
    store read or write failed teardown (the store error teardown kept in *live*), the run
    decided a terminal status and *returned* is not terminal. Teardown that failed for any
    other reason, such as an artifact removed while it was being verified, never reached a
    write the store could refuse. A terminal *returned* that differs from the decision is
    another writer's record, not a lost write, and a run that decided `running` (a linked
    engine session still runs) decided no end. The decision is the outcome teardown recorded in
    *live* before its write, or, when teardown failed before deciding, the terminal status it
    was asked to write, with the code teardown resolved for it, or the one `resolve_run_reason`
    gives when teardown failed before resolving one. The caller leaves out a leg that defers
    its end to an auto-resume.
    """
    from lionagi.state.db import SESSION_TERMINAL_STATUSES

    if not live or returned in SESSION_TERMINAL_STATUSES:
        return None
    if live.get("teardown_store_error") is None:
        return None
    decision = live.get("terminal_reason")
    if decision:
        status = decision[0]
        code = decision[2] if len(decision) > 2 else None
    else:
        status, code = requested, live.get("requested_reason_code")
    if status not in SESSION_TERMINAL_STATUSES:
        return None
    if code is None:
        code = resolve_run_reason(status=status, exception=exception)[0]
    return {"status": status, "reason_code": code, "at": time.time()}


def _store_error_in(error: BaseException | None) -> BaseException | None:
    """The store error *error* is, raises from, or carries inside exception groups.

    A database error, or the store having no row for its write, anywhere in nested exception
    groups. An error raised while another was being handled is not taken as caused by it.
    """
    import sqlite3

    from sqlalchemy.exc import SQLAlchemyError

    from lionagi.ln.concurrency._compat import BaseExceptionGroup
    from lionagi.state.lifecycle import LifecycleNotFoundError

    seen: set[int] = set()
    pending: list[BaseException] = [error] if error is not None else []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, sqlite3.Error | SQLAlchemyError | LifecycleNotFoundError):
            return current
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return None


def _is_time(value: Any) -> bool:
    """A finite number that is not a bool, the only value a reader of run.json takes as a time.

    `math.isfinite` converts to float first, and an integer past float range (JSON's integers
    are unbounded) makes that conversion raise; such a value is not a time either.
    """
    import math

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _drop_a_time_that_is_not_one(manifest: dict[str, Any]) -> None:
    """Remove a degradation time no reader takes as one from *manifest*, read from run.json, as
    `RunDir.write_manifest` leaves it out: a NaN or an infinity there would make the write fail."""
    if not _is_time(manifest.get(SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD)):
        manifest.pop(SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD, None)


def _carry_earliest_time(payload: dict[str, Any], kept: Any) -> None:
    """Put *kept*, the degradation time on file, into *payload* unless *payload* has one no later."""
    mine = payload.get(SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD)
    if _is_time(kept) and not (_is_time(mine) and mine <= kept):
        payload[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD] = kept


def _write_signal_persistence_degraded_at(state_root: Path, at: float) -> None:
    """Add *at* to the run.json in *state_root* unless it already holds a time no later.

    Runs on its own thread and takes only the run directory's manifest lock, never the state
    store's writer. A failure is logged, since nothing above this frame can catch it.
    """
    manifest_path = state_root / "run.json"
    try:
        with _run_manifest_lock(state_root, required=False):
            manifest = json.loads(manifest_path.read_text())
            if not isinstance(manifest, dict):
                raise TypeError("run manifest is not a JSON object")
            kept = manifest.get(SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD)
            if not (_is_time(kept) and kept <= at):
                manifest[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD] = at
                _atomic_write_json(manifest_path, manifest)
    except Exception as exc:  # noqa: BLE001 — a marker that cannot be written must not fail the run
        _log.warning(
            "could not record signal persistence degradation in %s: %r", manifest_path, exc
        )


def _write_terminal_write_lost(state_root: Path, record: dict[str, Any]) -> None:
    """Set `terminal_write_lost` to *record* on the run.json in *state_root*, under its manifest
    lock, and leave every other field as the file holds it, but drop a malformed degradation time.

    For a run copying its auto-resumed leg's record onto its own run.json: the run has nothing
    else left to say there, and what another invocation sharing its run id wrote since the run's
    last write stays.
    """
    manifest_path = state_root / "run.json"
    with _run_manifest_lock(state_root, required=False):
        manifest = json.loads(manifest_path.read_text())
        if not isinstance(manifest, dict):
            raise TypeError("run manifest is not a JSON object")
        manifest[TERMINAL_WRITE_LOST_FIELD] = record
        _drop_a_time_that_is_not_one(manifest)
        _atomic_write_json(manifest_path, manifest)


def _signal_window_marker(run_manifest: dict[str, Any], state_root: Path) -> Callable[[], None]:
    """The callback a signal persistence binding calls on the event loop when its skip window opens.

    It sets the time on *run_manifest*, so the run's later whole-file writes carry it, and hands
    the file write to a thread of its own: the window exists so that an emit never waits on the
    store, and the manifest lock another writer may hold must not make it wait on a file. The
    thread is a daemon, so a lock held at exit cannot hold the process; a run that exits normally
    writes the field with its manifest anyway. Once per run: a time already on *run_manifest* is
    kept.
    """

    def mark() -> None:
        if run_manifest.get(SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD) is not None:
            return
        at = time.time()
        run_manifest[SIGNAL_PERSISTENCE_DEGRADED_AT_FIELD] = at
        try:
            threading.Thread(
                target=_write_signal_persistence_degraded_at,
                args=(state_root, at),
                name="lionagi-signal-window-marker",
                daemon=True,
            ).start()
        except Exception as exc:  # noqa: BLE001 — the run's own manifest write still carries it
            _log.warning("could not hand off the signal persistence degradation record: %r", exc)

    return mark


def allocate_run(
    save_dir: str | os.PathLike | None = None,
    run_id: str | None = None,
) -> RunDir:
    """Allocate a run dir whose provenance is a placeholder (the current directory,
    MCP reason ``not_resolved``) until :meth:`RunDir.with_execution_provenance` binds it."""
    global _ALLOCATED_RUN_ID

    inherited_run_id = current_run_id()
    rid = run_id or inherited_run_id or _new_run_id()
    if inherited_run_id is not None:
        os.environ.pop(_RUN_ID_ENV_VAR, None)
    _ALLOCATED_RUN_ID = rid
    state_root = RUNS_ROOT / rid

    if save_dir is not None:
        artifact_root = Path(save_dir).expanduser().resolve()
    else:
        artifact_root = state_root / "artifacts"

    run = RunDir(
        run_id=rid,
        state_root=state_root,
        artifact_root=artifact_root,
        cwd=Path.cwd().resolve(),
        mcp_config_reason="not_resolved",
    )
    run.ensure_state_dirs()
    run.ensure_artifact_root()
    # From here on there is durable state on disk under this run id, so a later
    # failure is a failed run and must not be reported as an unusable
    # environment. Marked here rather than at the call sites so every caller,
    # including ones added later, is covered.
    mark_run_allocated()
    run.write_manifest(
        {
            "status": "running",
            "started_at": time.time(),
            "ended_at": None,
        }
    )
    return run


def _branch_dirs() -> list[tuple[str | None, Path, str]]:
    """Every directory that can hold a branch file, with its run id and suffix.

    Newest run first, so an exact hit is found in the order a caller expects.
    """
    places: list[tuple[str | None, Path, str]] = []
    if RUNS_ROOT.exists():
        for run_dir in sorted(RUNS_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            branches = run_dir / "branches"
            if run_dir.is_dir() and branches.exists():
                places.append((run_dir.name, branches, ".json"))
    if _LEGACY_AGENTS_ROOT.exists():
        places.extend(
            (None, provider_dir, "")
            for provider_dir in sorted(_LEGACY_AGENTS_ROOT.iterdir())
            if provider_dir.is_dir()
        )
    return places


def find_branch(branch_id: str) -> tuple[str | None, Path]:
    """Locate a branch JSON; returns (run_id, path), run_id=None for legacy logs/agents/ storage.

    Branch ids may be given truncated, so a prefix is accepted. It is resolved
    in two passes over every place a branch can live rather than one pass that
    accepts whatever it finds first: an exact id is a complete answer and must
    win wherever it lives, and a prefix that fits more than one branch has no
    correct winner. Resuming acts, so picking one of several would silently put
    a new leg on a branch the caller did not name.
    """
    places = _branch_dirs()

    for run_id, directory, suffix in places:
        exact = directory / f"{branch_id}{suffix}"
        if exact.exists():
            return run_id, exact

    matches: list[tuple[str | None, Path]] = []
    for run_id, directory, suffix in places:
        # startswith re-checks the glob: a case-insensitive filesystem matches
        # names the id does not actually prefix.
        matches.extend(
            (run_id, match)
            for match in sorted(directory.glob(f"{branch_id}*{suffix}"))
            if match.name.startswith(branch_id)
        )

    if len(matches) > 1:
        raise AmbiguousIdError(
            branch_id, "branch", [m.name.removesuffix(".json") for _, m in matches]
        )
    if matches:
        return matches[0]

    raise FileNotFoundError(f"No branch log found for id {branch_id!r}")


def load_last_branch() -> tuple[str | None, str]:
    """Read the last-branch pointer; returns (run_id, branch_id), run_id=None for pre-run-scoped schema."""
    if not _LAST_BRANCH_POINTER.exists():
        raise FileNotFoundError(
            f"No last-branch pointer at {_LAST_BRANCH_POINTER}. "
            "Run `li agent <model> <prompt>` at least once before using -c."
        )
    data = json.loads(_LAST_BRANCH_POINTER.read_text())
    branch_id = data["branch_id"]
    run_id = data.get("run_id")  # None for legacy pointers
    return run_id, branch_id


def save_last_branch_pointer(run_id: str, branch_id: str) -> None:
    """Record which branch `--continue` should pick up next. Best effort.

    This is a convenience pointer, and it is written late — after a run has
    produced its answer and, in the agent path, before that answer's terminal
    notice goes out. Letting a filesystem error escape from here would cost the
    caller the notice and the return value both, to protect a file whose only
    job is to save someone typing a branch id.

    So the failure is reported and not raised. It is reported rather than
    swallowed because the next `--continue` will silently pick up an older run,
    and that is confusing precisely when nobody remembers this warning.
    """
    from lionagi.cli._logging import warn

    try:
        ensure_lionagi_dir(LIONAGI_HOME)
        _LAST_BRANCH_POINTER.write_text(json.dumps({"run_id": run_id, "branch_id": branch_id}))
    except Exception as exc:  # noqa: BLE001 — a convenience pointer never fails a run
        _log.warning("could not write the last-branch pointer: %r", exc, exc_info=exc)
        warn(
            f"could not record this run as the last branch ({exc}); "
            f"`li agent -c` will resume an earlier run instead of this one"
        )


def list_runs(limit: int | None = None) -> list[RunDir]:
    """Return all runs under RUNS_ROOT, newest first (by mtime)."""
    if not RUNS_ROOT.exists():
        return []
    dirs = [p for p in RUNS_ROOT.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if limit is not None:
        dirs = dirs[:limit]
    out: list[RunDir] = []
    for d in dirs:
        manifest_path = d / "run.json"
        artifact_root = d / "artifacts"
        manifest: dict[str, Any] = {}
        if manifest_path.exists():
            try:
                loaded = json.loads(manifest_path.read_text())
                manifest = loaded if isinstance(loaded, dict) else {}
                art = manifest.get("artifact_root")
                if art:
                    artifact_root = Path(art)
            except (OSError, json.JSONDecodeError):
                pass
        cwd = manifest.get("cwd")
        mcp_source = manifest.get("mcp_config_source")
        mcp_searched_from = manifest.get("mcp_config_searched_from")
        out.append(
            RunDir(
                run_id=d.name,
                state_root=d,
                artifact_root=artifact_root,
                cwd=Path(cwd) if isinstance(cwd, str) else None,
                mcp_config_source=Path(mcp_source) if isinstance(mcp_source, str) else None,
                mcp_config_searched_from=(
                    Path(mcp_searched_from) if isinstance(mcp_searched_from, str) else None
                ),
                mcp_config_explicit=(
                    manifest.get("mcp_config_explicit")
                    if isinstance(manifest.get("mcp_config_explicit"), bool)
                    else None
                ),
                mcp_config_reason=(
                    manifest.get("mcp_config_reason")
                    if isinstance(manifest.get("mcp_config_reason"), str)
                    else None
                ),
            )
        )
    return out


_log = logging.getLogger("lionagi.cli")


def resolve_run_reason(
    *,
    status: str,
    exception: BaseException | None,
) -> tuple[str, str, list[dict] | None]:
    from lionagi.state.reasons import RunReasons

    if status == "completed":
        return RunReasons.COMPLETED_OK, "Run completed successfully.", None
    if status == "completed_empty":
        return (
            RunReasons.COMPLETED_EMPTY_NO_EVIDENCE,
            "Run exited clean but produced no commits ahead of base and no artifacts.",
            None,
        )
    if status == "timed_out":
        return RunReasons.TIMED_OUT_DEADLINE, "Run exceeded the configured timeout.", None
    if status == "aborted":
        return RunReasons.CANCELLED_SIGINT, "User pressed Ctrl-C (SIGINT).", None
    if status == "cancelled":
        from lionagi.ln.concurrency.utils import (
            SigtermInterrupt,
            consume_sigterm_received,
        )

        # At teardown the surfaced exception is usually a plain CancelledError
        # even when an external SIGTERM caused it — SigtermInterrupt is only
        # raised after the worker thread joins, after this record is stamped.
        # The handler's process-wide latch is the reliable signal. Consume it
        # unconditionally (before the branch) so a later, unrelated run/test
        # can't inherit a latch left set on the explicit-SigtermInterrupt path.
        sigterm_latched = consume_sigterm_received()
        if isinstance(exception, SigtermInterrupt) or sigterm_latched:
            return (
                RunReasons.CANCELLED_SIGTERM,
                "sigterm_external: process received an external SIGTERM mid-run.",
                None,
            )
        return (
            RunReasons.CANCELLED_SYSTEM,
            "Task cancelled by the runtime (anyio CancelledError).",
            None,
        )
    if exception is not None:
        # iModel re-raises every stream failure as a ValueError, so a provider's typed
        # error (a CLI's quota refusal, say) reaches here as that error's cause.
        cause, seen = exception, set()
        while cause is not None and not isinstance(cause, ProviderError) and id(cause) not in seen:
            seen.add(id(cause))
            cause = cause.__cause__
        if isinstance(cause, ProviderError):
            code = (
                RunReasons.FAILED_PROVIDER_RETRYABLE
                if cause.retryable
                else RunReasons.FAILED_PROVIDER_NONRETRYABLE
            )
            return code, f"{type(cause).__name__}: {cause}", None
        return RunReasons.FAILED_EXCEPTION, f"{type(exception).__name__}: {exception}", None
    return RunReasons.FAILED_EXCEPTION, "Run failed.", None


async def _linked_engine_session(
    db: StateDB,
    engine_session_uid: str | None,
    *,
    retries: int = 3,
    retry_interval: float = 0.1,
) -> dict[str, Any] | None:
    """The claude/codex-mirror session row for a CLI provider's real engine session.

    Retries a bounded number of times, since the mirror row may not be written yet at teardown.
    """
    if not engine_session_uid:
        return None

    from lionagi.ln.concurrency import sleep
    from lionagi.state.claude_mirror import session_db_id

    db_id = session_db_id(engine_session_uid)
    linked = await db.get_session(db_id)
    if linked is not None:
        return linked
    for _ in range(retries):
        await sleep(retry_interval)
        linked = await db.get_session(db_id)
        if linked is not None:
            return linked
    return None


async def _teardown_write(committed: list[str] | None, name: str, call: Any) -> Any:
    """Run one of teardown's own store writes, recording *name* once it returns.

    Each store operation opens its own transaction, so a refusal raised by a later write names
    only that write and reads as if nothing had committed. The names collected here are the ones
    the run's reason carries instead (ADR-0128 D6).
    """
    result = await call
    if committed is not None:
        committed.append(name)
    return result


@dataclass(frozen=True)
class _TerminalStatus:
    """The terminal session status a teardown decided, written once the lifecycle emits have run."""

    status: str
    reason_code: str
    reason_summary: str
    evidence_refs: list[dict] | None
    metadata: dict | None
    ended_at: float
    duration_ms: float | None


def _run_may_clear_current_phase(db: StateDB) -> bool:
    """Whether this run may clear the stored ``current_phase`` in a terminal status write.

    Natively a run owns the column and always clears it with the status. In compatibility mode
    every write this run made was authorized against the same profile, so a profile that does not
    admit ``sessions.current_phase`` means the run never wrote a phase: there is nothing of its own
    to clear, and naming the column would make the profile refuse the status write itself
    (ADR-0128 D5, D6).
    """
    if db.mode != "compatibility":
        return True
    profile = getattr(db, "compatibility", None)
    writes = getattr(profile, "writes", None)
    if writes is None:
        return False
    sessions = writes.get("sessions")
    return sessions is not None and "current_phase" in sessions.update


def _branch_end_is_this_runs(db: StateDB, branch_id: str) -> bool:
    """Whether a BRANCH_END write for *branch_id* is this run's to make.

    Orchestration registers each worker lazily, so a registered worker that produced no message
    has no branch row. Natively every branch is the run's, as before; in compatibility mode a
    branch row another run created is not written, and emitting for a row this run did not create
    would refuse the whole teardown (ADR-0128 D5).
    """
    if db.mode != "compatibility":
        return True
    return branch_id in getattr(db, "_compat_created", {}).get("branches", ())


def _terminal_status_extra_fields(
    db: StateDB, *, ended_at: float, duration_ms: float | None
) -> dict[str, Any]:
    """The same-row columns this run's terminal status write may set.

    ``current_phase`` is included only where :func:`_run_may_clear_current_phase` admits it, so a
    profile without that column grant still takes the status write.
    """
    fields: dict[str, Any] = {"ended_at": ended_at}
    if duration_ms is not None:
        fields["duration_ms"] = duration_ms
    if _run_may_clear_current_phase(db):
        fields["current_phase"] = None
    return fields


async def _teardown_common(
    db: StateDB,
    *,
    session_id: str,
    session_prog_id: str,
    status: str,
    exception: BaseException | None,
    artifacts_path: str | None,
    artifact_contract: dict | None,
    extras: dict | None = None,
    identity_markers: dict | None = None,
    escalated_evidence: list[dict] | None = None,
    failed_operation_evidence: list[dict] | None = None,
    spawn_refusal_evidence: list[dict] | None = None,
    finalize_error: dict | None = None,
    artifact_write_error: dict | None = None,
    gate_rejected_evidence: list[dict] | None = None,
    cwd: str | None = None,
    engine_session_uid: str | None = None,
    defer_terminal: bool = False,
    reason_out: dict | None = None,
    committed: list[str] | None = None,
    terminal_out: list[_TerminalStatus] | None = None,
) -> str:
    """Run the teardown's store writes and decide the run's terminal status.

    By default this writes the terminal session status itself and returns it. When
    *terminal_out* is given, the decision is appended there instead and no status write is made:
    the caller writes it, once the lifecycle emits have run, so a refusal those emits record is
    the run's reason rather than landing after a status already written (ADR-0128 D6).
    """
    from lionagi.state.artifact_verifier import (
        missing_artifact_evidence,
        missing_artifact_summary,
        verify_artifact_contract,
    )

    if defer_terminal:
        # A resumed leg on this same session owns the real terminal write (ADR-0035);
        # skip the DB mutation here and let the caller's non-status bookkeeping run.
        return status

    all_msgs = await db.get_progression(session_prog_id)
    completion_evidence_msgs = list(all_msgs)

    # Fetched before this call's own write so started_at reflects session
    # creation, not a value this same update is about to touch.
    session_before_teardown = await db.get_session(session_id) or {}

    # ended_at and duration_ms are terminal fields and must land in the same
    # atomic write as the status transition below (see the
    # CAS/TransitionRejectedError branch) -- never written here on their own, or
    # a failed/lost status write leaves a row with status="running" carrying a
    # non-null end time and duration. duration_ms is derived from ended_at, so
    # it inherits that requirement rather than merely resembling it.
    ended_at = time.time()
    duration_ms: float | None = None
    started_at = session_before_teardown.get("started_at")
    if isinstance(started_at, int | float):
        # The prerequisite for telling "never started" / "hung before first
        # request" / "request sent, no response" apart on a terminal row --
        # duration_ms was previously left NULL on every session regardless of
        # outcome, which is loudest on a zero-turn timeout: the record could
        # not even say how long nothing happened for.
        duration_ms = max(0.0, (ended_at - started_at) * 1000)
    update_kwargs: dict[str, Any] = {}
    if all_msgs:
        update_kwargs["first_msg_id"] = all_msgs[0]
        update_kwargs["last_msg_id"] = all_msgs[-1]

    if extras:
        # NOT routed through merge_session_node_metadata(): `extras` here can
        # carry a nested-dict value (finalize_orchestration's
        # "khive_injection" telemetry block, set on env._finalize_extras
        # before this runs -- see _orchestration.py's finalize_orchestration
        # and flow.py/fanout.py's stop_live_persist call in their `finally`),
        # and the atomic merge intentionally rejects a dict-valued patch key
        # (sqlite/postgres would merge it differently -- see
        # StateDB._merge_node_metadata). Switching this call to the atomic
        # merge would turn today's benign-if-racy read-modify-write into a
        # hard ValueError on every run with injection activity. So this stays
        # an open instance of the clobber class that the stale-sweep path in
        # kill.py closes; closing it safely needs a schema decision (flatten
        # khive_injection to scalar keys, or a merge-helper variant that
        # tolerates one level of nested object).
        markers = identity_markers or {}
        if db.mode == "compatibility":
            # A compatibility-mode store merges node_metadata by key inside its write, so it is
            # handed only this run's keys and keeps any other writer added since the read above.
            update_kwargs["node_metadata"] = {**extras, **markers}
        else:
            existing_metadata = session_before_teardown.get("node_metadata") or {}
            if isinstance(existing_metadata, str):
                try:
                    existing_metadata = json.loads(existing_metadata)
                except (TypeError, ValueError):
                    existing_metadata = {}
            if not isinstance(existing_metadata, dict):
                existing_metadata = {}
            update_kwargs["node_metadata"] = json.dumps({**existing_metadata, **extras, **markers})

    if update_kwargs:
        await _teardown_write(
            committed, "update_session", db.update_session(session_id, **update_kwargs)
        )

    reason_code, reason_summary, evidence_refs = resolve_run_reason(
        status=status, exception=exception
    )
    if reason_out is not None:
        # Resolving consumes the SIGTERM latch, so a write below that fails before the outcome is
        # decided leaves the run this code for the status it asked for rather than a second read.
        reason_out["requested_reason_code"] = reason_code
    metadata: dict | None = None
    if exception is not None:
        metadata = {"exception_class": type(exception).__name__}

    session_row = await db.get_session(session_id) or {}
    contract = artifact_contract or session_row.get("artifact_contract_json")
    artifacts_root = artifacts_path or session_row.get("artifacts_path")
    verification = verify_artifact_contract(contract, artifacts_root=artifacts_root)
    if (
        reason_out is not None
        and status == "completed"
        and verification
        and verification["status"] == "failed"
    ):
        from lionagi.state.reasons import RunReasons

        # A completed request whose required artifact is missing is decided here: nothing below
        # turns a `failed` status back or changes its code, so a write the store refuses from
        # here on leaves this decision, not the request.
        reason_out["terminal_reason"] = (
            "failed",
            missing_artifact_summary(verification["missing_required"]),
            RunReasons.FAILED_MISSING_ARTIFACT,
        )
    await _teardown_write(
        committed,
        "update_artifact_verification",
        db.update_artifact_verification(session_id, verification),
    )

    final_status = status
    final_reason_code = reason_code
    final_reason_summary = reason_summary
    final_evidence_refs = evidence_refs

    # Suppress a phantom "failed" only for this exact unclassified ProviderError class
    # when the linked engine session is still alive/completed; exact-type (not isinstance)
    # so genuine ProviderQuotaError/AuthError/ContextError subclasses still fail loud.
    if final_status == "failed" and type(exception) is ProviderError and engine_session_uid:
        from lionagi.state.claude_mirror import session_db_id
        from lionagi.state.db import SESSION_TERMINAL_STATUSES
        from lionagi.state.reasons import RunReasons

        linked_id = session_db_id(engine_session_uid)
        linked = await _linked_engine_session(db, engine_session_uid)

        # Record the link durably (id is deterministic) so `li monitor run <id>` can
        # resolve status later, even if this teardown's bounded wait ran out first.
        # Merged atomically -- `linked_id` is a single scalar, so unlike the
        # extras merge above there is no nested-value contract to give up by
        # using merge_session_node_metadata() instead of a read here plus a
        # whole-column update_session() write below.
        metadata = dict(metadata or {})
        metadata["linked_engine_session_id"] = linked_id
        await _teardown_write(
            committed,
            "merge_session_node_metadata",
            db.merge_session_node_metadata(session_id, {"linked_engine_session_id": linked_id}),
        )

        if linked is not None and linked["status"] in SESSION_TERMINAL_STATUSES:
            reason_by_status = {
                "completed": RunReasons.COMPLETED_OK,
                "completed_empty": RunReasons.COMPLETED_EMPTY_NO_EVIDENCE,
                "failed": RunReasons.FAILED_EXCEPTION,
                "timed_out": RunReasons.TIMED_OUT_DEADLINE,
                "aborted": RunReasons.CANCELLED_SIGINT,
                "cancelled": RunReasons.CANCELLED_SYSTEM,
            }
            final_status = linked["status"]
            final_reason_code = reason_by_status.get(linked["status"], RunReasons.FAILED_EXCEPTION)
            final_reason_summary = (
                f"reconciled to linked engine session {linked_id} terminal status "
                f"{linked['status']!r}"
            )
            final_evidence_refs = [{"kind": "session", "id": linked_id, "label": linked["status"]}]
            linked_prog_id = linked.get("progression_id")
            if linked_prog_id:
                completion_evidence_msgs.extend(await db.get_progression(linked_prog_id))
        elif linked is not None and linked["status"] == "running":
            final_status = "running"
            final_reason_code = RunReasons.STARTED_OK
            final_reason_summary = (
                f"suppressed phantom 'failed': linked engine session {linked_id} is still running"
            )
            final_evidence_refs = [{"kind": "session", "id": linked_id, "label": "running"}]
        # else: engine uid was captured but no mirror row landed within the
        # bounded wait — can't confirm the engine is alive, so `failed` stands.

    if verification and verification["status"] == "failed":
        from lionagi.state.reasons import RunReasons

        missing = verification["missing_required"]
        if final_status == "completed":
            final_status = "failed"
            final_reason_code = RunReasons.FAILED_MISSING_ARTIFACT
            final_reason_summary = missing_artifact_summary(missing)
            final_evidence_refs = missing_artifact_evidence(missing)
        else:
            metadata = dict(metadata or {})
            metadata["artifact_verification_status"] = verification["status"]
            metadata["missing_required_artifact_ids"] = [
                str(entry.get("id", "")) for entry in missing
            ]

    # Node-failure backstop: a DAG operation's invoke() can raise and be
    # recorded EventStatus.FAILED while still folding into
    # completed_operations alongside genuine completions, so a run whose
    # terminal (or any) node died could otherwise read as a clean completion.
    # Runs before the completion-trust gate below, since a fully-failed run
    # usually has no artifacts/commits either and would otherwise be demoted
    # to "completed_empty" before this evidence had a chance to apply.
    if failed_operation_evidence and final_status == "completed":
        from lionagi.state.reasons import RunReasons

        final_status = "failed"
        final_reason_code = RunReasons.FAILED_EXCEPTION
        ids = [str(e.get("id", "")) for e in failed_operation_evidence]
        final_reason_summary = (
            f"{len(failed_operation_evidence)} operation(s) failed: {', '.join(ids)}."
        )
        final_evidence_refs = failed_operation_evidence

    # Escalation backstop: a leg that gave up mid-run via EscalationRequest with
    # no artifact contract must not read as a clean completion. Same ordering
    # reason as the node-failure backstop above.
    if escalated_evidence and final_status == "completed":
        from lionagi.state.reasons import RunReasons

        final_status = "failed"
        final_reason_code = RunReasons.FAILED_ESCALATED
        ids = [str(e.get("id", "")) for e in escalated_evidence]
        final_reason_summary = (
            f"{len(escalated_evidence)} operation(s) escalated without producing "
            f"required output: {', '.join(ids)}."
        )
        final_evidence_refs = escalated_evidence

    # Gate-rejection backstop: a gate node rejected mid-DAG and the executor
    # short-circuited its dependent subtree to skipped instead of running those
    # nodes against the rejected baseline. That's a correct, deliberate stop,
    # not a failure -- status stays "completed" -- but the reason code must say
    # so explicitly. Runs before the completion-trust gate for the same reason
    # as the other two backstops, and unlike them leaves final_status at
    # "completed" rather than flipping to "failed", so it must run first to
    # place its evidence before the gate's no-evidence check.
    if gate_rejected_evidence and final_status == "completed":
        from lionagi.state.reasons import RunReasons

        metadata = dict(metadata or {})
        metadata["gate_rejections"] = gate_rejected_evidence
        final_reason_code = RunReasons.COMPLETED_GATE_REJECTED
        gate_names = ", ".join(
            str(e.get("label") or e.get("id") or "") for e in gate_rejected_evidence
        )
        final_reason_summary = (
            f"DAG completed successfully; {len(gate_rejected_evidence)} gate(s) rejected "
            f"({gate_names}) and their dependent subtree was short-circuited instead of "
            "running against the rejected baseline."
        )
        final_evidence_refs = gate_rejected_evidence

    # A capacity-refused SpawnRequest is work the live DAG asked to perform
    # but was not allowed to add. The planned graph may still complete, so keep
    # the terminal status at ``completed`` while distinguishing it from a run
    # that never needed to grow. The reason code reaches Studio status and the
    # terminal-callback envelope; the evidence names each requesting node.
    if spawn_refusal_evidence:
        from lionagi.state.reasons import RunReasons

        metadata = dict(metadata or {})
        metadata["spawn_refusal_count"] = len(spawn_refusal_evidence)
        metadata["spawn_refusals"] = spawn_refusal_evidence
        if final_status == "completed":
            final_reason_code = RunReasons.COMPLETED_SPAWN_REFUSED
            final_reason_summary = (
                f"{len(spawn_refusal_evidence)} reactive spawn request(s) were refused "
                "because the run's spawn capacity was exhausted."
            )
            final_evidence_refs = spawn_refusal_evidence

    # Completion-trust gate: don't accept "completed" on faith. Require a git trace
    # (commits ahead/dirty tree) or a durable assistant response as real evidence.
    # Skipped when gate_rejected_evidence fired above: a gate rejection is itself
    # real evidence of a deliberate stop, and (unlike the node-failure/escalation
    # backstops) it leaves final_status at "completed" rather than "failed", so
    # without this guard the demotion below would still run and overwrite the
    # gate-rejection reason/evidence with a plain no-evidence verdict.
    if (
        final_status == "completed"
        and not gate_rejected_evidence
        and not spawn_refusal_evidence
        and not (verification and verification.get("produced"))
    ):
        from lionagi.state.completion_evidence import (
            check_completion_evidence,
            has_completion_evidence,
        )
        from lionagi.state.reasons import RunReasons

        evidence = check_completion_evidence(cwd)
        if evidence["checked"]:
            has_output = await _has_assistant_output_evidence(db, completion_evidence_msgs)
            metadata = dict(metadata or {})
            metadata["completion_evidence"] = evidence
            metadata["has_assistant_output"] = has_output
            if not has_completion_evidence(evidence) and not has_output:
                final_status = "completed_empty"
                final_reason_code = RunReasons.COMPLETED_EMPTY_NO_EVIDENCE
                base_label = evidence.get("base_ref") or "base"
                final_reason_summary = (
                    f"No commits ahead of {base_label}, no artifacts produced, and no "
                    "assistant response recorded; working tree clean."
                )
                final_evidence_refs = [
                    {
                        "kind": "git_evidence",
                        "id": "completion_check",
                        "label": (
                            f"base={base_label} "
                            f"commits_ahead={evidence.get('commits_ahead')} "
                            f"dirty={evidence.get('dirty')}"
                        ),
                    }
                ]

    # The synthesis artifact IS the run's output. A DAG that completed but
    # whose output write raised has not delivered anything -- that is a real
    # failure of the run, not a best-effort finalize hiccup, so this flips
    # "completed" to "failed" instead of only annotating the reason code the
    # way COMPLETED_FINALIZE_ERROR below does.
    if artifact_write_error:
        from lionagi.state.reasons import RunReasons

        metadata = dict(metadata or {})
        metadata["artifact_write_error"] = artifact_write_error
        if final_status == "completed":
            final_status = "failed"
            final_reason_code = RunReasons.FAILED_ARTIFACT_WRITE
            final_reason_summary = (
                "DAG completed successfully but writing its output artifact raised "
                f"{artifact_write_error.get('error_class', 'an error')}: "
                f"{artifact_write_error.get('error', '')}"
            )
            final_evidence_refs = [
                {
                    "kind": "artifact_write_error",
                    "id": artifact_write_error.get("error_class", "error"),
                    "label": artifact_write_error.get("error", ""),
                }
            ]

    # A post-completion finalize step (persistence/team-teardown) raised after
    # the DAG itself already produced its result. That failure is real and must
    # not be silently dropped, but it is not a DAG failure either — surface it
    # via reason_code/metadata only, never by overwriting a "completed" status.
    if finalize_error:
        from lionagi.state.reasons import RunReasons

        metadata = dict(metadata or {})
        metadata["finalize_error"] = finalize_error
        if final_status == "completed":
            final_reason_code = RunReasons.COMPLETED_FINALIZE_ERROR
            final_reason_summary = (
                "DAG completed successfully; a post-completion finalize step raised "
                f"{finalize_error.get('error_class', 'an error')}: "
                f"{finalize_error.get('error', '')}"
            )
            final_evidence_refs = [
                {
                    "kind": "finalize_error",
                    "id": finalize_error.get("error_class", "error"),
                    "label": finalize_error.get("error", ""),
                }
            ]

    if reason_out is not None:
        # The outcome this teardown decided for its own run, whether or not the write it
        # makes below lands: a row already terminal, or a write that loses its compare-and-set,
        # keeps the status and reason another run left on it (a resume reuses the session row).
        # The code rides third, for a run whose write is then lost to say what it decided.
        reason_out["terminal_reason"] = (final_status, final_reason_summary, final_reason_code)

    decision = _TerminalStatus(
        status=final_status,
        reason_code=final_reason_code,
        reason_summary=final_reason_summary,
        evidence_refs=final_evidence_refs,
        metadata=metadata,
        ended_at=ended_at,
        duration_ms=duration_ms,
    )
    if terminal_out is not None:
        # The caller writes the status itself, after the lifecycle emits.
        terminal_out.append(decision)
        return final_status
    # Snapshot of the status observed at the start of this teardown; used only as the CAS guard
    # (not updated_at, which this function may itself have touched).
    return await _write_terminal_session_status(
        db, session_id, decision, session_row.get("status"), committed=committed
    )


async def _write_terminal_session_status(
    db: StateDB,
    session_id: str,
    terminal: _TerminalStatus,
    observed_status: Any,
    *,
    committed: list[str] | None = None,
) -> str:
    """Write the session's terminal status, once the lifecycle emits have run.

    The status write is the run's last store write: a refusal a lifecycle handler records while it
    runs -- the BRANCH_END and SESSION_END default handlers among them -- then fails the run with
    that refusal instead of landing after a status the run already wrote (ADR-0128 D6).
    *observed_status* is the status read just before this write, so the write's compare-and-set
    reflects what is stored at the moment it runs rather than a read taken before the emits.
    """
    from lionagi.state.db import SESSION_TERMINAL_STATUSES, TransitionRejectedError

    if observed_status in SESSION_TERMINAL_STATUSES:
        # Already terminal (a concurrent writer finalized the row, or an earlier run did)
        # before this write -- skip it and report the outcome this teardown decided
        # (ADR-0035 protects the earlier record).
        if observed_status != terminal.status:
            _log.warning(
                "session %s already terminal at %r; this invocation's %r "
                "outcome was not persisted (ADR-0094 protects the earlier "
                "terminal record)",
                session_id,
                observed_status,
                terminal.status,
            )
        else:
            _log.debug(
                "session %s already terminal at %r; skipping duplicate status write",
                session_id,
                observed_status,
            )
        return terminal.status

    try:
        written = await _teardown_write(
            committed,
            "update_status",
            db.update_status(
                "session",
                session_id,
                new_status=terminal.status,
                reason_code=terminal.reason_code,
                reason_summary=terminal.reason_summary,
                evidence_refs=terminal.evidence_refs,
                source="executor",
                actor=session_id,
                metadata=terminal.metadata,
                expected_statuses={observed_status},
                extra_fields=_terminal_status_extra_fields(
                    db, ended_at=terminal.ended_at, duration_ms=terminal.duration_ms
                ),
            ),
        )
        if not written:
            # CAS miss: a concurrent teardown of the same session won the race.
            # Read back the persisted status rather than raising past callers.
            persisted = await db.get_session(session_id) or {}
            status = persisted.get("status", terminal.status)
            _log.debug(
                "session %s status changed under this teardown; using persisted status %s",
                session_id,
                status,
            )
            return status
    except TransitionRejectedError:
        # Defensive fallback: the row became terminal between this teardown's
        # read and the write despite the CAS guard above.
        persisted = await db.get_session(session_id) or {}
        status = persisted.get("status", terminal.status)
        _log.debug(
            "session %s already terminal (%s); skipped duplicate status write",
            session_id,
            status,
        )
        return status
    return terminal.status


async def _has_assistant_output_evidence(db: StateDB, message_ids: list[str]) -> bool:
    """Walk the progression newest-first; a non-empty assistant message counts as
    durable completion evidence even when there's no commit, dirty tree, or artifact."""
    for message_id in reversed(message_ids):
        msg = await db.get_message(message_id)
        if not msg or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except (ValueError, TypeError):
                content = {"assistant_response": content}
        text_val = ""
        if isinstance(content, dict):
            text_val = str(content.get("assistant_response") or content.get("content") or "")
        elif content:
            text_val = str(content)
        if text_val.strip():
            return True
    return False


def _resolve_project(project: str | None) -> tuple[str | None, str | None]:
    if project:
        return project, "explicit"
    from lionagi.cli._project import detect_project

    return detect_project()


async def teardown_persist(
    ctx: dict | None,
    *,
    status: str = "completed",
    exception: BaseException | None = None,
    extras: dict | None = None,
    escalated_evidence: list[dict] | None = None,
    failed_operation_evidence: list[dict] | None = None,
    spawn_refusal_evidence: list[dict] | None = None,
    finalize_error: dict | None = None,
    artifact_write_error: dict | None = None,
    gate_rejected_evidence: list[dict] | None = None,
    cwd: str | None = None,
    engine_session_uid: str | None = None,
    defer_terminal: bool = False,
) -> str:
    if ctx is None:
        return status

    ctx["worker_spawn_db_closed"] = None
    ctx["worker_spawn_teardown_status"] = None
    pending_worker_cancel = None
    owner = ctx.get("worker_spawn_owner")
    if owner is not None:
        while True:
            try:
                await owner.aclose()
                break
            except cancelled_exc_classes() as exc:
                if pending_worker_cancel is None:
                    pending_worker_cancel = exc
        if pending_worker_cancel is not None:
            status = "cancelled"
            exception = pending_worker_cancel
            defer_terminal = False

    db = ctx["db"]
    committed: list[str] = []
    try:
        await _flush_pending_message_events(ctx)
        # A signal write the store refused is not a dropped one to read past: the store declined
        # it for good, so this teardown fails with that refusal rather than writing a status that
        # would read as a run which succeeded without its signals (ADR-0128 D6).
        signal_refusal = _signal_persist_refusal(ctx)
        if signal_refusal is not None:
            raise signal_refusal
        decision: list[_TerminalStatus] = []
        final_status = await _teardown_common(
            db,
            session_id=ctx["session_id"],
            session_prog_id=ctx["session_prog_id"],
            status=status,
            exception=exception,
            artifacts_path=ctx.get("artifacts_path"),
            artifact_contract=ctx.get("artifact_contract"),
            extras=extras,
            identity_markers=ctx.get("identity_markers"),
            escalated_evidence=escalated_evidence,
            failed_operation_evidence=failed_operation_evidence,
            spawn_refusal_evidence=spawn_refusal_evidence,
            finalize_error=finalize_error,
            artifact_write_error=artifact_write_error,
            gate_rejected_evidence=gate_rejected_evidence,
            cwd=cwd,
            engine_session_uid=engine_session_uid,
            defer_terminal=defer_terminal,
            reason_out=ctx,
            committed=committed,
            terminal_out=decision,
        )

        # A signal emitted while this teardown ran is refused the same way as one emitted before
        # it: the store declined the write, so the run does not read as one that succeeded
        # without it (ADR-0128 D6). Checked before the terminal status write and again after the
        # emits below, so a refusal either side of them fails the run.
        signal_refusal = _signal_persist_refusal(ctx)
        if signal_refusal is not None:
            raise signal_refusal

        from lionagi.hooks.bus import HookPoint

        _unroute_message_hooks(ctx)

        session_obj = ctx.get("session")
        # Skip SESSION_END here when deferred: the resumed leg's own (non-deferred)
        # teardown emits it once, carrying cumulative usage for both legs.
        if session_obj is not None and not defer_terminal:
            err_str = str(exception) if exception is not None else None
            _usage: dict = {}
            _branch = ctx.get("branch")
            # Orchestrator/DAG sessions never set a singular ctx["branch"];
            # every leg (including the orchestrator branch itself) is
            # tracked in ctx["hooks"] as (branch, handler) pairs instead.
            _hook_branches = [b for b, _h in ctx.get("hooks", [])]
            try:
                if _branch is not None:
                    from lionagi.session.signal import _collect_branch_usage

                    _usage = _collect_branch_usage(_branch)
                elif _hook_branches:
                    from lionagi.session.signal import _collect_multi_branch_usage

                    _usage = _collect_multi_branch_usage(_hook_branches)
            except Exception:  # noqa: BLE001, S110
                pass

            # BRANCH_END safety net for legs that never reached a terminal
            # signal; finalize_branch()'s own guard skips branches a per-op
            # writer already finalized. See docs/internals/cli.md#_runspy-agent-session-setupteardown-adr-0035
            # for why final_status must stay a genuine terminal outcome here.
            from lionagi.state.db import SESSION_TERMINAL_STATUSES

            if final_status in SESSION_TERMINAL_STATUSES:
                _end_at = time.time()
                for _b in [_branch] if _branch is not None else _hook_branches:
                    if not _branch_end_is_this_runs(db, str(_b.id)):
                        continue
                    await session_obj.hooks.emit(
                        HookPoint.BRANCH_END,
                        branch_id=str(_b.id),
                        status=final_status,
                        ended_at=_end_at,
                        db=db,
                    )

            await session_obj.hooks.emit(
                HookPoint.SESSION_END,
                session_id=ctx["session_id"],
                status=final_status,
                error=err_str,
                db=db,
                **_usage,
            )

        # A signal write the emits above declined -- the signal sink keeps such a refusal rather
        # than raising it -- is not a dropped signal to read past: it fails the run here, before
        # the terminal status write, exactly as one recorded before the emits does (ADR-0128 D6).
        signal_refusal = _signal_persist_refusal(ctx)
        if signal_refusal is not None:
            raise signal_refusal

        # The terminal status write is the run's last store write, after the BRANCH_END and
        # SESSION_END emits. A refusal either emit records then leaves the run failed with that
        # refusal instead of standing beside a status the run had already written (ADR-0128 D6),
        # and the row is read here so the write's compare-and-set reflects the status stored once
        # the emits have run, rather than the one read before them.
        if decision and not defer_terminal:
            observed = (await db.get_session(ctx["session_id"]) or {}).get("status")
            final_status = await _write_terminal_session_status(
                db, ctx["session_id"], decision[0], observed, committed=committed
            )

        ctx["worker_spawn_teardown_status"] = final_status
        return final_status
    except Exception as exc:
        from lionagi.state.compat import refusal_in

        # Teardown writes run inside task groups, so the refusal can arrive wrapped beside
        # another failure; it is still the reason this run fails.
        refusal = refusal_in(exc)
        if refusal is not None:
            # The store refused a write in compatibility mode, so the run fails with that
            # refusal as its reason instead of reading back a status it could not write
            # (ADR-0128 D6). Nothing more is written and the message hooks are detached: the
            # refusal rose either from one of the teardown's own writes, before SESSION_END
            # was reached, or from a SESSION_END handler, after the emit below had run.
            _log.error("live persist teardown refused: %s", refusal)
            _unroute_message_hooks(ctx)
            reason = f"The store refused a write: {refusal}"
            if committed:
                # Each store operation commits on its own, so the refusal above reads as if the
                # teardown had committed nothing. Name what it did commit, so the run's record
                # does not send a reader looking for state that is already durable.
                reason += f"; already committed in this teardown: {', '.join(committed)}"
            ctx["terminal_reason"] = ("failed", reason)
            ctx["store_refusal"] = refusal
            ctx["worker_spawn_teardown_status"] = "failed"
            return "failed"
        _log.warning("live persist teardown failed: %s", exc, exc_info=True)
        # Only a store error (a database error, or no row for the write) says the store failed
        # teardown, so only that one leaves a terminal write the store may not have taken; a
        # failure from anywhere else (an artifact removed while it was verified) stopped
        # teardown before any refused write (ADR-0104 Amendment 3).
        store_error = _store_error_in(exc)
        if store_error is not None:
            ctx["teardown_store_error"] = store_error
        # A failure anywhere in the block above (any of _teardown_common's
        # separate write transactions, or the bookkeeping around them) must
        # not make this return the *requested* terminal status as if it had
        # landed -- that status may never have reached the database. Read
        # back what is actually durable instead; only fall back to the
        # caller's request if even that read fails.
        try:
            persisted = await db.get_session(ctx["session_id"])
        except Exception:  # noqa: BLE001 -- best-effort; the DB itself may be unreachable
            persisted = None
        if persisted is not None and persisted.get("status"):
            ctx["worker_spawn_teardown_status"] = persisted["status"]
            return persisted["status"]
        # The readback itself failed (or found nothing) -- there is no
        # evidence the requested terminal status ever reached the database.
        # Reporting `status` here would be the same false-terminal-status
        # class this teardown path exists to prevent, so surface an
        # explicit unknown outcome instead of a guess.
        ctx["worker_spawn_teardown_status"] = "unknown"
        return "unknown"
    finally:
        _session_obj = ctx.get("session")
        # Detach signal persistence on every path, so the observer handler cannot
        # fire after the db handle closes below, and hand back how many signals it
        # could not confirm it wrote, writes still in flight included (run.json reads
        # it from ctx).
        if _session_obj is not None:
            try:
                _session_obj.observer.unbind_db_persistence()
                ctx["signal_persist_dropped"] = _session_obj.observer.persist_dropped
            except Exception as _exc:  # noqa: BLE001
                _log.debug("signal persist unbind failed: %s", _exc)
        # Release branch ownership even when the bookkeeping above failed -- a
        # stranded owner marker would make the long-lived branch unresumable.
        if _session_obj is not None:
            for _b in [ctx.get("branch"), *(b for b, _h in ctx.get("hooks", []))]:
                if _b is None:
                    continue
                try:
                    _session_obj.remove_branch(_b)
                except Exception as _exc:  # noqa: BLE001
                    _log.debug("branch ownership release failed: %s", _exc)
        try:
            await db.close()
            ctx["worker_spawn_db_closed"] = db
        except Exception as exc:
            _log.warning("live persist db.close failed: %s", exc, exc_info=True)
        if ctx.get("shared_db_registered", True):
            # Ordinary CLI runs retain the historical sweep. Embedded daemon
            # callers opt out so teardown cannot close unrelated shared state.
            from lionagi.state.db import close_shared_db

            # The sweep raises for a handle it could not close (one another
            # event loop is opening). Teardown reports the run's outcome and
            # must not raise, so the refusal is logged like db.close() above.
            try:
                await close_shared_db()
            except Exception as exc:  # noqa: BLE001
                _log.warning("live persist shared db sweep failed: %s", exc, exc_info=True)

        if pending_worker_cancel is not None:
            raise pending_worker_cancel


def _signal_persist_refusal(ctx: dict) -> BaseException | None:
    """The first signal write this run's observer saw the store refuse, if any.

    A refusal is not a drop: the signal was not persisted and will not be retried, so teardown
    fails the run with it instead of reading back a status the store could not have accepted
    with the signal (ADR-0128 D6).
    """
    session_obj = ctx.get("session")
    observer = getattr(session_obj, "observer", None)
    return getattr(observer, "persist_refusal", None)


def _unroute_message_hooks(ctx: dict) -> None:
    from lionagi.hooks import unroute_message_persistence

    hook = ctx.get("hook")
    if hook is not None:
        unroute_message_persistence(ctx["branch"], hook)
    for branch, h in ctx.get("hooks", []):
        unroute_message_persistence(branch, h)


# Keep old names as aliases so callers don't break.
teardown_agent_persist = teardown_persist


async def teardown_orchestration_persist(*args, **kwargs) -> str:
    """Deprecated alias for :func:`teardown_persist`; delegates unchanged."""
    warnings.warn(
        "lionagi.cli._runs.teardown_orchestration_persist is deprecated; "
        "use lionagi.cli._runs.teardown_persist instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    return await teardown_persist(*args, **kwargs)


async def _open_shared_db(*, register: bool = True):
    """Open a StateDB, optionally registering it as the process-wide handle."""
    from lionagi.state.db import StateDB, register_shared_db, unregister_shared_db

    db = StateDB()
    try:
        await db.open()
        if register:
            # Ordinary CLI lifecycle hooks reuse this connection. Embedded
            # callers bind their observer/message handlers directly instead.
            await register_shared_db(db)
    except Exception:
        try:
            await db.close()
        except Exception as close_exc:
            _log.warning("fallback db.close after open failure also failed: %s", close_exc)
        if register:
            unregister_shared_db(db)
        raise
    return db


def _make_message_handler(
    db,
    branch_id: str,
    session_id: str,
    branch_prog_id: str,
    session_prog_id: str,
    *,
    dedup_set: set | None = None,
    new_msg_ids_list: list | None = None,
    on_first_msg=None,
    message_retry_queues: list | None = None,
):
    """Return an async _on_message handler for live DB persistence."""
    from lionagi.hooks._message_retry import MessagePersistRetryQueue, PendingMessageEvent
    from lionagi.ln import copy
    from lionagi.state.compat import StateCompatibilityRefused

    retry_queue = MessagePersistRetryQueue(
        db,
        logger=_log,
        owner=f"branch {branch_id}",
    )
    if message_retry_queues is not None:
        message_retry_queues.append(retry_queue)
    queued_adds: set[str] = set()

    async def _persist(msg, *, update=False):
        try:
            if not update and on_first_msg is not None:
                await on_first_msg()
            msg_dict = msg.to_dict(mode="db")
            msg_id = msg_dict["id"]
            if update and msg_id not in queued_adds:
                _log.warning(
                    "live persist skipped update for message %s: original write was never queued",
                    msg_id,
                )
                return
            append_to_progressions = not update and (dedup_set is None or msg_id not in dedup_set)
            on_persisted = None
            if append_to_progressions and new_msg_ids_list is not None:

                def _record_persisted() -> None:
                    new_msg_ids_list.append(msg_id)

                on_persisted = _record_persisted
            await retry_queue.submit(
                PendingMessageEvent(
                    message=copy(msg_dict),
                    session_id=session_id,
                    branch_progression_id=(branch_prog_id if append_to_progressions else None),
                    session_progression_id=(session_prog_id if append_to_progressions else None),
                    system_branch_id=branch_id
                    if not update and msg_dict.get("role") == "system"
                    else None,
                    activity_at=msg_dict.get("created_at"),
                    on_persisted=on_persisted,
                )
            )
            if not update:
                queued_adds.add(msg_id)
        except StateCompatibilityRefused:
            # A write the store refused in compatibility mode stops the run (ADR-0128 D6).
            raise
        except Exception as exc:
            _log.warning(
                "live persist write failed for branch %s: %s",
                branch_id,
                exc,
                exc_info=True,
            )

    async def _on_message(msg):
        await _persist(msg)

    async def _persist_update(msg):
        await _persist(msg, update=True)

    _on_message._persist_update = _persist_update
    return _on_message


def register_branch_hook(ctx: dict[str, Any], branch: Any) -> None:
    from lionagi.ln.concurrency import Lock

    db = ctx["db"]
    session_id = ctx["session_id"]
    session_prog_id = ctx["session_prog_id"]
    branch_id = str(branch.id)

    branch_prog_id = str(uuid.uuid4())
    ctx["branch_prog_ids"][branch_id] = branch_prog_id
    initialized = {"done": False}
    init_lock = Lock()

    async def _ensure_branch_row():
        if initialized["done"]:
            return
        async with init_lock:
            if initialized["done"]:
                return

            await db.create_progression(branch_prog_id)

            branch_dict = branch.to_dict(mode="db")
            node_meta = branch_dict.get("node_metadata") or {}
            if isinstance(node_meta, str):
                node_meta = json.loads(node_meta)
            if "chat_model" in branch_dict:
                node_meta["chat_model"] = branch_dict["chat_model"]
            node_meta = json.loads(json.dumps(node_meta, default=str))

            system_msg_id = None
            if branch.system:
                sys_dict = branch.system.to_dict(mode="db")
                system_msg_id = sys_dict["id"]
                await db.insert_message(sys_dict)

            br_model: str | None = None
            br_provider: str | None = None
            try:
                from lionagi.state import provenance as _provenance

                ep_cfg = branch.chat_model.endpoint.config
                br_provider = getattr(ep_cfg, "provider", None)
                br_model_raw = (ep_cfg.kwargs or {}).get("model")
                br_model = _provenance.resolve_model_spec(br_provider, br_model_raw)
            except Exception as _prov_exc:
                _log.debug("branch provenance lookup failed for %s: %s", branch_id, _prov_exc)

            await db.create_branch(
                {
                    "id": branch_id,
                    "created_at": branch_dict["created_at"],
                    "node_metadata": node_meta,
                    "user": branch_dict.get("user"),
                    "name": branch_dict.get("name"),
                    "session_id": session_id,
                    "progression_id": branch_prog_id,
                    "system_msg_id": system_msg_id,
                    "model": br_model,
                    "provider": br_provider,
                    "agent_name": branch_dict.get("name"),
                }
            )
            initialized["done"] = True

    _on_message = _make_message_handler(
        db,
        branch_id,
        session_id,
        branch_prog_id,
        session_prog_id,
        on_first_msg=_ensure_branch_row,
        message_retry_queues=ctx["message_retry_queues"],
    )

    from lionagi.hooks import route_message_persistence

    handler = route_message_persistence(ctx["session"], branch, _on_message)
    ctx["hooks"].append((branch, handler))


async def _flush_pending_message_events(ctx: dict) -> bool:
    """Retry queued messages before teardown reads completion evidence.

    Returns whether every queue emptied. What follows this reads the run's
    completion evidence, so a queue that gave up here means that evidence is
    being read against a transcript missing messages the run produced. A queue
    the store refuses in compatibility mode raises that refusal, once every
    queue has been flushed.
    """
    from lionagi.state.compat import StateCompatibilityRefused

    flushed = True
    refusal = None
    for retry_queue in ctx.get("message_retry_queues", []):
        try:
            if not await retry_queue.flush_final():
                flushed = False
        except StateCompatibilityRefused as exc:
            if refusal is None:
                refusal = exc
            else:
                # The first refusal is raised; each later one is logged, so none goes unreported.
                _log.error("live persist flush refused another queue: %s", exc)
    if refusal is not None:
        raise refusal
    return flushed


async def _reopen_session_for_resume(
    db,
    session_id: str,
    existing_session: dict | None,
    *,
    drains_controls: bool = False,
    generation_id: str | None = None,
) -> bool:
    """Return a resumed session to ``running`` so its next close is a real change.

    A closing transition only announces itself when the status actually
    changes. A resume adopts a session an earlier leg already took terminal,
    so writing that same terminal status at the end is a no-op: the leg
    finishes silently and the job record never closes. Reopening first
    restores the invariant the rest of the system reads off this column --
    that a session marked terminal is not currently executing.

    Reopening is the only sanctioned exit from a terminal status and carries
    an explicit override rather than a session-policy rule, so each reopening
    stays attributable and terminal exit isn't opened to every writer --
    finality is what the reapers, the teardown guard, and ``li wait`` all rest on.
    """
    from lionagi.cli.kill import current_pid_markers
    from lionagi.state.db import SESSION_TERMINAL_STATUSES
    from lionagi.state.reasons import SessionReasons

    if not existing_session or existing_session.get("status") not in SESSION_TERMINAL_STATUSES:
        # Not terminal: a resume racing a live leg on the same branch. The row
        # already describes the session correctly, so there is nothing to reopen.
        return False

    # Process-liveness markers move in the same transaction as the status
    # update below: a running row must never carry the previous (exited)
    # leg's markers, or a liveness sweep could cancel this live leg.
    # node_metadata is dropped rather than raised on if unreadable — a resume
    # must not fail on its own bookkeeping.
    node_metadata = existing_session.get("node_metadata")
    if isinstance(node_metadata, str):
        try:
            node_metadata = json.loads(node_metadata)
        except ValueError:
            node_metadata = None
    if not isinstance(node_metadata, dict):
        node_metadata = {}

    try:
        applied = await db.update_status(
            "session",
            session_id,
            new_status="running",
            reason_code=SessionReasons.REOPENED_BY_RESUME,
            reason_summary="branch resumed by a new leg",
            source="executor",
            actor=session_id,
            expected_statuses=SESSION_TERMINAL_STATUSES,
            extra_fields={
                "ended_at": None,
                **({"cli_worker_generation": generation_id} if generation_id is not None else {}),
                # The control-drain declaration rides this same write. It has to:
                # it describes the leg that is executing now, and so do the
                # process markers beside it. Written separately afterwards, it
                # would be a read-modify-write over the row as it was read
                # *before* this transition, which restores the exited leg's pid
                # and pid_create_time and hands the stale-session doctor grounds
                # to terminalize a live leg. One statement, one set of facts
                # about one leg.
                "node_metadata": json.dumps(
                    {
                        **node_metadata,
                        **current_pid_markers(),
                        "drains_controls": bool(drains_controls),
                    }
                ),
            },
            override=True,
            override_actor="cli.resume",
            override_justification="branch resumed by a new leg; the session is executing again",
        )
    except LookupError:
        # update_status reports a missing row this way. The row was read a
        # moment ago and is gone now: maintenance removes old terminal sessions
        # and holds each candidate for the length of its transaction, so a
        # resume that arrives just before one starts is let through only after
        # it commits, to find nothing there. That is a legitimate outcome
        # rather than a failure of this leg — there is no session to reopen,
        # and the caller falls back to starting one.
        _log.warning(
            "session %s no longer exists and was not reopened for resume; "
            "this leg will record itself under a new session",
            session_id,
        )
        return False

    if not applied:
        # A resumed leg must not fail because its bookkeeping lost a race, but
        # the consequence is worth saying out loud: this leg will finish without
        # announcing itself, and from the outside that is indistinguishable from
        # a leg that is still running.
        _log.warning(
            "session %s was not reopened for resume; another writer moved it "
            "first, so this leg will close without emitting a terminal notice",
            session_id,
        )
        return False

    return True


async def setup_agent_persist(
    branch: Branch,
    *,
    agent_name: str | None = None,
    artifacts_path: str | None = None,
    artifact_contract: dict | None = None,
    invocation_id: str | None = None,
    model: str | None = None,
    provider: str | None = None,
    effort: str | None = None,
    project: str | None = None,
    run_id: str | None = None,
    run_manifest: dict[str, Any] | None = None,
    run: RunDir | None = None,
    share_db: bool = True,
    drains_controls: bool = False,
    parent_marker: Callable[[], None] | None = None,
    _worker_spawn_continuation: object | None = None,
) -> dict | None:
    from lionagi.session.session import Session
    from lionagi.state import provenance as _provenance
    from lionagi.state.cli_workers import (
        _consume_worker_spawn_continuation,
        _WorkerSpawnOwner,
    )

    db = None
    session = None
    owner = None
    shared_db_registered = False
    fresh_session = False
    admitted_session_id = None
    inserted_session_id = None
    unrecorded = False
    generation_id = str(uuid4())
    try:
        # Claim the branch before touching the shared DB registry: registering a
        # shared DB closes the previous handle, which would break its owner's teardown.
        if branch._owning_session_id is not None:
            raise _WorkerSpawnAdmissionError("the branch already belongs to an execution")
        session = Session(name="agent", default_branch=branch)
        session_id = str(session.id)
        branch_id = str(branch.id)

        db = await _open_shared_db(register=False)
        if _worker_spawn_continuation is not None:
            owner = await _consume_worker_spawn_continuation(
                _worker_spawn_continuation,
                db,
                run_id=run_id if run_id is not None else active_run_id(),
                branch_id=branch_id,
            )
            generation_id = owner.generation_id

        existing_branch = await db.get_branch(branch_id)
        existing_session = None
        if existing_branch:
            existing_session = await db.get_session(existing_branch["session_id"])
            if owner is not None:
                if existing_branch["session_id"] != owner.session_id:
                    raise _WorkerSpawnAdmissionError("the continuation does not own this branch")
            elif existing_session is not None:
                if existing_session.get("status") == "running":
                    raise _WorkerSpawnAdmissionError(
                        "a running session requires an execution continuation"
                    )
                await _reopen_session_for_resume(
                    db,
                    existing_branch["session_id"],
                    existing_session,
                    drains_controls=drains_controls,
                    generation_id=generation_id,
                )
                existing_session = await db.get_session(existing_branch["session_id"])
            if existing_session is None:
                if owner is not None:
                    raise _WorkerSpawnAdmissionError("the continuation session is unavailable")
                existing_branch = None
        elif owner is not None:
            raise _WorkerSpawnAdmissionError("the continuation branch is unavailable")

        if existing_branch:
            session_id = existing_branch["session_id"]
            if (
                existing_session.get("status") != "running"
                or existing_session.get("cli_worker_generation") != generation_id
            ):
                raise _WorkerSpawnAdmissionError("the session generation was not admitted")
            admitted_session_id = session_id
            if owner is None:
                owner = _WorkerSpawnOwner(
                    db,
                    session_id=session_id,
                    generation_id=generation_id,
                    run_id=run_id if run_id is not None else active_run_id(),
                    branch_id=branch_id,
                )
            session_prog_id = existing_session["progression_id"]
            branch_prog_id = existing_branch["progression_id"]

            if session_prog_id is None:
                candidate = str(uuid4())
                await db.create_progression(candidate)
                effective = await db.repair_session_progression(session_id, candidate)
                session_prog_id = effective or candidate
            if branch_prog_id is None:
                candidate = str(uuid4())
                await db.create_progression(candidate)
                effective = await db.repair_branch_progression(branch_id, candidate)
                branch_prog_id = effective or candidate

            existing_msg_ids = set(await db.get_progression(branch_prog_id))

            if invocation_id and existing_session.get("invocation_id") != invocation_id:
                # A resume reopens this row rather than inserting a new one,
                # so the ON CONFLICT DO NOTHING branch below never runs for
                # it and this leg's invocation_id would otherwise be
                # dropped. Backfill the same linkage a brand-new session
                # gets at insert time.
                await db.attach_session_invocation(session_id, invocation_id)

        else:
            fresh_session = True
            if await db.get_session(session_id) is not None:
                raise _WorkerSpawnAdmissionError("the fresh session key is already in use")
            session_prog_id = str(uuid4())
            branch_prog_id = str(uuid4())
            existing_msg_ids = set()

            await db.create_progression(session_prog_id)
            await db.create_progression(branch_prog_id)

            session_dict = session.to_dict(mode="db")
            _proj, _proj_src = _resolve_project(project)
            from lionagi.cli.kill import current_pid_markers

            # Whether this session's runner consumes operator controls, declared
            # by the caller that starts the run rather than inferred. The
            # control writer refuses a session whose runner has no drain, and it
            # has no other way to tell one agent-kind session from another:
            # every runner that persists through here writes the same kind and
            # a run_id, so run_id presence stopped distinguishing them the
            # moment a second caller began supplying one. Default False so a
            # new runner that forgets to declare it gets a visible refusal
            # rather than a control nobody will ever read.
            _node_meta = {
                **(session_dict.get("node_metadata") or {}),
                **current_pid_markers(),
                "drains_controls": bool(drains_controls),
            }
            # Another lineage lays out a store in compatibility mode, and its schema carries no
            # worker generation and no spawn records, so the run starts with no worker owner and
            # its workers stay outside the terminal kill arm, as an unpersisted run's do.
            from lionagi.state.compat import is_compatibility_store

            unrecorded = is_compatibility_store(db)
            inserted_session_id = session_id
            await db.create_session(
                {
                    "id": session_id,
                    "cli_worker_generation": None if unrecorded else generation_id,
                    # Embedded callers may own a RunDir without mutating the
                    # CLI process-global allocation pointer. Ordinary CLI
                    # callers omit this and retain the established behavior.
                    "run_id": run_id if run_id is not None else active_run_id(),
                    "created_at": session_dict["created_at"],
                    "node_metadata": _node_meta,
                    "name": session_dict.get("name"),
                    "user": session_dict.get("user"),
                    "progression_id": session_prog_id,
                    "first_msg_id": None,
                    "last_msg_id": None,
                    "invocation_kind": "agent",
                    "agent_name": agent_name,
                    "artifacts_path": artifacts_path,
                    "artifact_contract_json": artifact_contract,
                    "status": "running",
                    "started_at": time.time(),
                    "invocation_id": invocation_id,
                    "model": model,
                    "provider": provider,
                    "effort": effort,
                    "agent_hash": _provenance.agent_definition_hash(agent_name),
                    "project": _proj,
                    "project_source": _proj_src,
                }
            )

            admitted = await db.get_session(session_id)
            if (
                admitted is None
                or admitted.get("status") != "running"
                or (not unrecorded and admitted.get("cli_worker_generation") != generation_id)
            ):
                raise _WorkerSpawnAdmissionError("the session generation was not admitted")
            admitted_session_id = session_id
            if not unrecorded:
                owner = _WorkerSpawnOwner(
                    db,
                    session_id=session_id,
                    generation_id=generation_id,
                    run_id=run_id if run_id is not None else active_run_id(),
                    branch_id=branch_id,
                )
            system_msg_id = None
            if branch.system:
                sys_dict = branch.system.to_dict(mode="db")
                system_msg_id = sys_dict["id"]
                await db.insert_message(sys_dict)

            branch_dict = branch.to_dict(mode="db")
            node_meta = branch_dict.get("node_metadata") or {}
            if isinstance(node_meta, str):
                node_meta = json.loads(node_meta)
            if "chat_model" in branch_dict:
                node_meta["chat_model"] = branch_dict["chat_model"]

            await db.create_branch(
                {
                    "id": branch_id,
                    "created_at": branch_dict["created_at"],
                    "node_metadata": node_meta,
                    "user": branch_dict.get("user"),
                    "name": branch_dict.get("name"),
                    "session_id": session_id,
                    "progression_id": branch_prog_id,
                    "system_msg_id": system_msg_id,
                    "model": model,
                    "provider": provider,
                    "agent_name": agent_name,
                }
            )

        if share_db:
            from lionagi.state.db import register_shared_db

            await register_shared_db(db)
            shared_db_registered = True
        if fresh_session:
            with owner.bind() if owner is not None else contextlib.nullcontext():
                from lionagi.hooks.bus import HookPoint

                # The run's own handle goes with both emits, as with the teardown's. The session
                # and branch rows above already carry every field the default handlers write, so
                # handed a handle they write nothing: no process-wide store is reached, and no
                # repeated write needs a grant this setup does not.
                await session.hooks.emit(
                    HookPoint.SESSION_START,
                    session_id=session_id,
                    model=model,
                    provider=provider,
                    effort=effort,
                    agent_name=agent_name,
                    agent_hash=_provenance.agent_definition_hash(agent_name),
                    invocation_id=invocation_id,
                    db=db,
                )
                await session.hooks.emit(
                    HookPoint.BRANCH_CREATE,
                    branch_id=branch_id,
                    model=model,
                    provider=provider,
                    agent_name=agent_name,
                    db=db,
                )

        new_msg_ids: list = []
        message_retry_queues: list = []
        ctx = {
            "db": db,
            "session": session,
            "branch": branch,
            "session_id": session_id,
            "session_prog_id": session_prog_id,
            "branch_prog_id": branch_prog_id,
            "existing_msg_ids": existing_msg_ids,
            "new_msg_ids": new_msg_ids,
            "message_retry_queues": message_retry_queues,
            "artifacts_path": artifacts_path,
            "artifact_contract": artifact_contract,
            "shared_db_registered": shared_db_registered,
            "worker_spawn_owner": owner,
            # Set only where the store cannot hold worker records, so a setup result without an
            # owner is refused unless it says why it has none.
            "worker_spawns_unrecorded": owner is None,
            "worker_spawn_db_closed": None,
            "worker_spawn_teardown_status": None,
        }

        _on_message = _make_message_handler(
            db,
            branch_id,
            session_id,
            branch_prog_id,
            session_prog_id,
            dedup_set=existing_msg_ids,
            new_msg_ids_list=new_msg_ids,
            message_retry_queues=message_retry_queues,
        )

        # Bind through the already-open DB so signals land in session_signals
        # without opening a new connection per signal. A run that owns a manifest and a run
        # directory records on it when its signal persistence first stops waiting on the store,
        # and a leg resumed for another run (*parent_marker*) records on that run's too: its
        # run.json is the one the job reads.
        markers = [
            mark
            for mark in (
                _signal_window_marker(run_manifest, run.state_root)
                if run_manifest is not None and run is not None
                else None,
                parent_marker,
            )
            if mark is not None
        ]

        def _mark_every_run() -> None:
            for mark in markers:
                mark()

        session.observer.bind_db_persistence(
            session_id,
            db=db,
            on_window_opened=_mark_every_run if markers else None,
        )

        from lionagi.hooks import route_message_persistence

        ctx["hook"] = route_message_persistence(session, branch, _on_message)
        return ctx
    except BaseException as exc:
        from lionagi.state.compat import refusal_in
        from lionagi.state.lifecycle import LifecycleNotFoundError
        from lionagi.state.reasons import RunReasons

        # A store that refuses a write in compatibility mode stops the run instead of
        # letting it go on without persistence (ADR-0128 D6); it is raised as itself, after
        # cleanup, so a caller's check finds it even when a task group wrapped it.
        refusal = exc if isinstance(exc, _WorkerSpawnAdmissionError) else refusal_in(exc)
        drain_cancel = None
        cleanup_cancel = None
        if owner is not None:
            while True:
                try:
                    await owner.aclose()
                    break
                except cancelled_exc_classes() as close_cancel:
                    if drain_cancel is None:
                        drain_cancel = close_cancel
        if refusal is None and isinstance(exc, Exception):
            _log.warning(
                "live persist setup failed (%r) — disabling persistence for this run",
                exc,
                exc_info=True,
            )
            _record_persistence_degraded(exc, run_id=run_id, run_manifest=run_manifest)
        if (admitted_session_id is not None or inserted_session_id is not None) and db is not None:
            # The row this setup admitted reads as a live execution while it stays running,
            # and a later setup refuses a running session that comes without a continuation,
            # so the branch could never be resumed. Close the row out before the handle goes.
            cancelled = drain_cancel is not None or isinstance(exc, cancelled_exc_classes())
            try:
                with (
                    CancelScope(shield=True),
                    move_on_after(_SETUP_RECOVERY_TIMEOUT_SECONDS) as scope,
                ):
                    try:
                        recovery_id = admitted_session_id
                        expected_updated_at = None
                        if recovery_id is None and not unrecorded:
                            candidate = await db.get_session(inserted_session_id)
                            if candidate is not None:
                                if (
                                    candidate.get("status") == "running"
                                    and candidate.get("cli_worker_generation") == generation_id
                                    and candidate.get("updated_at") is not None
                                ):
                                    recovery_id = inserted_session_id
                                    expected_updated_at = candidate["updated_at"]
                                else:
                                    _log.warning(
                                        "session setup recovery did not complete: ownership unproven"
                                    )
                        elif recovery_id is None:
                            _log.warning(
                                "session setup recovery did not complete: generation unrecorded"
                            )
                        if recovery_id is not None:
                            # The timestamp guards intervening writes, but is not an atomic generation
                            # predicate: equal timestamps or replacement under the key can defeat it.
                            try:
                                applied = await db.update_status(
                                    "session",
                                    recovery_id,
                                    new_status="cancelled" if cancelled else "failed",
                                    reason_code=(
                                        RunReasons.CANCELLED_SYSTEM
                                        if cancelled
                                        else RunReasons.FAILED_EXCEPTION
                                    ),
                                    reason_summary=(
                                        "live persist setup failed after the session row was admitted"
                                        if admitted_session_id is not None
                                        else "live persist setup failed before the insert outcome was observed"
                                    ),
                                    source="executor",
                                    actor=recovery_id,
                                    expected_statuses={"running"},
                                    expected_updated_at=expected_updated_at,
                                    extra_fields=_terminal_status_extra_fields(
                                        db, ended_at=time.time(), duration_ms=None
                                    ),
                                )
                            except LookupError as missing:
                                # The row disappeared before the guarded close.
                                if type(missing) is not LookupError or not isinstance(
                                    missing.__cause__, LifecycleNotFoundError
                                ):
                                    raise
                                applied = None
                            if applied is False:
                                _log.warning("session setup recovery did not complete: row changed")
                    except Exception as status_exc:  # noqa: BLE001 -- the setup failure stays primary
                        _log.warning("session setup recovery did not complete: %s", status_exc)
                if scope.cancel_called:
                    _log.warning("session setup recovery did not complete: deadline expired")
            except cancelled_exc_classes() as caught_cancel:
                # Direct task cancellation can interrupt a shield. Preserve the first
                # cancellation and report the unfinished recovery, without orphaning work.
                if cleanup_cancel is None:
                    cleanup_cancel = caught_cancel
                _log.warning("session setup recovery did not complete: interrupted")
        # If the wrapper session already claimed the branch, release it so a
        # later setup (or retry) can wrap the same branch again.
        if session is not None:
            try:
                session.remove_branch(branch)
            except Exception as release_exc:  # noqa: BLE001
                _log.debug("branch ownership release failed: %s", release_exc)
        if db is not None:
            close_deadline = time.monotonic() + _SETUP_RECOVERY_TIMEOUT_SECONDS
            while True:
                try:
                    with (
                        CancelScope(shield=True),
                        move_on_after(max(0, close_deadline - time.monotonic())) as close_scope,
                    ):
                        await db.close()
                    if close_scope.cancel_called:
                        _log.warning("fallback db.close after setup failure did not complete")
                    break
                except cancelled_exc_classes() as caught_cancel:
                    if cleanup_cancel is None:
                        cleanup_cancel = caught_cancel
                    if time.monotonic() >= close_deadline:
                        _log.warning("fallback db.close after setup failure did not complete")
                        break
                except Exception as close_exc:
                    _log.warning("fallback db.close after setup failure also failed: %s", close_exc)
                    break
            if shared_db_registered:
                # Drop the now-closed handle so get_shared_db() can't hand it out.
                from lionagi.state.db import unregister_shared_db

                unregister_shared_db(db)
        # Raise store refusals as themselves first so the caller can identify them (ADR-0128 D6).
        # Then preserve drain cancellation, the original non-Exception failure,
        # and finally any cancellation caught during cleanup, in that order.
        if refusal is not None:
            raise refusal  # noqa: B904 -- raised as found, keeping its own cause
        if drain_cancel is not None:
            raise drain_cancel  # noqa: B904 -- preserve the drain cancellation's own cause
        if not isinstance(exc, Exception):
            raise
        if cleanup_cancel is not None:
            raise cleanup_cancel  # noqa: B904 -- preserve the cleanup cancellation's own cause
        return None


async def find_incomplete_session_for_run(run_id: str) -> dict | None:
    """Recover a session row that ``setup_agent_persist()`` committed before
    failing on a later step, terminalizing it if it is still "running".

    ``setup_agent_persist()`` can call ``create_session()`` and then raise
    inside the same setup (e.g. the following ``create_branch()``); it catches
    that exception and returns None, so its caller sees no context but the
    session row itself is still durable and left running forever. A caller
    whose *run_id* is minted fresh for every attempt (never reused across a
    resume, the way a new operator turn always is) can pass it here after
    setup fails to recover that row -- and close it out -- instead of
    reporting the run as never having existed. Returns None only when no
    session was ever recorded under this run id.
    """
    from lionagi.state.db import SESSION_TERMINAL_STATUSES, StateDB
    from lionagi.state.reasons import RunReasons

    db = StateDB()
    try:
        await db.open()
        rows = await db.get_sessions_for_run(run_id)
        if not rows:
            return None
        row = rows[-1]
        status = row.get("status")
        if status not in SESSION_TERMINAL_STATUSES:
            await db.update_status(
                "session",
                row["id"],
                new_status="failed",
                reason_code=RunReasons.FAILED_EXCEPTION,
                reason_summary="run setup failed after the session row was committed",
                source="executor",
                actor=row["id"],
                expected_statuses={status},
                extra_fields=(
                    {"current_phase": None} if _run_may_clear_current_phase(db) else None
                ),
            )
            row = await db.get_session(row["id"]) or row
        return row
    finally:
        # Plain open may have created an engine even when it failed or was cancelled.
        # Bound disposal separately so a cancelled lookup still attempts to release it.
        original_error = sys.exc_info()[1]
        try:
            with CancelScope(shield=True), move_on_after(_SETUP_RECOVERY_TIMEOUT_SECONDS) as scope:
                await db.close()
            if scope.cancel_called:
                raise TimeoutError("session recovery database close did not complete")
        except BaseException as close_error:
            if original_error is None:
                raise
            _log.warning("session recovery database close did not complete: %s", close_error)
