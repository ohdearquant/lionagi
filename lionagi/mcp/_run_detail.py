# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Execution-graph, artifact-contract, and stall detail for ``job.status(detail=True)``.

Reuses the Studio session service (``lionagi.studio.services.sessions.get_session``)
for the planned graph, per-branch status, and artifact-verification state rather
than re-deriving any of that here. This module only reshapes what that service
already computed into the per-node/per-artifact/per-stall triples a machine
caller reads, and resolves the CLI run id to the StateDB session backing it
(the two live in separate id spaces — see ``StateDB.get_sessions_for_run``).

Every failure mode here is soft: a run with no session, a session with no
graph, or a missing ``studio`` extra all report through ``detail_unavailable``
rather than raising, so a caller asking for the richer view never turns a
plain ``job.status`` into an error.
"""

from __future__ import annotations

import json
import time
from typing import Any

__all__ = ("build_run_detail",)


async def build_run_detail(run_id: str) -> dict[str, Any]:
    """``{"nodes": ..., "artifact_contract": ..., "stalls": ...}`` for *run_id*,
    or ``{"detail_unavailable": <reason>}`` when any of that cannot be built."""
    try:
        from lionagi.state.db import StateDB
    except ImportError:
        return {"detail_unavailable": "state_db_not_installed"}

    try:
        async with StateDB() as db:
            sessions = await db.get_sessions_for_run(run_id)
    except Exception:
        return {"detail_unavailable": "state_db_unreadable"}

    if not sessions:
        return {"detail_unavailable": "no_session_recorded_for_run"}

    # One run can persist more than one session (see get_sessions_for_run);
    # the most recently updated one is the one still worth reading.
    session_row = max(sessions, key=lambda s: s.get("updated_at") or 0)
    session_id = session_row["id"]

    try:
        from lionagi.studio.services import sessions as sessions_svc
    except ImportError:
        return {"detail_unavailable": "studio_extra_not_installed"}

    try:
        session = await sessions_svc.get_session(session_id)
    except Exception:
        return {"detail_unavailable": "session_detail_unreadable"}

    if session is None:
        return {"detail_unavailable": "session_not_found"}

    # A session's graph/segments come from node_metadata, a caller-adjacent
    # JSON blob this reads but never wrote — a shape it doesn't expect (e.g. a
    # node id that isn't hashable) must not turn an opt-in detail request into
    # an error on the base job.status call, so the whole reshape is guarded.
    try:
        nodes = _build_nodes(session, run_id)
        return {
            "nodes": nodes,
            "artifact_contract": _build_artifact_contract(session),
            "stalls": _build_stalls(nodes, session),
        }
    except Exception:
        return {"detail_unavailable": "malformed_session_detail"}


def _segments_by_op_id(session: dict[str, Any]) -> dict[str, dict[str, Any]]:
    segments = session.get("segments")
    if not isinstance(segments, list):
        return {}
    by_op: dict[str, dict[str, Any]] = {}
    for seg in segments:
        if isinstance(seg, dict) and seg.get("op_id"):
            by_op[seg["op_id"]] = seg  # later entries (resumed runs) win
    return by_op


def _spawned_by_map(run_id: str) -> dict[str, str]:
    """Reactive-spawn parents, read from this run's own checkpoint file.

    Best effort only: an absent or unreadable checkpoint (most runs finish
    with none once the DAG completes, and non-flow kinds never write one)
    just means no parent is reported, never a failure of the detail view.
    """
    try:
        from lionagi._paths import RUNS_ROOT

        checkpoint_path = RUNS_ROOT / run_id / "checkpoint.json"
        if not checkpoint_path.is_file():
            return {}
        checkpoint = json.loads(checkpoint_path.read_text())
        spawned = checkpoint.get("spawned")
        if not isinstance(spawned, list):
            return {}
        return {
            e["node_id"]: e["parent_id"]
            for e in spawned
            if isinstance(e, dict) and e.get("node_id") and e.get("parent_id")
        }
    except Exception:
        return {}


def _node_from_branch(
    branch: dict[str, Any], *, role: str, spawned_by: str | None
) -> dict[str, Any]:
    started_at = branch.get("started_at")
    ended_at = branch.get("ended_at")
    status = branch.get("status") or "pending"
    return {
        "id": branch.get("id"),
        "role": role,
        "status": status,
        "started_at": started_at,
        "duration_s": _duration(started_at, ended_at, status),
        "spawned_by": spawned_by,
    }


def _duration(started_at: Any, ended_at: Any, status: str) -> float | None:
    if started_at is None:
        return None
    if ended_at is not None:
        return max(0.0, ended_at - started_at)
    if status == "running":
        return max(0.0, time.time() - started_at)
    return None


def _branch_by_name(session: dict[str, Any]) -> dict[str, dict[str, Any]]:
    branches = session.get("branches") or []
    return {b["name"]: b for b in branches if isinstance(b, dict) and b.get("name")}


def _build_nodes(session: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    branches = session.get("branches") or []
    branch_by_name = _branch_by_name(session)
    spawned_by_map = _spawned_by_map(run_id)
    graph = session.get("graph")
    graph_nodes = graph.get("nodes") if isinstance(graph, dict) else None

    if not graph_nodes:
        # No planned/authored graph — the common shape for a plain `li agent`
        # run. Every branch is its own node; there is nothing to correlate a
        # reactive-spawn parent against, so spawned_by stays unreported.
        return [
            _node_from_branch(b, role=b.get("agent_name") or "", spawned_by=None)
            for b in branches
            if isinstance(b, dict)
        ]

    segments_by_op = _segments_by_op_id(session)
    nodes: list[dict[str, Any]] = []
    for node in graph_nodes:
        if not isinstance(node, dict) or "id" not in node:
            continue
        node_id = node["id"]
        branch = branch_by_name.get(node_id)
        segment = segments_by_op.get(node_id)

        # Branch status is written synchronously on the node's own terminal
        # transition and is the recorded fact; a segment's status can lag it
        # (the heartbeat loop mutates its dict in place between persists), so
        # a branch already carrying a status wins over a possibly-stale one.
        if branch is not None:
            status = branch.get("status") or "pending"
            started_at = branch.get("started_at")
            ended_at = branch.get("ended_at")
        elif segment is not None:
            status = segment.get("status") or "pending"
            started_at = segment.get("started_at")
            ended_at = segment.get("ended_at")
        else:
            status = "pending"
            started_at = None
            ended_at = None

        nodes.append(
            {
                "id": node_id,
                "role": node.get("role") or "",
                "status": status,
                "started_at": started_at,
                "duration_s": _duration(started_at, ended_at, status),
                "spawned_by": spawned_by_map.get(node_id),
            }
        )
    return nodes


def _build_artifact_contract(session: dict[str, Any]) -> list[dict[str, Any]]:
    contract = session.get("artifact_contract_json")
    if not isinstance(contract, dict):
        return []
    expected = contract.get("expected")
    if not isinstance(expected, list):
        return []

    verification = session.get("artifact_verification_json")
    missing_paths: set[str] | None = None
    if isinstance(verification, dict):
        missing = [
            *(verification.get("missing_required") or []),
            *(verification.get("missing_optional") or []),
        ]
        missing_paths = {e["path"] for e in missing if isinstance(e, dict) and e.get("path")}

    out = []
    for entry in expected:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path", "")
        # Per-leg artifact entries are namespaced "<node_id>/<declared_path>"
        # (see cli/orchestrate/flow.py:_leg_artifact_entries) — the first
        # segment is the node that declared the requirement.
        required_by = path.split("/", 1)[0] if "/" in path else None
        satisfied = None if missing_paths is None else path not in missing_paths
        out.append({"path": path, "required_by": required_by, "satisfied": satisfied})
    return out


def _build_stalls(nodes: list[dict[str, Any]], session: dict[str, Any]) -> list[dict[str, Any]]:
    """Stall signal per still-running node.

    ``last_heartbeat_at`` is mutated on the in-memory segment every tick (see
    ``cli/orchestrate/flow.py:_heartbeat_loop``) but is persisted only at the
    node's own status transitions — so a segment read back from storage for a
    node that is still running has never carried a heartbeat write and reading
    it here would always fall through to ``started_at``, reporting every
    long-running node as stalled since the moment it began. A branch's
    ``last_message_at`` (the newest persisted message timestamp for that node,
    computed live from the messages table — see
    ``sessions.py:_fetch_message_bounds``) is a real activity signal that is
    already being written for an unrelated reason, so it is preferred here
    instead. Its absence is preferred honestly, as "unknown", over a heartbeat
    field that in practice never lands mid-run.
    """
    branch_by_name = _branch_by_name(session)
    segments_by_op = _segments_by_op_id(session)
    now = time.time()
    stalls = []
    for node in nodes:
        if node["status"] != "running":
            continue
        branch = branch_by_name.get(node["id"])
        segment = segments_by_op.get(node["id"])
        branch_last_message_at = branch.get("last_message_at") if branch else None
        segment_heartbeat_at = segment.get("last_heartbeat_at") if segment else None

        if branch_last_message_at is not None:
            last_activity: float | None = branch_last_message_at
            idle_source = "last_message_at"
        elif segment_heartbeat_at is not None:
            last_activity = segment_heartbeat_at
            idle_source = "heartbeat"
        else:
            last_activity = None
            idle_source = "none"

        stalls.append(
            {
                "node": node["id"],
                "seconds_idle": max(0.0, now - last_activity)
                if last_activity is not None
                else None,
                "last_activity": last_activity,
                "idle_source": idle_source,
            }
        )
    return stalls
