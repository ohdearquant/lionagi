# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""``run_progress`` Operator read tool and the shared run-reference resolver.

Resolves a human-supplied run reference (a run/session id, an id prefix, a
name/playbook substring, or ``"current"``) to at most one session, then
reports how far that run's operations have gotten. Every number here is a
direct read of stored state — it reflects what the database recorded, not
necessarily a live process (see the ``freshness`` field).

Id/prefix resolution reuses ``lionagi.cli._util.fetch_unique_row`` — the same
exact-id-then-prefix primitive ``li kill`` and ``cancel_run.py`` use — rather
than a bespoke hex-only regex, so a reference this tool accepts and a
reference ``cancel_run`` accepts resolve identically.

``resolve_run`` is imported by ``run_findings.py`` so both read tools accept
the same reference vocabulary.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .redact import MAX_CANDIDATES, public_project, scrub_text

__all__ = ("MissingOwnerContextError", "RunProgressInput", "resolve_run", "run_progress")


class MissingOwnerContextError(ValueError):
    """The calling turn has no durable project mapping to authorize against.

    Raised before any row is resolved or reported on -- a turn whose
    identity is present but whose own context names no project must never
    fall back to matching every project's runs. Mirrors
    ``cancel_run.py``'s own copy of this error -- kept separate rather than
    a shared import for the same reason ``_allowed_project`` below is its
    own copy.
    """

    code = "missing_owner_context"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RunProgressInput(_StrictModel):
    run: str = Field(
        min_length=1,
        max_length=200,
        description=(
            "Run reference: a run/session id, an id prefix, a name or "
            "playbook substring (minimum 3 characters), or 'current' for the "
            "run the human is looking at."
        ),
    )


async def _resolve_current() -> str | None:
    """Resolve the 'current' reference via the existing get_current_view tool.

    Imported lazily: application_mcp.py will import this module's public
    names at its own module top once step 7 wires the tool registries, and a
    top-level import back into application_mcp here would be a load-time
    circular import. By the time this function actually runs, both modules
    are fully initialized, so the lazy import is safe.
    """
    from .application_mcp import get_current_view

    view = await get_current_view({})
    if not view.get("known"):
        return None
    selection = view.get("selection")
    if not isinstance(selection, dict):
        return None
    for key in ("s", "runId", "run_id", "sessionId"):
        candidate = selection.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _scrub(value: Any) -> Any:
    """Pass a non-string value through unchanged; scrub a string the same
    way every other free-text projection in this module is scrubbed. A
    name/model/playbook label is operator-supplied text, not a validated
    enum, so it can carry the same secret- or path-shaped substrings a
    message body can."""
    return scrub_text(value) if isinstance(value, str) else value


def _candidate(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "name": _scrub(row.get("name")),
        "playbookName": _scrub(row.get("playbook_name")),
        "agentName": _scrub(row.get("agent_name")),
        "status": row.get("status"),
        "project": public_project(row.get("project")),
    }


def _owns(row_project: Any, project: str | None) -> bool:
    """Whether a session's ``project`` column is visible to a turn scoped to
    ``project``. ``project is None`` preserves the pre-existing unscoped
    behavior (see ``_allowed_project``'s own docstring)."""
    return project is None or row_project == project


async def _fetch_ambiguous_candidates(
    db: Any, ids: list[str], *, project: str | None
) -> list[dict[str, Any]]:
    """Re-fetch the display columns for the ids an AmbiguousIdError named,
    dropping any row ``project`` may not see.

    fetch_unique_row()/AmbiguousIdError only carry ids (see
    lionagi/cli/_util.py) — enough to disambiguate on the CLI, not enough to
    show a project/status card here, so this does one bounded follow-up read.
    A foreign-project row is stripped out entirely rather than merely
    de-emphasized: it must not appear as a candidate at all.
    """
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = await db.fetch_all(
        f"SELECT id, name, playbook_name, agent_name, status, project "  # noqa: S608
        f"FROM sessions WHERE id IN ({placeholders})",
        tuple(ids),
    )
    by_id = {row["id"]: row for row in rows}
    return [
        _candidate(by_id[session_id])
        for session_id in ids
        if session_id in by_id and _owns(by_id[session_id].get("project"), project)
    ]


async def _allowed_project() -> str | None:
    """The project this Operator turn is scoped to, or ``None`` when there is
    no turn identity to enforce at all.

    Reads the same durable turn identity ``cancel_run.py``/``get_current_view``
    use. Falls open (no restriction) only when the turn identity environment
    is entirely absent — a real MCP subprocess always has it set (see
    ``engine.py::build_operator_branch``); tests and direct calls that omit
    the whole identity get the pre-existing unscoped behavior rather than a
    hard failure. When the identity *is* present -- a real turn exists --
    but that turn's own context names no project, this raises
    :class:`MissingOwnerContextError` rather than falling open: a turn with
    an owner but no declared project must never be treated as authorized
    for every project's runs. A lookup failure for a present identity also
    propagates rather than silently falling open, since that would defeat
    the isolation this exists to provide.
    """
    import os

    db_path = os.environ.get("LIONAGI_OPERATOR_DB_PATH")
    conversation_id = os.environ.get("LIONAGI_OPERATOR_CONVERSATION_ID")
    request_id = os.environ.get("LIONAGI_OPERATOR_REQUEST_ID")
    if not db_path or not conversation_id or not request_id:
        return None

    from .store import OperatorStore

    store = OperatorStore(db_path)
    turn = await store.get_turn(request_id)
    context = turn.get("context")
    project = context.get("project") if isinstance(context, dict) else None
    if not isinstance(project, str) or not project:
        raise MissingOwnerContextError(
            "operator turn has no project context -- refusing to resolve any run"
        )
    return project


async def _find_sessions_by_text(
    ref: str, *, limit: int, project: str | None
) -> list[dict[str, Any]]:
    """Sessions whose name or playbook name contains ``ref`` (case-insensitive),
    scoped to ``project`` when the calling turn names one — a name/playbook
    substring search must not enumerate another project's runs."""
    from lionagi.studio.services.sessions import SessionFilter, list_sessions

    by_name = await list_sessions(limit=limit, where=SessionFilter(search=ref, project=project))
    by_playbook = await list_sessions(
        limit=limit, where=SessionFilter(playbook=ref, project=project)
    )
    merged: dict[str, dict[str, Any]] = {}
    for row in (*by_name, *by_playbook):
        merged.setdefault(row["id"], row)
    ordered = sorted(merged.values(), key=lambda row: row.get("updated_at") or 0, reverse=True)
    return ordered[:limit]


async def resolve_run(ref: str) -> dict[str, Any]:
    """Resolve a human-named run reference to at most one session.

    Returns one of:
      - ``{"found": False}``
      - ``{"found": True, "ambiguous": True, "candidates": [...], "truncated": bool}``
      - ``{"found": True, "ambiguous": False, "session_id": "..."}``

    Never guesses: an id prefix matching more than one session, or a
    name/playbook substring matching 2-``MAX_CANDIDATES`` sessions, comes
    back as candidates; more than ``MAX_CANDIDATES`` text matches come back
    as the newest ``MAX_CANDIDATES`` plus ``truncated: True``.

    Every arm -- exact id, ambiguous prefix, "current", and the text search
    below -- is scoped to the calling turn's project (when it names one). A
    foreign project's run is reported exactly like a nonexistent one, never
    as e.g. an ambiguity candidate or a resolved id, which would themselves
    confirm the id exists.
    """
    from lionagi.cli._util import AmbiguousIdError, fetch_unique_row
    from lionagi.state.db import StateDB

    normalized = ref.strip()
    if not normalized:
        return {"found": False}

    project = await _allowed_project()

    if normalized.lower() == "current":
        session_id = await _resolve_current()
        if session_id is None:
            return {"found": False}
        async with StateDB(readonly=True) as db:
            row = await db.fetch_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if row is None or not _owns(row.get("project"), project):
            return {"found": False}
        return {"found": True, "ambiguous": False, "session_id": session_id}

    async with StateDB(readonly=True) as db:
        try:
            row = await fetch_unique_row(db, "sessions", normalized)
        except AmbiguousIdError as exc:
            owned = await _fetch_ambiguous_candidates(db, exc.candidates, project=project)
            if not owned:
                return {"found": False}
            if len(owned) == 1:
                return {"found": True, "ambiguous": False, "session_id": owned[0]["id"]}
            return {
                "found": True,
                "ambiguous": True,
                "candidates": owned,
                # fetch_unique_row's own prefix scan caps at 6 rows (see
                # lionagi/cli/_util.py::_CANDIDATES_SHOWN) before this
                # function ever sees the list, so hitting that cap is the
                # only honest truncation signal available here.
                "truncated": len(exc.candidates) > 5,
            }
        if row is not None:
            if not _owns(row.get("project"), project):
                return {"found": False}
            return {"found": True, "ambiguous": False, "session_id": row["id"]}

    if len(normalized) < 3:
        return {"found": False}

    rows = await _find_sessions_by_text(normalized, limit=MAX_CANDIDATES + 1, project=project)
    return _resolution_from_rows(rows)


def _resolution_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"found": False}
    if len(rows) == 1:
        return {"found": True, "ambiguous": False, "session_id": rows[0]["id"]}
    truncated = len(rows) > MAX_CANDIDATES
    return {
        "found": True,
        "ambiguous": True,
        "candidates": [_candidate(row) for row in rows[:MAX_CANDIDATES]],
        "truncated": truncated,
    }


# ADR-0064: completed_empty is terminal but unsuccessful (no trusted
# evidence produced) -- it is not in this set, so it falls through to the
# SESSION_TERMINAL_STATUSES branch below and counts as an op failure.
_COMPLETED_STATUSES = frozenset({"completed"})

# Per-node lifecycle lane, mirroring the ONLY existing "how far along is a DAG
# run" projection this codebase has:
# apps/studio/frontend/src/lib/operationGraph.ts::laneFor/buildNodeStatusesByName
# (live SSE events) and lionagi/session/signal.py::lane_for (live Signal
# objects). Neither is usable from a bounded, non-streaming read tool, so this
# reimplements the same state machine over the persisted
# ``session_signals`` rows both of those ultimately read from — the frontend
# correlates a planned graph node's live status by its authored id, carried as
# ``payload["name"]`` on every Node* signal, never by the runtime op_id (see
# operationGraph.ts's own comment on this), which is why this keys on name too.
_NODE_KIND_TO_STATE: dict[str, str] = {
    "NodeQueued": "queued",
    "NodeStarted": "running",
    "NodeAwaitingApproval": "awaiting_approval",
    "NodePaused": "paused",
    "NodeCompleted": "succeeded",
    "NodeFailed": "failed",
    "NodeEscalated": "escalated",
}
_NODE_TERMINAL_STATES = frozenset({"succeeded", "failed", "escalated"})
_NODE_STATE_BUCKET = {
    "queued": "pending",
    "running": "running",
    "awaiting_approval": "running",
    "paused": "running",
    "succeeded": "completed",
    "failed": "failed",
    # The scalar API has four buckets that must sum to total. Keep the
    # per-node "escalated" outcome distinct while its aggregate waits for
    # follow-up in pending rather than inflating failure.
    "escalated": "pending",
}
# Matches services.signals.get_signals_after's own default bound — this is
# not a new cap invented for this tool.
_SIGNAL_READ_LIMIT = 500


def _node_lane(events: list[tuple[str, str | None]]) -> str:
    state = "queued"
    in_terminal = False
    for kind, route in events:
        # A soft ("fyi") NodeEscalated is informational; the node keeps
        # working toward its own terminal state (mirrors laneFor/lane_for).
        if kind == "NodeEscalated" and route == "notify":
            continue
        new_state = _NODE_KIND_TO_STATE.get(kind)
        if new_state is None:
            continue
        if in_terminal and new_state not in ("queued", "running"):
            continue
        state = new_state
        in_terminal = state in _NODE_TERMINAL_STATES
    return state


async def _node_lanes_by_name(session_id: str) -> dict[str, str]:
    from lionagi.state.db import StateDB

    async with StateDB(readonly=True) as db:
        signals = await db.get_session_signals_after(session_id, 0, limit=_SIGNAL_READ_LIMIT)

    by_name: dict[str, list[tuple[str, str | None]]] = {}
    for signal in signals:
        kind = signal.get("kind")
        if kind not in _NODE_KIND_TO_STATE:
            continue
        payload = signal.get("payload") or {}
        name = payload.get("name")
        if not isinstance(name, str) or not name:
            continue
        route = payload.get("route")
        by_name.setdefault(name, []).append((kind, route if isinstance(route, str) else None))
    return {name: _node_lane(events) for name, events in by_name.items()}


async def _dag_progress(session_id: str, graph: dict[str, Any]) -> dict[str, Any]:
    """DAG-node totals/state for a run's planned graph, including nodes with
    no materialized branch yet. Honest about what it cannot map: a node with
    no recorded lifecycle signal reports status "unknown" rather than being
    silently assumed not-yet-started."""
    nodes = [
        node
        for node in (graph.get("nodes") or [])
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    ]
    lanes = await _node_lanes_by_name(session_id)

    completed = running = failed = pending = unknown = escalated = 0
    node_out: list[dict[str, Any]] = []
    for node in nodes:
        node_id = node["id"]
        lane = lanes.get(node_id)
        if lane is None:
            unknown += 1
            bucket = "unknown"
        else:
            bucket = _NODE_STATE_BUCKET.get(lane, "unknown")
            if bucket == "completed":
                completed += 1
            elif bucket == "running":
                running += 1
            elif bucket == "failed":
                failed += 1
            else:
                pending += 1
            # Counted alongside the buckets rather than inside them: an
            # escalation folds into pending for the sum, but a caller reading
            # only the scalars cannot otherwise tell a node that is queued
            # from one that has stopped and is waiting on a human decision,
            # and those ask for opposite responses.
            if lane == "escalated":
                escalated += 1
        node_out.append(
            {
                "id": node_id,
                "label": node.get("label") or node_id,
                "status": lane or "unknown",
            }
        )

    return {
        "total": len(nodes),
        "completed": completed,
        "running": running,
        "failed": failed,
        # A node this tool cannot map to a lifecycle signal folds into the
        # pending scalar bucket (it has not observably started) while still
        # being reported as its own "unknown" status per node below — the
        # scalars must sum to "total", the per-node list stays honest.
        "pending": pending + unknown,
        "unknownCount": unknown,
        # Always present, including as zero. A count that only appears when
        # non-zero is the field callers never wire up, because every run they
        # develop against lacks it.
        "escalatedCount": escalated,
        "nodes": node_out,
    }


async def run_progress(arguments: dict[str, Any]) -> dict[str, Any]:
    args = RunProgressInput.model_validate(arguments)
    resolution = await resolve_run(args.run)
    if not resolution["found"]:
        return {"found": False}
    if resolution.get("ambiguous"):
        return {
            "found": True,
            "ambiguous": True,
            "candidates": resolution["candidates"],
            "truncated": resolution.get("truncated", False),
        }

    from lionagi.state.db import SESSION_TERMINAL_STATUSES
    from lionagi.studio.services.runs import get_run

    run = await get_run(resolution["session_id"])
    if run is None:
        return {"found": False}

    branches = run.get("branches") or []
    ops_completed = ops_running = ops_failed = ops_pending = 0
    current_ops: list[dict[str, Any]] = []
    for branch in branches:
        status = branch.get("status")
        if status in _COMPLETED_STATUSES:
            ops_completed += 1
        elif status in SESSION_TERMINAL_STATUSES:
            ops_failed += 1
        elif status is not None and branch.get("started_at") is not None:
            ops_running += 1
            current_ops.append(
                {
                    "name": _scrub(branch.get("name")),
                    "agentName": _scrub(branch.get("agent_name")),
                    "status": status,
                }
            )
        else:
            ops_pending += 1

    started_at = run.get("started_at")
    ended_at = run.get("ended_at")
    now = time.time()
    elapsed_seconds = (
        (ended_at if ended_at is not None else now) - started_at if started_at is not None else None
    )

    graph = run.get("graph")
    dag_progress: dict[str, Any] | None = None
    ops_total = len(branches)
    if isinstance(graph, dict) and graph.get("nodes"):
        # A DAG can have planned nodes with no materialized branch yet, so
        # `len(branches)` under-counts and cannot say how far through the
        # graph the run is. Derive totals/state from the graph itself instead
        # -- branches remain the source for `currentOps` below, which is
        # about what has actually started, not what is merely planned.
        dag_progress = await _dag_progress(resolution["session_id"], graph)
        ops_total = dag_progress["total"]
        ops_completed = dag_progress["completed"]
        ops_running = dag_progress["running"]
        ops_failed = dag_progress["failed"]
        ops_pending = dag_progress["pending"]

    return {
        "found": True,
        "ambiguous": False,
        "id": run.get("id"),
        "status": run.get("status"),
        "effectiveHealth": run.get("effective_health"),
        "startedAt": started_at,
        "endedAt": ended_at,
        "elapsedSeconds": elapsed_seconds,
        "opsTotal": ops_total,
        "opsCompleted": ops_completed,
        "opsRunning": ops_running,
        "opsFailed": ops_failed,
        "opsPending": ops_pending,
        "currentOps": current_ops,
        "model": _scrub(run.get("model")),
        "playbookName": _scrub(run.get("playbook_name")),
        "agentName": _scrub(run.get("agent_name")),
        "project": public_project(run.get("project")),
        "hasGraph": bool(run.get("graph")),
        "dagProgress": dag_progress,
        "freshness": f"direct database read at {now:.3f}",
    }
