# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""``run_progress`` Operator read tool and the shared run-reference resolver.

Resolves a human-supplied run reference (a run/session id, an id prefix, a
name/playbook substring, or ``"current"``) to at most one session, then
reports how far that run's operations have gotten. Every number here is a
direct read of stored state — it reflects what the database recorded, not
necessarily a live process (see the ``freshness`` field). ``resolve_run`` is
also imported by ``run_findings.py``, ``resume_run.py``, and
``rename_session.py`` so every tool accepts the same reference vocabulary --
see docs/internals/studio.md ("Resolving a run reference").
"""

from __future__ import annotations

import re
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .redact import MAX_CANDIDATES, public_project, scrub_text

# A full canonical UUID (36 characters, 8-4-4-4-12 hex). A reference in this
# form identifies at most one row and cannot enumerate anything, which is what
# lets it pass the project fence below the same way ``run_detail``'s bare-id
# lookup does.
_EXACT_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

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
            "run the human is looking at. Prefix and substring resolution "
            "need the turn to carry a project context; without one, only a "
            "full 36-character id or 'current' resolves."
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


# The health classifier's own terminal set (lionagi/state/health.py): for
# these statuses it answers "healthy" whenever the run left no residue
# (no stale locks), because health is a LIVENESS concept and a finished
# run has no liveness. Projected next to status="failed", though, the
# word "healthy" reads as a claim about the run's outcome and misleads
# the caller. So for terminal runs this projection drops the vacuous
# "healthy" and keeps only a pathological verdict (e.g. a zombie's
# leftover locks), which is the only health information a finished run
# can still carry.
_TERMINAL_STATUSES = frozenset(
    {"completed", "completed_empty", "failed", "timed_out", "aborted", "cancelled"}
)


def _terminal_safe_health(run: dict[str, Any]) -> str | None:
    health = run.get("effective_health")
    if run.get("status") in _TERMINAL_STATUSES and health == "healthy":
        return None
    return health


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

    Falls open (no restriction) only when the turn identity environment is
    entirely absent -- a real MCP subprocess always has it set (see
    ``engine.py::build_operator_branch``); tests and direct calls that omit
    it get the pre-existing unscoped behavior. When the identity *is*
    present but that turn's own context names no project, this raises
    :class:`MissingOwnerContextError` rather than falling open: a turn with
    an owner but no declared project must never be authorized for every
    project's runs. A lookup failure for a present identity also propagates
    rather than silently falling open.
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
            "operator turn has no project context -- refusing to resolve a run "
            "by prefix or name. Pass the run's full 36-character id instead, "
            "or ask about the run the human has open ('current')."
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
    as the newest ``MAX_CANDIDATES`` plus ``truncated: True``. Every arm is
    scoped to the calling turn's project (when it names one). A turn whose
    identity is present but whose context names no project may still resolve
    an exact full-UUID reference (or 'current') -- a bare id identifies at
    most one row and cannot enumerate, so it is safe where prefix and
    substring resolution are not -- see docs/internals/studio.md
    ("Resolving a run reference").
    """
    from lionagi.cli._util import AmbiguousIdError, fetch_unique_row
    from lionagi.state.db import StateDB

    normalized = ref.strip()
    if not normalized:
        return {"found": False, "reason": "empty run reference"}

    from_current_view = False
    if normalized.lower() == "current":
        # Resolved before the project fence: this only reads the human's own
        # view selection, it enumerates nothing. Whatever id it yields still
        # goes through the same fence-or-exact-id logic as a typed reference.
        session_id = await _resolve_current()
        if session_id is None:
            return {
                "found": False,
                "reason": "no run is selected in the human's current view",
            }
        normalized = session_id
        from_current_view = True

    try:
        project = await _allowed_project()
    except MissingOwnerContextError:
        if _EXACT_UUID_RE.fullmatch(normalized) is None:
            raise
        # A turn with an owner but no declared project may still look up one
        # run by its full id: an exact 36-character UUID identifies at most
        # one row and cannot enumerate anything, the same position
        # ``run_detail`` already takes for a bare id. Prefix and
        # name-substring resolution stay behind the fence above, and a turn
        # that *does* declare a project keeps full ownership scoping on
        # every arm, including this one.
        async with StateDB(readonly=True) as db:
            row = await db.fetch_one("SELECT id FROM sessions WHERE id = ?", (normalized,))
        if row is None:
            return {"found": False, "reason": "no run with that id"}
        return {"found": True, "ambiguous": False, "session_id": row["id"]}

    if from_current_view:
        # A 'current' selection under a project-scoped turn keeps the
        # pre-existing direct lookup + ownership check.
        async with StateDB(readonly=True) as db:
            row = await db.fetch_one("SELECT * FROM sessions WHERE id = ?", (normalized,))
        if row is None or not _owns(row.get("project"), project):
            return {
                "found": False,
                "reason": "the current view's selection is not a run this turn can read",
            }
        return {"found": True, "ambiguous": False, "session_id": normalized}

    async with StateDB(readonly=True) as db:
        try:
            row = await fetch_unique_row(db, "sessions", normalized)
        except AmbiguousIdError as exc:
            owned = await _fetch_ambiguous_candidates(db, exc.candidates, project=project)
            if not owned:
                return {
                    "found": False,
                    "reason": "no matching run within this turn's project scope",
                }
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
                return {
                    "found": False,
                    "reason": "no matching run within this turn's project scope",
                }
            return {"found": True, "ambiguous": False, "session_id": row["id"]}

    if len(normalized) < 3:
        return {
            "found": False,
            "reason": "reference too short: name or playbook substrings need 3+ characters",
        }

    rows = await _find_sessions_by_text(normalized, limit=MAX_CANDIDATES + 1, project=project)
    return _resolution_from_rows(rows)


def _resolution_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"found": False, "reason": "no run matches that name or playbook substring"}
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

# Per-node lifecycle lane, reimplementing over persisted ``session_signals``
# rows the same state machine the live SSE view
# (apps/studio/frontend/src/lib/operationGraph.ts::laneFor) and the in-run
# Signal bus (lionagi/session/signal.py::lane_for) use, since neither is
# usable from a bounded, non-streaming read tool. Keyed on the node's
# authored id (``payload["name"]``), matching how the frontend correlates a
# planned graph node to its live status -- never the runtime op_id.
_NODE_KIND_TO_STATE: dict[str, str] = {
    "NodeQueued": "queued",
    "NodeStarted": "running",
    "NodeAwaitingApproval": "awaiting_approval",
    "NodePaused": "paused",
    "NodeCompleted": "succeeded",
    "NodeFailed": "failed",
    "NodeSkipped": "skipped",
    "NodeEscalated": "escalated",
}
_NODE_TERMINAL_STATES = frozenset({"succeeded", "failed", "skipped", "escalated"})
# Node lanes that claim in-flight work — on a run that has itself reached a
# terminal status these are stale by definition (the engine died or was
# killed before emitting the node's own terminal signal).
_NODE_INFLIGHT_STATES = frozenset({"running", "awaiting_approval", "paused"})
_NODE_STATE_BUCKET = {
    "queued": "pending",
    "running": "running",
    "awaiting_approval": "running",
    "paused": "running",
    "succeeded": "completed",
    "failed": "failed",
    # A node the run's death cut off mid-flight. It did not observably fail
    # on its own, but it will never complete either — failure is the only
    # scalar bucket that doesn't misread as success or outstanding work; the
    # separate abortedCount keeps it distinguishable from genuine failures.
    "aborted": "failed",
    # Settled, so it folds into "completed" rather than "pending": a skipped
    # node will never run, and parking it in pending would leave a finished
    # flow reporting outstanding work forever. It is not a failure either --
    # the edge condition did what it was written to do. The separate
    # skippedCount below keeps it from silently inflating the success figure.
    "skipped": "completed",
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


def _evidence_failed_op_ids(run: dict[str, Any]) -> set[str]:
    """Node ids the run's own failure evidence names as failed operations."""
    refs = run.get("status_evidence_refs")
    out: set[str] = set()
    if isinstance(refs, list):
        for ref in refs:
            if (
                isinstance(ref, dict)
                and ref.get("kind") == "failed_operation"
                and isinstance(ref.get("id"), str)
                and ref["id"]
            ):
                out.add(ref["id"])
    return out


async def _dag_progress(
    session_id: str, graph: dict[str, Any], run: dict[str, Any]
) -> dict[str, Any]:
    """DAG-node totals/state for a run's planned graph, including nodes with
    no materialized branch yet. Honest about what it cannot map: a node with
    no recorded lifecycle signal reports status "unknown" rather than being
    silently assumed not-yet-started.

    Once the run itself is terminal, per-node lanes are reconciled against
    that fact instead of replayed verbatim: a dead run's engine often never
    emitted node-terminal signals, so the raw lanes would report agents
    mid-flight days after the run ended. Nodes the run's failure evidence
    names read "failed"; other in-flight lanes read "aborted"; nodes that
    never started read "skipped"."""
    nodes = [
        node
        for node in (graph.get("nodes") or [])
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    ]
    lanes = await _node_lanes_by_name(session_id)

    from lionagi.state.db import SESSION_TERMINAL_STATUSES

    if run.get("status") in SESSION_TERMINAL_STATUSES:
        named_failed = _evidence_failed_op_ids(run)
        for node in nodes:
            node_id = node["id"]
            lane = lanes.get(node_id)
            if node_id in named_failed:
                lanes[node_id] = "failed"
            elif lane in _NODE_INFLIGHT_STATES:
                lanes[node_id] = "aborted"
            elif lane == "queued" or lane is None:
                lanes[node_id] = "skipped"

    completed = running = failed = pending = unknown = escalated = skipped = 0
    aborted = 0
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
            elif lane == "skipped":
                skipped += 1
            elif lane == "aborted":
                aborted += 1
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
        # Same convention, and load-bearing for the same reason: without it a
        # caller reading only the scalars cannot tell work that ran and
        # succeeded from work an edge condition passed over.
        "skippedCount": skipped,
        # Nodes cut off mid-flight by the run's own death. They fold into the
        # failed scalar (they will never complete) but a genuine op failure
        # and an engine death ask for different responses.
        "abortedCount": aborted,
        "nodes": node_out,
    }


async def run_progress(arguments: dict[str, Any]) -> dict[str, Any]:
    args = RunProgressInput.model_validate(arguments)
    resolution = await resolve_run(args.run)
    if not resolution["found"]:
        return {"found": False, "reason": resolution.get("reason")}
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
        return {"found": False, "reason": "the resolved run vanished before it could be read"}

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
        dag_progress = await _dag_progress(resolution["session_id"], graph, run)
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
        "effectiveHealth": _terminal_safe_health(run),
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
