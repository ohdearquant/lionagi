# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Checkpoint persistence and resolution for cross-process flow resume."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lionagi._errors import LionError
from lionagi._paths import RUNS_ROOT
from lionagi.ln._json_dump import raise_if_non_finite

from .._runs import PERSISTENCE_DEGRADED_REASON_FIELD, RunDir
from .._util import AmbiguousIdError

__all__ = (
    "CHECKPOINT_VERSION",
    "CheckpointWriter",
    "FlowResumeError",
    "load_checkpoint",
    "resolve_checkpoint_target",
)

LEGACY_CHECKPOINT_VERSION = 2
CHECKPOINT_VERSION = 3
_DEFAULT_COMPACT_EVERY = 128


class FlowResumeError(LionError):
    """Raised when a checkpoint cannot be resolved, loaded, or safely resumed."""


@dataclass
class CheckpointWriter:
    """Serialize checkpoint updates in completion order without blocking the event loop."""

    path: Path
    session_id: str
    prompt: str
    plan: list[dict]
    config: dict[str, Any]
    version: int = LEGACY_CHECKPOINT_VERSION
    compact_every: int = _DEFAULT_COMPACT_EVERY
    max_spawn: int | None = None
    flow_context: dict[str, Any] = field(default_factory=dict)
    ops: dict[str, dict[str, Any]] = field(default_factory=dict)
    spawned: list[dict] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    _seq: int = field(default=0, repr=False, compare=False)
    _generation: str = field(default_factory=lambda: uuid.uuid4().hex, repr=False, compare=False)
    _journal_seq: int = field(default=0, repr=False, compare=False)
    _delta_count: int = field(default=0, repr=False, compare=False)
    _bytes_written: int = field(default=0, repr=False, compare=False)
    _base_ready: bool = field(default=False, repr=False, compare=False)
    _journal_reset_required: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.version not in (LEGACY_CHECKPOINT_VERSION, CHECKPOINT_VERSION):
            raise ValueError(f"unsupported checkpoint version: {self.version}")
        if self.compact_every < 1:
            raise ValueError("compact_every must be at least 1")

    @property
    def journal_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.journal")

    @property
    def bytes_written(self) -> int:
        return self._bytes_written

    def journal_records(self) -> list[dict[str, Any]]:
        """Read complete records for the active generation, primarily for diagnostics."""
        if not self.journal_path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in self.journal_path.read_bytes().splitlines():
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                break
            if isinstance(record, dict) and record.get("generation") == self._generation:
                records.append(record)
        return records

    def to_dict(self) -> dict[str, Any]:
        state: dict[str, Any] = {
            "version": self.version,
            "session_id": self.session_id,
            "prompt": self.prompt,
            "plan": self.plan,
            "flow_context": self.flow_context,
            "ops": self.ops,
            "spawned": self.spawned,
            "config": self.config,
        }
        if self.max_spawn is not None:
            state["max_spawn"] = self.max_spawn
        if self.version == CHECKPOINT_VERSION:
            state["generation"] = self._generation
            state["journal_seq"] = 0
        return state

    async def record(
        self,
        agent_id: str,
        *,
        status: str,
        response: Any,
        flow_context: dict[str, Any] | None = None,
    ) -> None:
        """Record one planned op's outcome and persist the whole checkpoint atomically.

        flow_context, when given, replaces the writer's snapshot of the
        shared context workspace — latest wins, since it accumulates rather
        than being per-op data.
        """
        entry = {"agent_id": agent_id, "status": status, "response": response}
        async with self._lock:
            if self.version == CHECKPOINT_VERSION:
                captured = _capture_context(flow_context)
                await self._ensure_base_locked()
                context_delta = await _context_delta_async(self.flow_context, captured)
                delta = {"kind": "op", "entry": entry}
                if context_delta is not None:
                    delta["flow_context"] = context_delta
                await self._append_delta_locked(delta)
                self.ops[agent_id] = entry
                if captured is not None:
                    self.flow_context = captured
                await self._maybe_compact_locked()
            else:
                self.ops[agent_id] = entry
                if flow_context is not None:
                    self.flow_context = flow_context
                await self._write_locked()

    async def record_spawned(
        self,
        node_id: str,
        *,
        status: str,
        response: Any,
        flow_context: dict[str, Any] | None = None,
        operation: str | None = None,
        assignee: str | None = None,
        instruction: str | None = None,
        parent_id: str | None = None,
        spawn_id: str | None = None,
        context: Any | None = None,
    ) -> None:
        """Record one reactively spawned node's outcome, keyed by its own node id.

        Kept out of the `ops` keyspace so a spawned child's branch name can't
        collide with and silently overwrite a planned `agent_id` entry.
        `operation`/`assignee`/`instruction`/`parent_id`/`spawn_id`
        (CHECKPOINT_VERSION 2) are what resume needs to rebuild the node into
        a fresh graph; `spawn_id` must accompany `assignee` (the finalize-time
        scan raises if one appears without the other) — a checkpoint
        predating this field set has entries with no `operation`, and resume
        refuses only those nodes, not the whole run. `context` is the node's
        `parameters["context"]` (e.g. a team round's `prior_team_messages`),
        distinct from `instruction` (generic boilerplate for a team round);
        `None` when there's no context payload.
        """
        entry = {
            "node_id": node_id,
            "status": status,
            "response": response,
            "operation": operation,
            "assignee": assignee,
            "instruction": instruction,
            "parent_id": parent_id,
            "spawn_id": spawn_id,
            "context": context,
        }
        async with self._lock:
            captured = (
                _capture_context(flow_context) if self.version == CHECKPOINT_VERSION else None
            )
            if self.version == CHECKPOINT_VERSION:
                await self._ensure_base_locked()
                context_delta = await _context_delta_async(self.flow_context, captured)
                delta = {"kind": "spawned", "entry": entry}
                if context_delta is not None:
                    delta["flow_context"] = context_delta
                await self._append_delta_locked(delta)
            for i, existing in enumerate(self.spawned):
                if existing.get("node_id") == node_id:
                    self.spawned[i] = entry
                    break
            else:
                self.spawned.append(entry)
            if self.version == CHECKPOINT_VERSION:
                if captured is not None:
                    self.flow_context = captured
            elif flow_context is not None:
                self.flow_context = flow_context
            if self.version == CHECKPOINT_VERSION:
                await self._maybe_compact_locked()
            else:
                await self._write_locked()

    async def flush(self) -> None:
        """Persist the current state without changing any op entry."""
        async with self._lock:
            if self.version == CHECKPOINT_VERSION:
                await self._write_v3_base_locked()
            else:
                await self._write_locked()

    async def _write_locked(self) -> None:
        self._seq += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"checkpoint.{self._seq}.tmp")
        state = self.to_dict()
        # A non-finite float would be written as the token NaN/Infinity, which
        # Python reads back and strict readers reject; refuse it at the write.
        raise_if_non_finite(state, default=str)
        payload = json.dumps(state, default=str)
        tmp.write_text(payload)
        os.replace(tmp, self.path)

    async def _ensure_base_locked(self) -> None:
        if not self._base_ready:
            await self._write_v3_base_locked()
        elif self._journal_reset_required:
            written = await _durable_to_thread(_atomic_write_bytes, self.journal_path, b"")
            self._bytes_written += written
            self._journal_reset_required = False

    async def _append_delta_locked(self, delta: dict[str, Any]) -> None:
        next_seq = self._journal_seq + 1
        record = {
            "version": CHECKPOINT_VERSION,
            "generation": self._generation,
            "seq": next_seq,
            **delta,
        }
        written = await _durable_to_thread(_append_journal_record, self.journal_path, record)
        self._bytes_written += written
        self._journal_seq = next_seq
        self._delta_count += 1
        self._seq += 1

    async def _maybe_compact_locked(self) -> None:
        if self._delta_count >= self.compact_every:
            await self._write_v3_base_locked()

    async def _write_v3_base_locked(self) -> None:
        previous_generation = self._generation
        self._generation = uuid.uuid4().hex
        state = self.to_dict()
        try:
            written = await _durable_to_thread(_atomic_write_json, self.path, state)
        except BaseException:
            self._generation = previous_generation
            raise

        self._bytes_written += written
        self._journal_seq = 0
        self._delta_count = 0
        self._base_ready = True
        self._journal_reset_required = True
        written = await _durable_to_thread(_atomic_write_bytes, self.journal_path, b"")
        self._bytes_written += written
        self._journal_reset_required = False


def _serialize_json(value: Any) -> bytes:
    raise_if_non_finite(value, default=str)
    return json.dumps(value, default=str, separators=(",", ":")).encode()


async def _durable_to_thread(function: Any, *args: Any) -> Any:
    """Finish a started durable write even if its bookkeeping task is cancelled."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # asyncio cannot stop the worker thread. Returning early would let
            # the in-memory sequence lag a record that can still reach disk,
            # so wait through cancellation and advance both states together.
            if task.done():
                return task.result()


def _fsync_parent(path: Path) -> None:
    try:
        descriptor = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, payload: bytes) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _fsync_parent(path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return len(payload)


def _atomic_write_json(path: Path, value: Any) -> int:
    return _atomic_write_bytes(path, _serialize_json(value))


def _append_journal_record(path: Path, record: dict[str, Any]) -> int:
    payload = _serialize_json(record) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return len(payload)


def _context_snapshot(current: dict[str, Any]) -> dict[str, Any]:
    """Baseline for the next delta, insulated from later in-place mutation.

    The caller holds a live reference to the shared context workspace and keeps
    mutating it between completions. A shallow copy shares every nested value
    with that workspace, so a nested mutation compares equal against the
    baseline, journals no delta, and a crash before compaction resumes with
    context the run had already moved past.
    """
    try:
        return copy.deepcopy(current)
    except Exception:
        # Something here refuses to be deep-copied. The journal already
        # requires this to serialize, so fall back to the serialized form
        # rather than to a shallow copy that reintroduces the aliasing.
        return json.loads(_serialize_json(current))


def _capture_context(current: dict[str, Any] | None) -> dict[str, Any] | None:
    """Freeze the caller's live workspace before anything can await on it.

    This has to run synchronously, and that is the whole point of it being a
    separate call. The caller keeps a reference to the shared context and goes
    on mutating it between completions, so every ``await`` between reading the
    context and storing it is a window in which it can change underneath us.

    Deriving the journal delta from the live dict and then snapshotting the
    live dict again afterwards puts those two reads on opposite sides of that
    window. A mutation landing inside it enters the new baseline without ever
    entering a delta, and because the baseline now contains it, the next
    comparison sees it as unchanged and never journals it either: recovery
    restores context the run had already moved past, permanently. Capturing
    once up front and deriving both the delta and the next baseline from that
    one value is what makes them describe the same state by construction.

    Moving this off the event loop with ``to_thread`` would reintroduce
    exactly the window it exists to close.
    """
    if current is None:
        return None
    return _context_snapshot(current)


def _context_delta(
    previous: dict[str, Any], current: dict[str, Any] | None
) -> dict[str, Any] | None:
    if current is None:
        return None
    changed = {
        key: value
        for key, value in current.items()
        if key not in previous or previous[key] != value
    }
    removed = [key for key in previous if key not in current]
    if not changed and not removed:
        return None
    return {"set": changed, "delete": removed}


async def _context_delta_async(
    previous: dict[str, Any], current: dict[str, Any] | None
) -> dict[str, Any] | None:
    return await asyncio.to_thread(_context_delta, previous, current)


def load_checkpoint(path: Path) -> dict[str, Any]:
    state = json.loads(path.read_text())
    if state.get("version") != CHECKPOINT_VERSION:
        return state

    journal_path = path.with_name(f"{path.name}.journal")
    if not journal_path.exists():
        return state

    recovery: dict[str, Any] = {}
    expected_seq = int(state.get("journal_seq") or 0) + 1
    stale_records = 0
    raw = journal_path.read_bytes()
    for line_number, line in enumerate(raw.splitlines(keepends=True), start=1):
        if not line.endswith(b"\n"):
            recovery.update({"torn_final_record": True, "line": line_number})
            break
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            recovery.update(
                {
                    "invalid_record": True,
                    "line": line_number,
                    "error": type(exc).__name__,
                }
            )
            break
        if not isinstance(record, dict):
            recovery.update({"invalid_record": True, "line": line_number})
            break
        if record.get("generation") != state.get("generation"):
            stale_records += 1
            continue
        if record.get("seq") != expected_seq:
            recovery.update(
                {
                    "invalid_sequence": True,
                    "line": line_number,
                    "expected": expected_seq,
                    "actual": record.get("seq"),
                }
            )
            break
        if not _apply_journal_record(state, record):
            recovery.update({"invalid_record": True, "line": line_number})
            break
        state["journal_seq"] = expected_seq
        expected_seq += 1

    if stale_records:
        recovery["stale_generation_records"] = stale_records
    if recovery:
        state["_recovery"] = recovery
    return state


def _apply_journal_record(state: dict[str, Any], record: dict[str, Any]) -> bool:
    entry = record.get("entry")
    if not isinstance(entry, dict):
        return False
    kind = record.get("kind")
    if kind == "op":
        entry_id = entry.get("agent_id")
        if not isinstance(entry_id, str):
            return False
    elif kind == "spawned":
        entry_id = entry.get("node_id")
        if not isinstance(entry_id, str):
            return False
    else:
        return False

    context_delta = record.get("flow_context")
    changed: dict[str, Any] = {}
    removed: list[Any] = []
    if context_delta is not None:
        if not isinstance(context_delta, dict):
            return False
        changed = context_delta.get("set", {})
        removed = context_delta.get("delete", [])
        if not isinstance(changed, dict) or not isinstance(removed, list):
            return False
        flow_context = state.setdefault("flow_context", {})
        if not isinstance(flow_context, dict):
            return False
        if any(not isinstance(key, str) for key in removed):
            return False

    if kind == "op":
        state.setdefault("ops", {})[entry_id] = entry
    else:
        spawned = state.setdefault("spawned", [])
        for index, existing in enumerate(spawned):
            if isinstance(existing, dict) and existing.get("node_id") == entry_id:
                spawned[index] = entry
                break
        else:
            spawned.append(entry)

    if context_delta is not None:
        flow_context.update(changed)
        for key in removed:
            flow_context.pop(key, None)
    return True


def _find_run_dir_by_id(run_id: str) -> RunDir | None:
    """Resolve a run id, or an unambiguous prefix of one, to its directory.

    Taking the most recent of several prefix matches would answer a question
    the caller did not ask: they named a run, not "the newest run starting
    with this", and the commands built on this act on what comes back.
    """
    exact = RUNS_ROOT / run_id
    if exact.is_dir():
        return RunDir(run_id=run_id, state_root=exact, artifact_root=exact / "artifacts")
    if not RUNS_ROOT.exists():
        return None

    # startswith re-checks the glob: a case-insensitive filesystem matches
    # names the id does not actually prefix.
    matches = sorted(
        p for p in RUNS_ROOT.glob(f"{run_id}*") if p.is_dir() and p.name.startswith(run_id)
    )
    if len(matches) > 1:
        raise AmbiguousIdError(run_id, "run", [p.name for p in matches])
    if not matches:
        return None
    match = matches[0]
    return RunDir(run_id=match.name, state_root=match, artifact_root=match / "artifacts")


def _raise_if_persistence_degraded(run_dir: RunDir) -> None:
    """Explain a missing checkpoint using state that survived the failed database."""
    try:
        reason = run_dir.read_manifest().get(PERSISTENCE_DEGRADED_REASON_FIELD)
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        return
    if isinstance(reason, str) and reason:
        raise FlowResumeError(
            f"Cannot resume run {run_dir.run_id!r}: persistence was disabled "
            f"for that run ({reason}); no checkpoint is available."
        )


async def resolve_checkpoint_target(target: str) -> tuple[RunDir, dict[str, Any]]:
    """Resolve a run_id, or a session/invocation/play id, to (RunDir, checkpoint dict).

    A run_id matches a directory under RUNS_ROOT directly, no DB lookup
    needed. Anything else is resolved as a session/invocation/play id (same
    resolution `li o ctl status` uses) to its backing session, whose
    node_metadata carries the run_id every flow run stamps at startup.
    """
    run_dir = _find_run_dir_by_id(target)
    if run_dir is not None:
        if run_dir.checkpoint_path.exists():
            return run_dir, load_checkpoint(run_dir.checkpoint_path)
        _raise_if_persistence_degraded(run_dir)

    from lionagi.cli.status import _resolve_any_target, _resolve_primary_session
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        resolved = await _resolve_any_target(db, target)
        if resolved is None:
            raise FlowResumeError(f"No run, session, invocation, or play found for {target!r}.")
        entity_type, row = resolved
        session_row = await _resolve_primary_session(db, entity_type, row)
        if session_row is None:
            raise FlowResumeError(f"No backing session found for {target!r}.")
        node_meta = session_row.get("node_metadata") or {}
        run_id = node_meta.get("run_id")

    if not run_id:
        raise FlowResumeError(
            f"Session {session_row['id']} has no run_id on record "
            "(it predates checkpoint support, or never reached _build_dag)."
        )

    run_dir = _find_run_dir_by_id(run_id)
    if run_dir is None:
        raise FlowResumeError(
            f"No checkpoint.json found for run {run_id!r} (resolved from {target!r})."
        )
    if not run_dir.checkpoint_path.exists():
        _raise_if_persistence_degraded(run_dir)
        raise FlowResumeError(
            f"No checkpoint.json found for run {run_id!r} (resolved from {target!r})."
        )
    return run_dir, load_checkpoint(run_dir.checkpoint_path)
