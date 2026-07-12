# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Checkpoint persistence and resolution for cross-process flow resume."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lionagi._errors import LionError
from lionagi._paths import RUNS_ROOT

from .._runs import RunDir

__all__ = (
    "CHECKPOINT_VERSION",
    "CheckpointWriter",
    "FlowResumeError",
    "load_checkpoint",
    "resolve_checkpoint_target",
)

CHECKPOINT_VERSION = 2


class FlowResumeError(LionError):
    """Raised when a checkpoint cannot be resolved, loaded, or safely resumed."""


@dataclass
class CheckpointWriter:
    """Serializes checkpoint writes so concurrent op completions never tear the file on disk.

    Every write serializes the FULL current state to a unique temp name and
    renames it into place, so a reader only ever sees a complete file; a lock
    held across serialize-write-rename keeps renames in acquisition order.
    """

    path: Path
    session_id: str
    prompt: str
    plan: list[dict]
    config: dict[str, Any]
    flow_context: dict[str, Any] = field(default_factory=dict)
    ops: dict[str, dict[str, Any]] = field(default_factory=dict)
    spawned: list[dict] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    _seq: int = field(default=0, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": CHECKPOINT_VERSION,
            "session_id": self.session_id,
            "prompt": self.prompt,
            "plan": self.plan,
            "flow_context": self.flow_context,
            "ops": self.ops,
            "spawned": self.spawned,
            "config": self.config,
        }

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
        async with self._lock:
            self.ops[agent_id] = {"agent_id": agent_id, "status": status, "response": response}
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
    ) -> None:
        """Record one reactively spawned node's outcome, keyed by its own node id.

        Spawned nodes must never share the `ops` keyspace: a spawned child's
        branch can carry a name identical to a planned agent_id's, so using
        that name as the key would silently overwrite the planned entry.

        operation/assignee/instruction/parent_id (added in CHECKPOINT_VERSION 2)
        are what resume needs to reconstruct the spawned node into a fresh
        graph — the operation type, its routed role (if any), the instruction
        it ran with, and the node id of whichever op's completion produced the
        SpawnRequest (None for an independent spawn or one with no emitter). A
        checkpoint written before this field set existed carries entries
        without `operation`; resume treats those as unreconstructable and
        refuses only for the affected node(s), not the whole run.
        """
        async with self._lock:
            entry = {
                "node_id": node_id,
                "status": status,
                "response": response,
                "operation": operation,
                "assignee": assignee,
                "instruction": instruction,
                "parent_id": parent_id,
            }
            for i, existing in enumerate(self.spawned):
                if existing.get("node_id") == node_id:
                    self.spawned[i] = entry
                    break
            else:
                self.spawned.append(entry)
            if flow_context is not None:
                self.flow_context = flow_context
            await self._write_locked()

    async def flush(self) -> None:
        """Persist the current state without changing any op entry."""
        async with self._lock:
            await self._write_locked()

    async def _write_locked(self) -> None:
        self._seq += 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"checkpoint.{self._seq}.tmp")
        payload = json.dumps(self.to_dict(), default=str)
        tmp.write_text(payload)
        os.replace(tmp, self.path)


def load_checkpoint(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _find_run_dir_by_id(run_id: str) -> RunDir | None:
    exact = RUNS_ROOT / run_id
    if exact.is_dir():
        return RunDir(run_id=run_id, state_root=exact, artifact_root=exact / "artifacts")
    if RUNS_ROOT.exists():
        for match in sorted(
            RUNS_ROOT.glob(f"{run_id}*"), key=lambda p: p.stat().st_mtime, reverse=True
        ):
            if match.is_dir():
                return RunDir(
                    run_id=match.name, state_root=match, artifact_root=match / "artifacts"
                )
    return None


async def resolve_checkpoint_target(target: str) -> tuple[RunDir, dict[str, Any]]:
    """Resolve a run_id, or a session/invocation/play id, to (RunDir, checkpoint dict).

    A run_id matches a directory under RUNS_ROOT directly, no DB lookup
    needed. Anything else is resolved as a session/invocation/play id (same
    resolution `li o ctl status` uses) to its backing session, whose
    node_metadata carries the run_id every flow run stamps at startup.
    """
    run_dir = _find_run_dir_by_id(target)
    if run_dir is not None and run_dir.checkpoint_path.exists():
        return run_dir, load_checkpoint(run_dir.checkpoint_path)

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
    if run_dir is None or not run_dir.checkpoint_path.exists():
        raise FlowResumeError(
            f"No checkpoint.json found for run {run_id!r} (resolved from {target!r})."
        )
    return run_dir, load_checkpoint(run_dir.checkpoint_path)
