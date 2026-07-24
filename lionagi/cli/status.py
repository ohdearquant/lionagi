# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li agent status` / `li play status` / `li o ctl status` — read-only lifecycle surfaces.

See docs/internals/cli.md for the `--json` output key-set and exit-code contract.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any

from ._logging import log_error
from ._project import detect_project
from ._util import AmbiguousIdError, fetch_unique_row

__all__ = (
    "run_agent_status",
    "run_play_status",
    "run_ctl_status",
)

# ── Status vocabularies (mirror the CHECK constraints / VALID_* sets in state/db.py) ──
# "cancelled" lands in FAILURE by elimination (neither success nor still-running).

_SESSION_SUCCESS = frozenset({"completed"})
# "completed_empty" (ran clean but produced no commits/artifacts — the
# completion-trust gate) is classified as failure here, not success.
_SESSION_FAILURE = frozenset({"completed_empty", "failed", "timed_out", "aborted", "cancelled"})
_PLAY_SUCCESS = frozenset({"merged"})
_PLAY_FAILURE = frozenset({"gate_failed", "escalated", "blocked", "aborted_after_finish"})

EXIT_RUNNING = 3
EXIT_UNKNOWN = 2

# StateDB.open() always runs a schema-apply step under a write transaction,
# even for pure reads — this bounds a status read so it fails fast, not hangs.
_DB_BUSY_TIMEOUT_S = 10.0


# ── Entity resolution ───────────────────────────────────────────────────────


async def _fetch_by_id(db: Any, table: str, id_or_short: str) -> dict[str, Any] | None:
    """Exact-id fetch, then unique-prefix fetch, scoped to one table (see
    _util.resolve_entity for the multi-table sweep).

    Raises `AmbiguousIdError` when a prefix matches more than one row; the
    status entry points turn that into EXIT_UNKNOWN, never a status readout
    for an arbitrarily chosen run.
    """
    row = await fetch_unique_row(db, table, id_or_short)
    return db._row_to_dict(row) if row is not None else None


async def _latest_session(
    db: Any, *, invocation_kinds: tuple[str, ...], project: str | None
) -> dict[str, Any] | None:
    """Newest session (by updated_at) among *invocation_kinds*, optionally project-scoped."""
    placeholders = ", ".join("?" for _ in invocation_kinds)
    sql = f"SELECT * FROM sessions WHERE invocation_kind IN ({placeholders})"  # noqa: S608
    params: list[Any] = list(invocation_kinds)
    if project:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY updated_at DESC LIMIT 1"
    row = await db.fetch_one(sql, params)
    return db._row_to_dict(row) if row is not None else None


async def _resolve_session_by_branch_id(db: Any, entity_id: str) -> dict[str, Any] | None:
    """Fallback: resolve *entity_id* as a branch_id to its owning session.

    See docs/internals/cli.md for the branches/sessions schema note.
    """
    branch = await _fetch_by_id(db, "branches", entity_id)
    if branch is None:
        return None
    return await db.get_session(branch["session_id"])


async def _resolve_agent_target(
    db: Any, entity_id: str | None, project: str | None
) -> tuple[str, dict[str, Any]] | None:
    """`li agent status` resolution: session (any kind), invocation, or a
    branch_id, by id; default-latest is scoped to agent-kind sessions."""
    if entity_id:
        row = await _fetch_by_id(db, "sessions", entity_id)
        if row is not None:
            return "session", row
        row = await _fetch_by_id(db, "invocations", entity_id)
        if row is not None:
            return "invocation", row
        row = await _resolve_session_by_branch_id(db, entity_id)
        if row is not None:
            return "session", row
        return None
    row = await _latest_session(db, invocation_kinds=("agent",), project=project)
    return ("session", row) if row is not None else None


async def _resolve_play_target(
    db: Any, entity_id: str | None, project: str | None
) -> tuple[str, dict[str, Any]] | None:
    """`li play status` resolution: session, then invocation, then a show
    sub-play row, by id; default-latest is scoped to play/flow-kind sessions.
    """
    if entity_id:
        row = await _fetch_by_id(db, "sessions", entity_id)
        if row is not None:
            return "session", row
        row = await _fetch_by_id(db, "invocations", entity_id)
        if row is not None:
            return "invocation", row
        row = await _fetch_by_id(db, "plays", entity_id)
        if row is not None:
            return "play", row
        return None
    row = await _latest_session(db, invocation_kinds=("play", "flow"), project=project)
    return ("session", row) if row is not None else None


async def _resolve_any_target(db: Any, entity_id: str) -> tuple[str, dict[str, Any]] | None:
    """`li o ctl status <id>` resolution: no kind scoping, id required (no
    latest). Falls back to branch_id last, after sessions/invocations/plays."""
    row = await _fetch_by_id(db, "sessions", entity_id)
    if row is not None:
        return "session", row
    row = await _fetch_by_id(db, "invocations", entity_id)
    if row is not None:
        return "invocation", row
    row = await _fetch_by_id(db, "plays", entity_id)
    if row is not None:
        return "play", row
    row = await _resolve_session_by_branch_id(db, entity_id)
    if row is not None:
        return "session", row
    return None


async def _resolve_primary_session(
    db: Any, entity_type: str, row: dict[str, Any]
) -> dict[str, Any] | None:
    """Best-effort backing session for model/provider/phase/progress enrichment."""
    if entity_type == "session":
        return row
    if entity_type == "play":
        sid = row.get("session_id")
        return await db.get_session(sid) if sid else None
    if entity_type == "invocation":
        sessions = await db.list_sessions_for_invocation(row["id"])
        if not sessions:
            return None
        return max(sessions, key=lambda s: s.get("updated_at") or 0)
    return None


# ── Op progress (session_signals → lane_for reduction, ADR-0033) ───────────


async def _all_session_signals(db: Any, session_id: str) -> list[dict[str, Any]]:
    """Page through session_signals for *session_id*, ordered by seq, no cap."""
    rows: list[dict[str, Any]] = []
    after_seq = 0
    while True:
        page = await db.get_session_signals_after(session_id, after_seq, limit=1000)
        if not page:
            break
        rows.extend(page)
        after_seq = page[-1]["seq"]
        if len(page) < 1000:
            break
    return rows


async def _op_progress(db: Any, session_id: str) -> tuple[int, int] | None:
    """Reduce session_signals into (completed, total) op counts via lane_for().

    None when no op-scoped signals exist yet — "not derivable", not a stub.
    """
    rows = await _all_session_signals(db, session_id)
    if not rows:
        return None

    from lionagi.session import signal as _signal_mod

    by_op: dict[str, list[Any]] = {}
    for r in rows:
        op_id = r.get("op_id")
        if not op_id:
            continue
        cls = getattr(_signal_mod, r.get("kind") or "", None)
        if not (isinstance(cls, type) and issubclass(cls, _signal_mod.Signal)):
            continue
        try:
            instance = cls()
        except Exception:  # noqa: BLE001, S112 — e.g. StructuredOutput requires `data`;
            continue  # best-effort reconstruction, skip what can't bare-construct.
        by_op.setdefault(op_id, []).append(instance)
    if not by_op:
        return None
    total = len(by_op)
    completed = sum(1 for sigs in by_op.values() if _signal_mod.lane_for(sigs) == "succeeded")
    return completed, total


# ── Terminal classification + degraded-completion detection ────────────────


def _classify(entity_type: str, status: str) -> tuple[bool, str, int]:
    """Map a raw stored status to (terminal, exit_class, exit_code)."""
    if entity_type == "play":
        success, failure = _PLAY_SUCCESS, _PLAY_FAILURE
    else:  # session, invocation share one vocabulary
        success, failure = _SESSION_SUCCESS, _SESSION_FAILURE
    if status in success:
        return True, "success", 0
    if status in failure:
        return True, "failure", 1
    return False, "running", EXIT_RUNNING


def _detect_degraded(
    *, entity_type: str, status: str, primary_session: dict[str, Any] | None
) -> tuple[bool, str | None]:
    """Detect a terminal-success record whose backing session shows no sign
    normal teardown ran. See docs/internals/cli.md for the heuristic rationale."""
    if primary_session is None:
        return False, None
    success = status in (_PLAY_SUCCESS if entity_type == "play" else _SESSION_SUCCESS)
    if not success:
        return False, None
    if primary_session.get("source_kind") not in (None, "live"):
        return False, None
    if primary_session.get("num_turns") is not None:
        return False, None
    phase = primary_session.get("current_phase")
    detail = f"; current_phase={phase!r} never advanced to a terminal marker" if phase else ""
    return True, f"status={status!r} but run-usage metrics (num_turns) were never recorded{detail}"


# ── View assembly ────────────────────────────────────────────────────────


async def _build_view(
    db: Any, *, command: str, entity_type: str, row: dict[str, Any]
) -> dict[str, Any]:
    primary_session = await _resolve_primary_session(db, entity_type, row)

    progress_completed: int | None = None
    progress_total: int | None = None
    if primary_session is not None:
        prog = await _op_progress(db, primary_session["id"])
        if prog is not None:
            progress_completed, progress_total = prog

    branch_id: str | None = None
    pending_controls: list[dict[str, Any]] = []
    if primary_session is not None:
        branches = await db.list_branches(primary_session["id"])
        if branches:
            branch_id = max(branches, key=lambda b: b.get("created_at") or 0).get("id")
        pending_controls = [
            {"id": c["id"], "verb": c["verb"], "created_at": c["created_at"]}
            for c in await db.list_pending_session_controls(primary_session["id"])
        ]

    status = row.get("status") or ""
    terminal, exit_class, exit_code = _classify(entity_type, status)
    degraded, degraded_reason = _detect_degraded(
        entity_type=entity_type, status=status, primary_session=primary_session
    )

    model = provider = current_phase = last_activity = None
    if primary_session is not None:
        model = primary_session.get("model")
        provider = primary_session.get("provider")
        current_phase = primary_session.get("current_phase")
        last_activity = primary_session.get("last_message_at") or primary_session.get("updated_at")
    else:
        last_activity = row.get("updated_at")

    if entity_type == "session":
        project = row.get("project")
        label = row.get("agent_name")
        invocation_id = row.get("invocation_id")
    elif entity_type == "invocation":
        project = (primary_session or {}).get("project")
        label = row.get("skill")
        invocation_id = row.get("id")
    else:  # play
        project = (primary_session or {}).get("project")
        label = row.get("name")
        invocation_id = (primary_session or {}).get("invocation_id")

    return {
        "id": row.get("id"),
        "entity_type": entity_type,
        "command": command,
        "status": status,
        "terminal": terminal,
        "exit_class": exit_class,
        "exit_code": exit_code,
        "current_phase": current_phase,
        "progress_completed": progress_completed,
        "progress_total": progress_total,
        "model": model,
        "provider": provider,
        "project": project,
        "last_activity_at": last_activity,
        "session_id": primary_session.get("id") if primary_session else None,
        "branch_id": branch_id,
        "invocation_id": invocation_id,
        "label": label,
        "degraded": degraded,
        "degraded_reason": degraded_reason,
        "status_reason_code": row.get("status_reason_code"),
        "status_reason_summary": row.get("status_reason_summary"),
        "status_evidence_refs": row.get("status_evidence_refs"),
        "pending_controls": pending_controls,
    }


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _render_human(view: dict[str, Any]) -> str:
    from .monitor import _bold, _colour_status, _dim

    lines = [_bold(f"{view['entity_type'].upper()}  {view['id']}")]
    status_line = f"  status:      {_colour_status(view['status'] or '?')}"
    if view["terminal"]:
        status_line += "  [terminal]"
    lines.append(status_line)
    if view["current_phase"]:
        lines.append(f"  phase:       {view['current_phase']}")
    if view["progress_total"] is not None:
        lines.append(
            f"  progress:    {view['progress_completed']}/{view['progress_total']} ops complete"
        )
    if view["label"]:
        lines.append(f"  label:       {view['label']}")
    lines.append(f"  model:       {view['model'] or '-'}")
    lines.append(f"  provider:    {view['provider'] or '-'}")
    if view["project"]:
        lines.append(f"  project:     {view['project']}")
    lines.append(f"  last active: {_fmt_ts(view['last_activity_at'])}")
    if view["session_id"] and view["session_id"] != view["id"]:
        lines.append(f"  session:     {view['session_id']}")
    if view["branch_id"]:
        lines.append(_dim(f'  resume:      li agent -r {view["branch_id"]} "..."'))
    if view["invocation_id"] and view["invocation_id"] != view["id"]:
        lines.append(f"  invocation:  {view['invocation_id']}")

    if view["pending_controls"]:
        lines.append("")
        lines.append(_dim("  -- pending controls --"))
        for ctl in view["pending_controls"]:
            lines.append(f"    {ctl['verb']:<8} {ctl['id']}  queued {_fmt_ts(ctl['created_at'])}")

    if view["degraded"]:
        lines.append("")
        lines.append(_dim(f"  [degraded]   {view['degraded_reason']}"))

    if view["terminal"]:
        lines.append("")
        lines.append(f"  exit_class:  {view['exit_class']}")
        if view["status_reason_code"]:
            lines.append(f"  reason:      {view['status_reason_code']}")
        if view["status_reason_summary"]:
            lines.append(f"  summary:     {view['status_reason_summary']}")
        evidence = view["status_evidence_refs"]
        if evidence:
            lines.append(_dim("  -- evidence --"))
            for ev in evidence:
                if isinstance(ev, dict):
                    ev_label = ev.get("label") or ev.get("kind") or "evidence"
                    ref = ev.get("path") or ev.get("id") or ev.get("ref") or ""
                    lines.append(f"    {ev_label}: {ref}")
                else:
                    lines.append(f"    {ev}")

    return "\n".join(lines)


# ── Async main routine ───────────────────────────────────────────────────


async def _run_status_inner(
    *, command: str, entity_id: str | None, as_json: bool
) -> tuple[str, int]:
    from lionagi.state.db import DEFAULT_DB_PATH, StateDB

    if not DEFAULT_DB_PATH.exists():
        return f"state.db not found — no {command} runs recorded yet", EXIT_UNKNOWN

    async with StateDB() as db:
        project = detect_project(Path.cwd())[0]
        try:
            if command == "agent":
                target = await _resolve_agent_target(db, entity_id, project)
            elif command == "play":
                target = await _resolve_play_target(db, entity_id, project)
            else:  # "ctl" — generic, id required (enforced by argparse), no kind scoping
                target = await _resolve_any_target(db, entity_id) if entity_id else None
        except AmbiguousIdError as exc:
            return str(exc), EXIT_UNKNOWN

        if target is None:
            who = f"id {entity_id!r}" if entity_id else f"latest {command} run"
            scope = f" (project={project!r})" if project else ""
            return f"no {who} found{scope}", EXIT_UNKNOWN

        entity_type, row = target
        view = await _build_view(db, command=command, entity_type=entity_type, row=row)

    if as_json:
        return json.dumps(view), view["exit_code"]
    return _render_human(view), view["exit_code"]


async def _run_status(*, command: str, entity_id: str | None, as_json: bool) -> tuple[str, int]:
    try:
        return await asyncio.wait_for(
            _run_status_inner(command=command, entity_id=entity_id, as_json=as_json),
            timeout=_DB_BUSY_TIMEOUT_S,
        )
    except (TimeoutError, asyncio.TimeoutError):  # 3.10 support: not aliased until 3.11
        return (
            f"state.db busy (no read within {_DB_BUSY_TIMEOUT_S:.0f}s) — "
            "another writer may be holding a long transaction; try again",
            EXIT_UNKNOWN,
        )


def _dispatch(command: str, entity_id: str | None, as_json: bool) -> int:
    from lionagi.ln.concurrency import run_async

    output, exit_code = run_async(
        _run_status(command=command, entity_id=entity_id, as_json=as_json)
    )
    if exit_code == EXIT_UNKNOWN:
        log_error(output)
    else:
        print(output)
    return exit_code


# ── Degraded-completion audit (one-shot, read-only aggregate) ──────────────


async def _audit_degraded(db: Any) -> dict[str, Any]:
    """Scan terminal-success play/flow records; count degraded per
    _detect_degraded. See docs/internals/cli.md for a known coverage gap."""
    sessions = await db.fetch_all(
        "SELECT * FROM sessions WHERE invocation_kind IN ('play', 'flow') AND status = 'completed'"
    )
    sessions_degraded = 0
    for raw in sessions:
        row = db._row_to_dict(raw)
        degraded, _ = _detect_degraded(
            entity_type="session", status=row.get("status") or "", primary_session=row
        )
        if degraded:
            sessions_degraded += 1

    plays = await db.fetch_all("SELECT * FROM plays WHERE status = 'merged'")
    plays_degraded = 0
    for raw in plays:
        row = db._row_to_dict(raw)
        sid = row.get("session_id")
        backing = await db.get_session(sid) if sid else None
        degraded, _ = _detect_degraded(
            entity_type="play", status=row.get("status") or "", primary_session=backing
        )
        if degraded:
            plays_degraded += 1

    return {
        "sessions_scanned": len(sessions),
        "sessions_degraded": sessions_degraded,
        "plays_scanned": len(plays),
        "plays_degraded": plays_degraded,
        "total_degraded": sessions_degraded + plays_degraded,
    }


def _dispatch_audit(*, as_json: bool) -> int:
    from lionagi.ln.concurrency import run_async
    from lionagi.state.db import DEFAULT_DB_PATH, StateDB

    if not DEFAULT_DB_PATH.exists():
        log_error("state.db not found — no runs recorded yet")
        return EXIT_UNKNOWN

    async def _go() -> dict[str, Any]:
        try:
            async with StateDB() as db:
                return await _audit_degraded(db)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}

    try:
        result = run_async(asyncio.wait_for(_go(), timeout=_DB_BUSY_TIMEOUT_S))
    except (TimeoutError, asyncio.TimeoutError):  # 3.10 support: not aliased until 3.11
        log_error(f"state.db busy (no read within {_DB_BUSY_TIMEOUT_S:.0f}s) — try again")
        return EXIT_UNKNOWN
    if "error" in result:
        log_error(result["error"])
        return EXIT_UNKNOWN

    if as_json:
        print(json.dumps(result))
    else:
        from .monitor import _bold

        print(_bold("degraded-completion audit (trust-debt baseline)"))
        print(f"  sessions: {result['sessions_degraded']} / {result['sessions_scanned']} scanned")
        print(f"  plays:    {result['plays_degraded']} / {result['plays_scanned']} scanned")
        print(f"  total:    {result['total_degraded']}")
        if result["sessions_scanned"] and result["sessions_degraded"] == result["sessions_scanned"]:
            print(
                "  note: 100% of scanned sessions flagged — this matches a known "
                "usage-metric persistence gap for orchestrator/play/flow sessions "
                "(num_turns is never recorded for that invocation class), not "
                "necessarily real degradation. See _audit_degraded docstring."
            )
    return 0


# ── CLI entry points ────────────────────────────────────────────────────────


def run_agent_status(argv: list[str]) -> int:
    """Entry point for `li agent status [<id>] [--json]`."""
    parser = argparse.ArgumentParser(prog="li agent status", add_help=True)
    parser.add_argument(
        "id", nargs="?", default=None, help="Session or invocation ID (or short prefix)."
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="Emit a stable JSON object."
    )
    args = parser.parse_args(argv)
    return _dispatch("agent", args.id, args.as_json)


def run_play_status(argv: list[str]) -> int:
    """Entry point for `li play status [<id>] [--json] [--audit-degraded]`."""
    parser = argparse.ArgumentParser(prog="li play status", add_help=True)
    parser.add_argument(
        "id", nargs="?", default=None, help="Session, invocation, or play ID (or short prefix)."
    )
    parser.add_argument(
        "--json", action="store_true", dest="as_json", help="Emit a stable JSON object."
    )
    parser.add_argument(
        "--audit-degraded",
        action="store_true",
        dest="audit_degraded",
        help=(
            "One-shot scan: count terminal-success play/flow records with no "
            "recorded run-usage metrics (a proxy for orphaned/forced completions). "
            "Ignores <id>."
        ),
    )
    args = parser.parse_args(argv)
    if args.audit_degraded:
        return _dispatch_audit(as_json=args.as_json)
    return _dispatch("play", args.id, args.as_json)


def run_ctl_status(args: argparse.Namespace) -> int:
    """`li o ctl status <id> [--json]` — generic alias, with no kind scoping."""
    return _dispatch("ctl", args.id, args.as_json)
