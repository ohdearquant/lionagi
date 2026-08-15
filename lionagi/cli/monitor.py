# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li monitor` — observe play/agent/run progress in real-time."""

from __future__ import annotations

import argparse
import json
import math
import re
import signal
import sys
import time
from functools import cache
from pathlib import Path
from typing import Any

from lionagi._auto import CliDeclaration, auto_register
from lionagi.state.engine import mask_credentials

from ._project import detect_project
from ._runs import RUNS_ROOT
from ._util import AmbiguousIdError, fetch_unique_row, resolve_entity
from ._util import pid_alive as _pid_alive_int

__all__ = (
    "add_monitor_subparser",
    "run_monitor",
    "run_monitor_wait",
)


def _pid_alive(pid: int | None) -> bool | None:
    if pid is None:
        return None
    return _pid_alive_int(pid)


# ── ANSI colours (only when stdout is a TTY) ─────────────────────────────────

_IS_TTY = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    """Wrap text in an ANSI colour code, but only on a TTY."""
    if not _IS_TTY:
        return text
    return f"\033[{code}m{text}\033[0m"


def _green(t: str) -> str:
    return _c(t, "32")


def _yellow(t: str) -> str:
    return _c(t, "33")


def _red(t: str) -> str:
    return _c(t, "31")


def _dim(t: str) -> str:
    return _c(t, "2")


def _bold(t: str) -> str:
    return _c(t, "1")


_STATUS_COLOUR = {
    "running": _green,
    "active": _green,
    "completed": _dim,
    "completed_empty": _yellow,
    "merged": _dim,
    "failed": _red,
    "aborted": _red,
    "gate_failed": _red,
    "cancelled": _red,
    "timed_out": _red,
    "pending": _yellow,
    "prepared": _yellow,
    "gated": _yellow,
    "redoing": _yellow,
    "running_complete": _yellow,
    "escalated": _yellow,
    "blocked": _yellow,
}


def _colour_status(status: str) -> str:
    fn = _STATUS_COLOUR.get(status, lambda t: t)
    return fn(status)


# ── Elapsed formatting ────────────────────────────────────────────────────────


def _elapsed(
    started_at: float | None,
    ended_at: float | None = None,
    *,
    approximate: bool = False,
    terminal: bool = False,
) -> str:
    """Human-readable elapsed time.  Uses ended_at if present, else now.

    A leading ``~`` marks a span whose end was reconstructed rather than
    observed. Rows that ended before the store recorded end times get an end
    inferred from whatever evidence was left behind, and the difference
    between that and a measured run is the whole reason the store keeps the
    provenance: printed bare, a guess reads as a measurement.
    """
    if started_at is None:
        return "-"
    # A row that has finished and recorded no end has no span to report, and
    # saying so is the whole answer. Measuring it up to now is what the branch
    # below does for a running row, and doing that here prints a duration that
    # grows every redraw for a session that stopped long ago -- the reading the
    # provenance flag exists to prevent, arrived at by a different route.
    if ended_at is None and terminal:
        return "-"
    # Only a closed span can be approximate. A row that is still running is
    # measured up to now whatever the flag says about the end it does not have.
    marker = "~" if (approximate and ended_at is not None) else ""
    end = ended_at if ended_at is not None else time.time()
    secs = int(end - started_at)
    if secs < 0:
        return f"{marker}0s"
    if secs < 60:
        return f"{marker}{secs}s"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{marker}{mins}m{secs:02d}s"
    hrs, mins = divmod(mins, 60)
    return f"{marker}{hrs}h{mins:02d}m"


def _since_timestamp(window: str) -> float:
    """Parse a window string like '1h', '30m', '2d' into a cutoff epoch float."""
    unit = window[-1].lower()
    try:
        value = int(window[:-1])
    except ValueError as exc:
        raise ValueError(
            f"Invalid --since value {window!r}; expected format like 1h, 30m, 2d"
        ) from exc
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if unit not in multipliers:
        raise ValueError(f"Unknown time unit {unit!r} in --since {window!r}")
    return time.time() - value * multipliers[unit]


# ── DB query helpers ─────────────────────────────────────────────────────────


async def _query_running_sessions(
    db: Any,
    *,
    since: float | None = None,
    project: str | None = None,
    invocation_kind: str | None = None,
) -> list[dict[str, Any]]:
    """Running only by default; with since, all statuses in the window."""
    query = (
        "SELECT sessions.*, "
        "(SELECT COUNT(*) FROM branches WHERE session_id = sessions.id) AS branch_count "
        "FROM sessions WHERE 1=1"  # noqa: S608
    )
    params: list[Any] = []
    if since is not None:
        query += " AND updated_at >= ?"
        params.append(since)
    else:
        query += " AND status = 'running'"
    if project:
        query += " AND project = ?"
        params.append(project)
    if invocation_kind is not None:
        query += " AND invocation_kind = ?"
        params.append(invocation_kind)
    query += " ORDER BY started_at DESC"
    rows = await db.fetch_all(query, params)
    return rows


async def _query_running_invocations(
    db: Any,
    *,
    since: float | None = None,
) -> list[dict[str, Any]]:
    """Running only by default; with since, all statuses in the window."""
    query = "SELECT * FROM invocations WHERE 1=1"  # noqa: S608
    params: list[Any] = []
    if since is not None:
        query += " AND updated_at >= ?"
        params.append(since)
    else:
        query += " AND status = 'running'"
    query += " ORDER BY started_at DESC"
    rows = await db.fetch_all(query, params)
    return rows


async def _query_active_shows(
    db: Any,
    *,
    since: float | None = None,
) -> list[dict[str, Any]]:
    """Active only by default; with since, all statuses in the window.
    `repo` is a path, not a project slug; scoped in Python via _show_project_matches."""
    query = "SELECT * FROM shows WHERE 1=1"  # noqa: S608
    params: list[Any] = []
    if since is not None:
        query += " AND updated_at >= ?"
        params.append(since)
    else:
        query += " AND status = 'active'"
    query += " ORDER BY updated_at DESC"
    rows = await db.fetch_all(query, params)
    return rows


async def _query_running_plays(
    db: Any,
    *,
    since: float | None = None,
    project: str | None = None,
) -> list[dict[str, Any]]:
    """In-flight statuses only by default; with since, all statuses in the window.
    Plays have no project column; scoping joins through the linked session."""
    query = (
        "SELECT plays.*, "  # noqa: S608
        "(SELECT COUNT(*) FROM branches WHERE session_id = plays.session_id) AS branch_count, "
        "(SELECT project FROM sessions WHERE id = plays.session_id) AS session_project "
        "FROM plays WHERE 1=1"
    )
    params: list[Any] = []
    if since is not None:
        query += " AND updated_at >= ?"
        params.append(since)
    else:
        # `gated` is deliberately absent: a play parked on a gate decision is
        # stopped until a human decides, so it must not sit on the live view
        # indefinitely. Still reachable via --since, `li monitor <id>`, or show detail.
        running_statuses = ("running", "running_complete", "redoing", "prepared")
        placeholders = ",".join("?" * len(running_statuses))
        query += f" AND status IN ({placeholders})"
        params.extend(running_statuses)
    if project:
        query += " AND plays.session_id IN (SELECT id FROM sessions WHERE project = ?)"
        params.append(project)
    query += " ORDER BY updated_at DESC"
    rows = await db.fetch_all(query, params)
    return rows


async def _query_plays_for_show(db: Any, show_id: str) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT * FROM plays WHERE show_id = ? ORDER BY sort_order, created_at",
        (show_id,),
    )
    return rows


async def _find_entity(db: Any, entity_id: str) -> tuple[str, dict[str, Any]] | None:
    """Resolve entity_id across all entity tables; returns (entity_type, row) or None.

    Raises `AmbiguousIdError` when a short prefix matches more than one row,
    whether the rows are of one kind or several, so a detail view can never be
    rendered for an arbitrarily chosen run. Kinds are searched together for
    that reason: taking the first kind that matches would answer a collision
    across kinds by search order, which says where to look first and nothing
    about which record the caller meant.
    """
    hit = await resolve_entity(db, entity_id, tables=("sessions", "invocations", "shows", "plays"))
    if hit is None:
        return None
    _table, entity_type, row = hit
    return entity_type, row


def _stream_tail(run_dir: Path, branch_id: str, n_lines: int = 5) -> list[str]:
    """Return the last N lines from a stream buffer file."""
    buf_path = run_dir / "stream" / f"{branch_id}.buffer.jsonl"
    if not buf_path.exists():
        return []
    try:
        import json as _json

        lines = buf_path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-n_lines:]
        texts: list[str] = []
        for line in tail:
            try:
                obj = _json.loads(line)
                # Claude stream chunks carry 'delta.text' or 'text'
                text = (obj.get("delta") or {}).get("text") or obj.get("text") or ""
                if text:
                    texts.append(text[:120])
            except (ValueError, AttributeError):
                texts.append(line[:120])
        return texts
    except OSError:
        return []


# ── Table rendering ───────────────────────────────────────────────────────────


def _trunc(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _stdout_is_tty() -> bool:
    """Evaluated at call time, not cached, so it reflects stdout redirection after import."""
    return sys.stdout.isatty()


# Ceiling on a non-TTY column's *layout* width — caps padding, not the
# value itself (a pathological value still prints in full, unaligned).
_NON_TTY_MAX_COL_WIDTH = 200


def _format_table(rows: list[dict[str, Any]]) -> str:
    """Render entity rows as a table. TTY truncates columns; piped output
    never truncates (grep-safe)."""
    if not rows:
        return _dim("(no running entities)")

    # Column widths — uppercase name is intentional (table layout constant)
    col_min = {  # noqa: N806
        "id": 16,
        "type": 11,
        "project": 14,
        "status": 15,
        "phase": 18,
        "elapsed": 9,
        "agents": 7,
    }
    headers = {  # noqa: N806
        "id": "ID",
        "type": "TYPE",
        "project": "PROJECT",
        "status": "STATUS",
        "phase": "PHASE",
        "elapsed": "ELAPSED",
        "agents": "AGENTS",
    }

    raw_rows = [
        {
            "id": str(row.get("id", "")),
            "type": str(row.get("type", "")),
            "project": str(row.get("project", "-")),
            "status": row.get("status", "") or "",
            "phase": str(row.get("phase", "-")),
            "elapsed": str(row.get("elapsed", "-")),
            "agents": str(row.get("agents", "-")),
        }
        for row in rows
    ]

    if _stdout_is_tty():
        col = dict(col_min)  # noqa: N806

        def _field(key: str, value: str) -> str:
            return _trunc(value, col[key])
    else:
        col = {  # noqa: N806
            key: min(
                max(width, len(headers[key]), max((len(r[key]) for r in raw_rows), default=0)),
                _NON_TTY_MAX_COL_WIDTH,
            )
            for key, width in col_min.items()
        }

        def _field(key: str, value: str) -> str:
            # Layout width is capped above; the value itself is never clipped.
            return value

    header_parts = [
        _bold(f"{headers['id']:<{col['id']}}"),
        _bold(f"{headers['type']:<{col['type']}}"),
        _bold(f"{headers['project']:<{col['project']}}"),
        _bold(f"{headers['status']:<{col['status']}}"),
        _bold(f"{headers['phase']:<{col['phase']}}"),
        _bold(f"{headers['elapsed']:>{col['elapsed']}}"),
        _bold(f"{headers['agents']:>{col['agents']}}"),
    ]
    header = "  ".join(header_parts)
    separator = _dim("-" * (sum(col.values()) + 2 * (len(col) - 1)))

    lines = [header, separator]
    for raw in raw_rows:
        eid = _field("id", raw["id"])
        etype = _field("type", raw["type"])
        eproject = _field("project", raw["project"])
        estatus = raw["status"]
        ephase = _field("phase", raw["phase"])
        eelapsed = _field("elapsed", raw["elapsed"])
        eagents = raw["agents"]

        coloured_status = _colour_status(estatus)
        # Pad status accounting for invisible ANSI codes
        pad = col["status"] - len(estatus)
        padded_status = coloured_status + " " * max(0, pad)

        line = "  ".join(
            [
                f"{eid:<{col['id']}}",
                f"{etype:<{col['type']}}",
                f"{eproject:<{col['project']}}",
                padded_status,
                f"{ephase:<{col['phase']}}",
                f"{eelapsed:>{col['elapsed']}}",
                f"{eagents:>{col['agents']}}",
            ]
        )
        lines.append(line)

    return "\n".join(lines)


# ── Entity row builders ───────────────────────────────────────────────────────


def _session_to_row(sess: dict[str, Any]) -> dict[str, Any]:
    from lionagi.state.db import SESSION_TERMINAL_STATUSES

    return {
        "id": sess["id"][:16],
        "type": sess.get("invocation_kind") or "session",
        "project": sess.get("project") or "-",
        "status": sess.get("status") or "?",
        # live flow phase (executing/synthesizing) wins over the static
        # orchestrator/playbook name once a flow leaves planning.
        "phase": (
            sess.get("current_phase") or sess.get("agent_name") or sess.get("playbook_name") or "-"
        ),
        "elapsed": _elapsed(
            sess.get("started_at"),
            sess.get("ended_at"),
            approximate=bool(sess.get("ended_at_is_approximate")),
            terminal=sess.get("status") in SESSION_TERMINAL_STATUSES,
        ),
        "agents": str(sess.get("branch_count") or 0),
    }


def _invocation_to_row(inv: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": inv["id"][:16],
        "type": "invocation",
        "project": "-",
        "status": inv.get("status") or "?",
        "phase": inv.get("skill") or "-",
        "elapsed": _elapsed(inv.get("started_at"), inv.get("ended_at")),
        "agents": str(inv.get("session_count") or 0),
    }


def _show_to_row(show: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": show["id"][:16],
        "type": "show",
        "project": show.get("repo") or "-",
        "status": show.get("status") or "?",
        "phase": _trunc(show.get("topic") or "-", 18),
        "elapsed": "-",
        "agents": "-",
    }


def _play_to_row(play: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": play["id"][:16],
        "type": "play",
        "project": play.get("session_project") or "-",
        "status": play.get("status") or "?",
        "phase": _trunc(play.get("name") or "-", 18),
        "elapsed": _elapsed(play.get("started_at"), play.get("ended_at")),
        "agents": str(play.get("branch_count") or 0),
    }


# ── Detail views ──────────────────────────────────────────────────────────────


async def _fetch_branches(db: Any, session_id: str) -> list[dict[str, Any]]:
    """Every branch (agent leg) on a session, oldest-started first."""
    return await db.fetch_all(
        "SELECT name, status, started_at, ended_at FROM branches "
        "WHERE session_id = ? ORDER BY started_at",
        (session_id,),
    )


def _render_branch_lines(rows: list[dict[str, Any]], *, indent: str = "  ") -> list[str]:
    """One line per branch (agent leg), named after its role (e.g. "reviewer")."""
    if not rows:
        return []
    lines = [_dim(f"{indent}-- branches --")]
    for r in rows:
        bname = r.get("name") or "-"
        bstatus = _colour_status(r.get("status") or "?")
        belapsed = _elapsed(r.get("started_at"), r.get("ended_at"))
        lines.append(f"{indent}  {bname:<20}  {bstatus}  {belapsed}")
    return lines


async def _detail_session(db: Any, sess: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(_bold(f"SESSION  {sess['id']}"))
    lines.append(f"  status:    {_colour_status(sess.get('status') or '?')}")
    lines.append(f"  kind:      {sess.get('invocation_kind') or '-'}")
    if sess.get("playbook_name"):
        lines.append(f"  playbook:  {sess['playbook_name']}")
    lines.append(f"  project:   {sess.get('project') or '-'}")
    lines.append(f"  model:     {sess.get('model') or '-'}")
    lines.append(f"  provider:  {sess.get('provider') or '-'}")
    lines.append(f"  effort:    {sess.get('effort') or '-'}")
    lines.append(
        "  elapsed:   "
        + _elapsed(
            sess.get("started_at"),
            sess.get("ended_at"),
            approximate=bool(sess.get("ended_at_is_approximate")),
        )
    )
    started = sess.get("started_at")
    if started:
        lines.append(f"  started:   {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started))}")
    last_msg = sess.get("last_message_at")
    if last_msg:
        lines.append(f"  last_msg:  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_msg))}")

    node_metadata = _parse_json_field(sess.get("node_metadata"))
    injection = (node_metadata or {}).get("khive_injection")
    if isinstance(injection, dict):
        injection_line = _format_khive_injection_line(injection)
        if injection_line:
            lines.append("")
            lines.append(_dim("  -- khive injection --"))
            lines.append(f"    {injection_line}")

    # Completion-trust evidence: surface why the status landed, so trusting
    # it doesn't require a manual git read.
    if sess.get("status") in ("completed", "completed_empty"):
        reason_summary = sess.get("status_reason_summary")
        if reason_summary:
            lines.append(f"  reason:    {reason_summary}")
        evidence_refs = sess.get("status_evidence_refs")
        if isinstance(evidence_refs, str):
            import json as _json_ev

            try:
                evidence_refs = _json_ev.loads(evidence_refs)
            except (ValueError, TypeError):
                evidence_refs = None
        if evidence_refs:
            lines.append(_dim("  -- evidence --"))
            for ev in evidence_refs:
                if isinstance(ev, dict):
                    ev_label = ev.get("label") or ev.get("kind") or "evidence"
                    lines.append(f"    {ev_label}")
                else:
                    lines.append(f"    {ev}")

    branch_rows = await _fetch_branches(db, sess["id"])
    lines.append(f"  branches:  {len(branch_rows)}")
    if branch_rows:
        lines.append("")
        lines.extend(_render_branch_lines(branch_rows))

    run_dir = RUNS_ROOT / sess["id"]
    if run_dir.exists():
        branches_dir = run_dir / "branches"
        if branches_dir.exists():
            branch_files = sorted(
                branches_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
            )
            if branch_files:
                branch_id = branch_files[0].stem
                tail = _stream_tail(run_dir, branch_id)
                if tail:
                    lines.append("")
                    lines.append(_dim("  -- output tail --"))
                    for chunk in tail:
                        lines.append(f"  {_dim(chunk)}")

    return "\n".join(lines)


def _parse_json_field(value: Any) -> dict[str, Any] | None:
    """Best-effort JSON-object decode for a column that may come back as a
    dict (Postgres) or a raw string (SQLite)."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _as_number(value: Any) -> int | float:
    """Coerce *value* to a count, treating anything non-numeric as 0 —
    persisted telemetry is untrusted and must never crash the monitor."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _as_counter(value: Any) -> int:
    """Return a finite, nonnegative integral counter.

    Invalid numeric telemetry is treated as absent.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 0 else 0
    if isinstance(value, float) and math.isfinite(value) and value >= 0 and value.is_integer():
        return int(value)
    return 0


def _format_khive_injection_line(telemetry: dict[str, Any]) -> str | None:
    """Render persisted aggregate injection counters.

    Empty or malformed counter sets are omitted from the session detail.
    """
    counter_names = (
        "recall_turns",
        "blocks_injected",
        "failed",
        "writeback_records",
        "writeback_failed",
    )
    counters = {name: _as_counter(telemetry.get(name)) for name in counter_names}
    if not any(counters.values()):
        return None
    return " ".join(f"{name}={value}" for name, value in counters.items())


def _format_coordination_line(telemetry: dict[str, Any]) -> str | None:
    """One-liner rendering of coordination telemetry; returns None when zero.
    See docs/internals/cli.md for the telemetry shape contract."""
    signals = telemetry.get("signals")
    signals = signals if isinstance(signals, dict) else {}
    emitted = signals.get("emitted")
    emitted = emitted if isinstance(emitted, dict) else {}
    emitted_total = sum(_as_number(v) for v in emitted.values())
    received = _as_number(signals.get("received"))
    acted_on = _as_number(signals.get("acted_on"))
    overlap = telemetry.get("files_overlap")
    overlap = overlap if isinstance(overlap, dict) else {}
    overlap_count = _as_number(overlap.get("count"))
    if not (emitted_total or received or acted_on or overlap_count):
        return None
    return (
        f"emitted={emitted_total} received={received} "
        f"acted_on={acted_on} files_overlap={overlap_count}"
    )


async def _detail_invocation(db: Any, inv: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(_bold(f"INVOCATION  {inv['id']}"))
    lines.append(f"  status:        {_colour_status(inv.get('status') or '?')}")
    lines.append(f"  skill:         {inv.get('skill') or '-'}")
    lines.append(f"  plugin:        {inv.get('plugin') or '-'}")
    lines.append(f"  session_count: {inv.get('session_count') or 0}")
    lines.append(f"  elapsed:       {_elapsed(inv.get('started_at'), inv.get('ended_at'))}")
    started = inv.get("started_at")
    if started:
        lines.append(
            f"  started:       {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started))}"
        )

    node_metadata = _parse_json_field(inv.get("node_metadata"))
    coordination = (node_metadata or {}).get("coordination")
    if isinstance(coordination, dict):
        coord_line = _format_coordination_line(coordination)
        if coord_line:
            lines.append("")
            lines.append(_dim("  -- coordination --"))
            lines.append(f"    {coord_line}")
            files_overlap = coordination.get("files_overlap")
            top = files_overlap.get("top") if isinstance(files_overlap, dict) else None
            top = top if isinstance(top, list) else []
            for entry in top:
                if isinstance(entry, dict) and entry.get("path"):
                    lines.append(f"      {entry['path']}  (workers={entry.get('workers', '?')})")

    child_rows = await db.fetch_all(
        "SELECT id, status, model, started_at FROM sessions WHERE invocation_id = ? ORDER BY created_at",
        (inv["id"],),
    )
    if child_rows:
        lines.append("")
        lines.append(_dim("  -- child sessions --"))
        for cr in child_rows:
            cstatus = _colour_status(cr["status"] or "?")
            celapsed = _elapsed(cr["started_at"])
            cmodel = (cr["model"] or "-")[:20]
            lines.append(f"    {cr['id'][:16]}  {cstatus:<20}  {cmodel}  {celapsed}")

    return "\n".join(lines)


async def _detail_show(db: Any, show: dict[str, Any]) -> str:
    from lionagi.state.db import PLAY_ACTIVE_STATUSES, PLAY_TERMINAL_STATUSES

    lines: list[str] = []
    lines.append(_bold(f"SHOW  {show['id']}"))
    lines.append(f"  topic:   {show.get('topic') or '-'}")
    lines.append(f"  status:  {_colour_status(show.get('status') or '?')}")
    lines.append(f"  repo:    {show.get('repo') or '-'}")
    lines.append(f"  branch:  {show.get('base_branch') or '-'}")
    lines.append(f"  goal:    {(show.get('goal') or '-')[:80]}")

    plays = await _query_plays_for_show(db, show["id"])
    if plays:
        lines.append("")
        lines.append(_dim("  -- plays --"))
        # Read from the lifecycle vocabulary, not restated as a literal here,
        # so a new terminal status doesn't silently stay [wait] in this view.
        terminal = PLAY_TERMINAL_STATUSES
        # Executing right now, which is narrower than "not finished". `gated`
        # and `pending` fall through to [wait] because waiting is what they are
        # doing. This set is also narrower than the default listing's in-flight
        # filter, which counts `prepared`: there the question is whether a run
        # is under way at all, here it is whether this play is the one moving.
        live = {"running", "running_complete", "redoing"}
        for play in plays:
            pstatus = play.get("status") or "?"
            if pstatus in terminal:
                marker = _dim("  [done]  ")
            elif pstatus in live:
                marker = _green("  [live]  ")
            elif pstatus in PLAY_ACTIVE_STATUSES:
                marker = _yellow("  [wait]  ")
            else:
                # An unrecognized status: not [wait] (unsupported claim it will
                # continue) and not [done] (would sweep live work as finished).
                # Its own marker flags a data problem instead of guessing.
                marker = _red("  [????]  ")
            pelapsed = _elapsed(play.get("started_at"), play.get("ended_at"))
            pname = _trunc(play.get("name") or play["id"][:12], 24)
            pstatus_col = _colour_status(pstatus)
            lines.append(f"{marker}{pname:<24}  {pstatus_col}  {pelapsed}")

    return "\n".join(lines)


async def _detail_play(db: Any, play: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(_bold(f"PLAY  {play['id']}"))
    lines.append(f"  name:     {play.get('name') or '-'}")
    lines.append(f"  status:   {_colour_status(play.get('status') or '?')}")
    lines.append(f"  playbook: {play.get('playbook') or '-'}")
    lines.append(f"  effort:   {play.get('effort') or '-'}")
    lines.append(f"  attempt:  {play.get('attempt') or 1}")
    lines.append(f"  elapsed:  {_elapsed(play.get('started_at'), play.get('ended_at'))}")
    started = play.get("started_at")
    if started:
        lines.append(f"  started:  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started))}")

    gp = play.get("gate_passed")
    if gp is not None:
        gate_str = _green("PASS") if gp else _red("FAIL")
        lines.append(f"  gate:     {gate_str}")
        feedback = play.get("gate_feedback")
        if feedback:
            lines.append(f"  feedback: {_trunc(str(feedback), 80)}")

    session_id = play.get("session_id")
    if session_id:
        srow = await db.fetch_one(
            "SELECT status, model, provider, effort FROM sessions WHERE id = ?",
            (session_id,),
        )
        if srow:
            lines.append("")
            lines.append(_dim("  -- linked session --"))
            lines.append(f"    id:       {session_id[:24]}")
            lines.append(f"    status:   {_colour_status(srow['status'] or '?')}")
            lines.append(f"    model:    {srow['model'] or '-'}")
            lines.append(f"    provider: {srow['provider'] or '-'}")
            lines.append(f"    effort:   {srow['effort'] or '-'}")

            branch_rows = await _fetch_branches(db, session_id)
            if branch_rows:
                lines.append("")
                lines.extend(_render_branch_lines(branch_rows, indent="    "))

            run_dir = RUNS_ROOT / session_id
            if run_dir.exists():
                branches_dir = run_dir / "branches"
                if branches_dir.exists():
                    branch_files = sorted(
                        branches_dir.glob("*.json"),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                    if branch_files:
                        branch_id = branch_files[0].stem
                        tail = _stream_tail(run_dir, branch_id)
                        if tail:
                            lines.append("")
                            lines.append(_dim("    -- output tail --"))
                            for chunk in tail:
                                lines.append(f"    {_dim(chunk)}")

    return "\n".join(lines)


# ── Gather all running entities ───────────────────────────────────────────────


_TRAILING_ANNOTATION_RE = re.compile(r"\s*\([^)]*\)\s*$")


@cache
def _cached_detect_project(repo: str) -> tuple[str | None, str | None]:
    """Cache detect_project() results by bare repo path (stable per monitor process)."""
    return detect_project(Path(repo))


def _show_project_matches(show: dict[str, Any], project: str) -> bool:
    """Derive the show's project slug via detect_project() and compare;
    a missing/unresolvable repo path excludes the show under --project."""
    repo = show.get("repo")
    if not repo:
        return False
    # Strip a trailing "(remote, ...)" annotation some _show.md authors
    # append after the path; anchored to end-of-string only.
    repo = _TRAILING_ANNOTATION_RE.sub("", repo)
    if not repo:
        return False
    try:
        derived, _source = _cached_detect_project(repo)
    except Exception:  # noqa: BLE001
        return False
    return derived == project


async def _gather_entities(
    db: Any,
    *,
    since: float | None,
    entity_type: str | None,
    project: str | None,
) -> list[tuple[str, dict[str, Any]]]:
    """Every in-flight row the views draw from, each paired with its kind.

    Selection lives here and rendering lives in the callers, so the table and
    the machine result answer with the same set of entities rather than with two
    independently maintained ideas of what is running.
    """
    rows: list[tuple[str, dict[str, Any]]] = []

    sessions: list[dict[str, Any]] = []
    if entity_type in (None, "session", "agent", "play_session", "play"):
        # For specific type filters, restrict to sessions whose invocation_kind matches.
        # "session" (no filter) and None (all) show every running session.
        inv_kind = entity_type if entity_type not in (None, "session") else None
        sessions = await _query_running_sessions(
            db, since=since, project=project, invocation_kind=inv_kind
        )

    plays: list[dict[str, Any]] = []
    if entity_type in (None, "play"):
        plays = await _query_running_plays(db, since=since, project=project)
        # A play row is the canonical rendering of its backing session, so
        # drop that session only when the play row itself is being shown.
        play_session_ids = {p["session_id"] for p in plays if p.get("session_id")}
        sessions = [s for s in sessions if s["id"] not in play_session_ids]

    rows.extend(("session", s) for s in sessions)

    if entity_type in (None, "invocation"):
        invocations = await _query_running_invocations(db, since=since)
        rows.extend(("invocation", i) for i in invocations)

    if entity_type in (None, "show"):
        shows = await _query_active_shows(db, since=since)
        if project:
            shows = [s for s in shows if _show_project_matches(s, project)]
        rows.extend(("show", s) for s in shows)

    rows.extend(("play", p) for p in plays)

    return rows


_ROW_BUILDERS = {
    "session": _session_to_row,
    "invocation": _invocation_to_row,
    "show": _show_to_row,
    "play": _play_to_row,
}


async def _gather_table_rows(
    db: Any,
    *,
    since: float | None,
    entity_type: str | None,
    project: str | None,
) -> list[dict[str, Any]]:
    """Collect entity rows across all tables and convert to table-row dicts."""
    entities = await _gather_entities(db, since=since, entity_type=entity_type, project=project)
    return [_ROW_BUILDERS[kind](row) for kind, row in entities]


# ── Async main routines ───────────────────────────────────────────────────────


async def _run_table(
    *,
    since: float | None,
    entity_type: str | None,
    project: str | None,
) -> str:
    try:
        from lionagi.state.db import StateDB, state_db_known_absent

        if state_db_known_absent():
            return _dim("(no state.db — run `li agent` at least once)")
        async with StateDB() as db:
            rows = await _gather_table_rows(
                db, since=since, entity_type=entity_type, project=project
            )
        return _format_table(rows)
    except Exception as exc:  # noqa: BLE001
        # A catch-all around a store open, so the message can be one that names
        # the store. Masked here for the same reason the envelope sinks are.
        return _red(f"error reading state.db: {mask_credentials(str(exc))}")


async def _run_detail(entity_id: str) -> str:
    try:
        from lionagi.state.db import StateDB, state_db_known_absent

        if state_db_known_absent():
            return _red(f"state.db not found — cannot look up {entity_id!r}")
        async with StateDB() as db:
            result = await _find_entity(db, entity_id)
            if result is None:
                return _red(f"entity {entity_id!r} not found in state.db")
            entity_type, entity_row = result
            if entity_type == "session":
                return await _detail_session(db, entity_row)
            if entity_type == "invocation":
                return await _detail_invocation(db, entity_row)
            if entity_type == "show":
                return await _detail_show(db, entity_row)
            if entity_type == "play":
                return await _detail_play(db, entity_row)
            return _red(f"unknown entity type {entity_type!r}")
    except AmbiguousIdError:
        # Propagate: an ambiguous id is a user-input error the caller has to
        # turn into a non-success exit code, not a rendered detail body.
        raise
    except Exception as exc:  # noqa: BLE001
        return _red(f"error: {mask_credentials(str(exc))}")


# ── Watch loop ────────────────────────────────────────────────────────────────


def _clear_screen() -> None:
    if _IS_TTY:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()


def _watch_loop(
    refresh_seconds: int,
    entity_id: str | None,
    *,
    since_window: str | None,
    entity_type: str | None,
    project: str | None,
) -> int:
    """Repeatedly clear screen and reprint; exit cleanly on SIGINT/SIGTERM."""
    from lionagi.ln.concurrency import SigtermInterrupt, run_async

    from ._logging import log_error
    from .status import EXIT_UNKNOWN

    interrupted = False

    def _handle_signal(signum: int, frame: Any) -> None:
        nonlocal interrupted
        interrupted = True

    def _install(signum):
        """Take over *signum* only if the handler can be given back.

        A handler installed outside Python is reported as None and cannot be
        reinstalled, so replacing one means keeping it forever. Leaving it
        alone costs this loop its clean exit on that signal; taking it would
        cost the whole process its handler, which is the larger harm.
        """
        prior = signal.getsignal(signum)
        if prior is None:
            return None
        signal.signal(signum, _handle_signal)
        return prior

    prior_sigint = _install(signal.SIGINT)
    prior_sigterm = _install(signal.SIGTERM)
    exit_code = 0

    try:
        while not interrupted:
            since = _since_timestamp(since_window) if since_window else None
            try:
                if entity_id:
                    output = run_async(_run_detail(entity_id))
                else:
                    output = run_async(
                        _run_table(since=since, entity_type=entity_type, project=project)
                    )
            except (KeyboardInterrupt, SigtermInterrupt):
                # run_async installs its own signal handlers for the duration
                # of the refresh, so a signal arriving inside that window
                # surfaces as an exception here instead of setting the flag
                # above. Both derive from BaseException, so they must be named.
                interrupted = True
                break
            except AmbiguousIdError as exc:
                # A longer prefix is the only fix; refreshing cannot resolve it.
                log_error(str(exc))
                exit_code = EXIT_UNKNOWN
                break
            _clear_screen()
            ts = time.strftime("%H:%M:%S")
            print(f"{_dim(f'Updated: {ts}  (refresh every {refresh_seconds}s, Ctrl-C to exit)')}")
            print()
            print(output)
            # Sleep in small increments so SIGINT is responsive
            for _ in range(refresh_seconds * 10):
                if interrupted:
                    break
                time.sleep(0.1)
    finally:
        # The loop's handlers must not outlive the loop. None here means the
        # signal was never taken over, so there is nothing to give back.
        if prior_sigint is not None:
            signal.signal(signal.SIGINT, prior_sigint)
        if prior_sigterm is not None:
            signal.signal(signal.SIGTERM, prior_sigterm)

    if _IS_TTY:
        print()  # newline after Ctrl-C
    return exit_code


# ── Wait-for-terminal primitive (li monitor run / li monitor --run) ───────────
# Scripting primitive; see docs/internals/cli.md for the full contract.

# Default wait bound so a stuck run can't hang forever; pass max_wait=0 to opt
# into unbounded waiting explicitly.
_DEFAULT_MAX_WAIT_SECONDS = 900.0
_CHAIN_GRACE_TICKS = 2
# Mirrors the scheduler engine's own chain-depth cap (engine.py _MAX_CHAIN_DEPTH).
_MAX_CHAIN_DEPTH = 10


def _split_watched_ids(raw: list[str]) -> list[str]:
    """Flatten comma/space-separated id tokens into one ordered, deduped list."""
    seen: dict[str, None] = {}
    for token in raw:
        for piece in token.split(","):
            piece = piece.strip()
            if piece:
                seen.setdefault(piece, None)
    return list(seen)


async def _resolve_schedule_run(db: Any, raw_id: str) -> dict[str, Any] | None:
    """Exact match then unique-prefix match. schedule_run ids are 12-char hex,
    not 36-char UUIDs, so any id length can be a prefix here."""
    row = await db.get_schedule_run(raw_id)
    if row:
        return row
    return await fetch_unique_row(db, "schedule_runs", raw_id)


async def _resolve_session_run(db: Any, raw_id: str) -> dict[str, Any] | None:
    """Exact match then unique-prefix match against the sessions table
    (agent/li agent run ids)."""
    row = await db.get_session(raw_id)
    if row:
        return row
    return await fetch_unique_row(db, "sessions", raw_id)


def _format_session_wait_line(row: dict[str, Any]) -> str:
    name = row.get("agent_name") or row.get("invocation_kind") or "session"
    artifacts = row.get("artifacts_path") or "-"
    return f"{row['id']}  name={name}  status={row['status']}  artifacts={artifacts}"


async def _effective_session_status(db: Any, row: dict[str, Any]) -> dict[str, Any]:
    """Reconcile a profile session's status against its linked engine session.
    See docs/internals/cli.md for the CAS/terminal-guard contract (ADR-0035)."""
    from lionagi.state.db import SESSION_TERMINAL_STATUSES, TransitionRejectedError
    from lionagi.state.reasons import RunReasons

    if row["status"] in SESSION_TERMINAL_STATUSES:
        return row

    node_metadata = row.get("node_metadata") or {}
    if isinstance(node_metadata, str):
        node_metadata = json.loads(node_metadata)
    linked_id = node_metadata.get("linked_engine_session_id")
    if not linked_id:
        return row
    linked = await db.get_session(linked_id)
    if linked is None or linked["status"] == row["status"]:
        return row
    if linked["status"] in SESSION_TERMINAL_STATUSES:
        reason_by_status = {
            "completed": RunReasons.COMPLETED_OK,
            "completed_empty": RunReasons.COMPLETED_EMPTY_NO_EVIDENCE,
            "failed": RunReasons.FAILED_EXCEPTION,
            "timed_out": RunReasons.TIMED_OUT_DEADLINE,
            "aborted": RunReasons.CANCELLED_SIGINT,
            "cancelled": RunReasons.CANCELLED_SYSTEM,
        }
        try:
            updated = await db.update_status(
                "session",
                row["id"],
                new_status=linked["status"],
                reason_code=reason_by_status.get(linked["status"], RunReasons.FAILED_EXCEPTION),
                reason_summary=(
                    f"reconciled to linked engine session {linked_id} terminal status "
                    f"{linked['status']!r}"
                ),
                evidence_refs=[{"kind": "session", "id": linked_id, "label": linked["status"]}],
                source="executor",
                actor=row["id"],
                expected_statuses={row["status"]},
            )
        except TransitionRejectedError:
            # Raced with another writer that took the row terminal first —
            # the persisted row is authoritative, not the synthesized status.
            persisted = await db.get_session(row["id"])
            return persisted if persisted is not None else row
        if not updated:
            # CAS mismatch: the write never landed, so the persisted row —
            # not the synthesized value below — is authoritative.
            persisted = await db.get_session(row["id"])
            return persisted if persisted is not None else row
    return {**row, "status": linked["status"]}


async def _poll_pending_sessions_once(
    db: Any,
    pending: dict[str, dict[str, Any]],
    done: list[dict[str, Any]],
) -> None:
    """Session analogue of _poll_pending_once: no schedule chain, just terminal-status polling."""
    from lionagi.state.db import SESSION_TERMINAL_STATUSES

    for run_id in list(pending):
        row = await db.get_session(run_id)
        if row is None:
            row = {**pending[run_id], "status": "failed"}
            from ._logging import log_error

            log_error(f"session {run_id!r} disappeared from state.db while waiting")
        else:
            row = await _effective_session_status(db, row)
            if row["status"] not in SESSION_TERMINAL_STATUSES:
                continue
        print(_format_session_wait_line(row))
        done.append(row)
        del pending[run_id]


# fire_now() hands the run_id back to its HTTP/CLI caller before the fired
# occurrence row is durably written (_fire runs as a background task) -- an
# id resolved immediately after a manual trigger can race that insert. Retry
# unresolved ids within this bounded grace period instead of giving up on a
# single up-front lookup.
_RESOLVE_GRACE_SECONDS = 1.0
_RESOLVE_GRACE_POLL_SECONDS = 0.2


async def _resolve_watched_runs(
    db: Any, ids: list[str], *, grace_seconds: float | None = None
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
    """Resolve every requested id, retrying not-yet-found ones for a bounded
    grace period. Ids that aren't schedule_runs are also tried against
    sessions (`li monitor run <session_id>` support)."""
    import asyncio

    if grace_seconds is None:
        grace_seconds = _RESOLVE_GRACE_SECONDS
    pending: dict[str, dict[str, Any]] = {}
    session_pending: dict[str, dict[str, Any]] = {}
    remaining = list(ids)
    deadline = time.monotonic() + grace_seconds if grace_seconds > 0 else None
    while remaining:
        still_missing: list[str] = []
        for raw_id in remaining:
            row = await _resolve_schedule_run(db, raw_id)
            if row is not None:
                pending[row["id"]] = row  # canonical id as key: dedupes prefix collisions
                continue
            session_row = await _resolve_session_run(db, raw_id)
            if session_row is not None:
                session_pending[session_row["id"]] = session_row
            else:
                still_missing.append(raw_id)
        remaining = still_missing
        if not remaining or deadline is None or time.monotonic() >= deadline:
            break
        await asyncio.sleep(_RESOLVE_GRACE_POLL_SECONDS)
    return pending, session_pending, remaining


async def _schedule_name(db: Any, schedule_id: str, *, cache: dict[str, str]) -> str:
    if schedule_id not in cache:
        sched = await db.get_schedule(schedule_id)
        cache[schedule_id] = (sched or {}).get("name") or schedule_id
    return cache[schedule_id]


def _format_wait_line(row: dict[str, Any], name: str) -> str:
    exit_code = row.get("exit_code")
    exit_str = "-" if exit_code is None else str(exit_code)
    return (
        f"{row['id']}  name={name}  chain_depth={row.get('chain_depth', 0)}  "
        f"status={row['status']}  exit_code={exit_str}"
    )


async def _poll_pending_once(
    db: Any,
    pending: dict[str, dict[str, Any]],
    schedule_names: dict[str, str],
    done: list[dict[str, Any]],
) -> None:
    """Check every still-pending run once; print, record, and drop terminal
    ones. See docs/internals/cli.md for the cancellation-atomicity contract."""
    # Shared schema vocabulary — a hand-listed subset here once omitted
    # timed_out, leaving timed-out occurrences waiting forever.
    from lionagi.state.db import SCHEDULE_RUN_TERMINAL_STATUSES

    from ._logging import log_error

    for run_id in list(pending):
        row = await db.get_schedule_run(run_id)
        if row is None:
            # Gone now (e.g. parent schedule deleted, cascading the run) —
            # resolve as failure so the wait can't hang on state that never returns.
            row = {**pending[run_id], "status": "failed", "exit_code": None}
            log_error(f"schedule_run {run_id!r} disappeared from state.db while waiting")
        elif row["status"] not in SCHEDULE_RUN_TERMINAL_STATUSES:
            continue
        name = await _schedule_name(db, row["schedule_id"], cache=schedule_names)
        # Resolved before the print/append/delete trio below (not between
        # them) so that trio stays await-free and atomic wrt cancellation.
        coord_line = None
        invocation_id = row.get("invocation_id")
        if invocation_id:
            invocation = await db.get_invocation(invocation_id)
            node_metadata = _parse_json_field((invocation or {}).get("node_metadata"))
            coordination = (node_metadata or {}).get("coordination")
            if isinstance(coordination, dict):
                coord_line = _format_coordination_line(coordination)
        print(_format_wait_line(row, name))
        if coord_line:
            print(f"  coordination: {coord_line}")
        done.append(row)
        del pending[run_id]


async def _find_chain_children(db: Any, parent_run_id: str) -> list[dict[str, Any]]:
    """schedule_runs the engine fired as an on_success/on_fail chain child
    of `parent_run_id` (chain_parent_id wiring — see engine.py's _fire)."""
    return await db.fetch_all(
        "SELECT * FROM schedule_runs WHERE chain_parent_id = ?",
        (parent_run_id,),
    )


async def _schedule_declares_chain_action(
    db: Any,
    schedule_id: str,
    exit_code: int | None,
    *,
    cache: dict[str, dict[str, Any] | None],
) -> bool:
    """True if `schedule_id` declares an on_success/on_fail action matching
    the outcome that just landed, i.e. a grace wait for the chain child is warranted."""
    if schedule_id not in cache:
        cache[schedule_id] = await db.get_schedule(schedule_id)
    sched = cache[schedule_id] or {}
    return bool(sched.get("on_success")) if exit_code == 0 else bool(sched.get("on_fail"))


def _new_chain_state(pending: dict[str, dict[str, Any]], *, chain: bool) -> dict[str, Any]:
    """Build the bookkeeping dict `_advance_chains` mutates tick over tick;
    each originally-watched id in `pending` starts out owning itself as its own root."""
    return {
        "root_of": {rid: {rid} for rid in pending},
        "chain_tail_exit": {},
        "awaiting_grace": {},
        "resolved_roots": set(),
        "schedule_cache": {},
        "chain": chain,
        "done_ids": set(),
        "exit_of": {},
        "handoff": {},
    }


async def _advance_chains(
    db: Any,
    pending: dict[str, dict[str, Any]],
    done: list[dict[str, Any]],
    *,
    chain_state: dict[str, Any],
    processed: int,
) -> int:
    """Fold the newly-terminal tail of `done` into chain bookkeeping; returns
    the new `processed` index. See docs/internals/cli.md for the algorithm."""
    root_of = chain_state["root_of"]
    chain_tail_exit = chain_state["chain_tail_exit"]
    awaiting_grace = chain_state["awaiting_grace"]
    resolved_roots = chain_state["resolved_roots"]
    schedule_cache = chain_state["schedule_cache"]
    chain = chain_state["chain"]
    done_ids = chain_state["done_ids"]
    exit_of = chain_state["exit_of"]
    handoff = chain_state["handoff"]

    for row in done[processed:]:
        done_ids.add(row["id"])
        exit_of[row["id"]] = row.get("exit_code")
        roots = root_of.get(row["id"]) or {row["id"]}
        for root in roots:
            chain_tail_exit[root] = row.get("exit_code")
        # A cancelled/skipped/no-exit-code run can never get a chain child —
        # skip the grace window rather than burn it waiting on nothing.
        if (
            chain
            and row.get("chain_depth", 0) < _MAX_CHAIN_DEPTH
            and row.get("status") not in ("cancelled", "skipped")
            and row.get("exit_code") is not None
            and await _schedule_declares_chain_action(
                db, row["schedule_id"], row.get("exit_code"), cache=schedule_cache
            )
        ):
            awaiting_grace[row["id"]] = {"roots": set(roots), "ticks_left": _CHAIN_GRACE_TICKS}
        else:
            resolved_roots.update(roots)
    processed = len(done)

    if chain:
        for parent_id in list(awaiting_grace):
            info = awaiting_grace[parent_id]
            children = await _find_chain_children(db, parent_id)
            if children:
                for child in children:
                    child_id = child["id"]
                    # Union rather than overwrite: the child may already own
                    # a root if it's also a directly-watched id.
                    root_of.setdefault(child_id, set()).update(info["roots"])
                    handoff[parent_id] = child_id
                    if child_id in done_ids:
                        # Already printed once — never re-add to `pending`.
                        # Follow the handoff trail to resolve on the final link.
                        carrier = child_id
                        seen = {carrier}
                        while carrier in handoff and handoff[carrier] not in seen:
                            carrier = handoff[carrier]
                            seen.add(carrier)
                        if carrier not in done_ids:
                            # Still-live descendant in `pending` — ride on it.
                            root_of.setdefault(carrier, set()).update(info["roots"])
                        else:
                            carrier_exit = exit_of.get(carrier)
                            for root in info["roots"]:
                                chain_tail_exit[root] = carrier_exit
                            if carrier in awaiting_grace:
                                # Join the carrier's own still-open grace
                                # window instead of separate bookkeeping.
                                awaiting_grace[carrier]["roots"].update(info["roots"])
                            else:
                                # Carrier resolved outright with no matching
                                # chain action of its own.
                                resolved_roots.update(info["roots"])
                    else:
                        pending[child_id] = child
                del awaiting_grace[parent_id]
                continue
            info["ticks_left"] -= 1
            if info["ticks_left"] <= 0:
                resolved_roots.update(info["roots"])
                del awaiting_grace[parent_id]

    return processed


async def _query_schedule_runs_since(db: Any, baseline: float) -> list[dict[str, Any]]:
    """--follow discovery query: schedule_runs created strictly after
    `baseline` (exclusive), oldest first — anti-backlog-replay discipline."""
    return await db.fetch_all(
        "SELECT * FROM schedule_runs WHERE created_at > ? ORDER BY created_at ASC",
        (baseline,),
    )


def _dispatch_wait(
    ids: list[str],
    *,
    interval: float,
    follow: bool,
    chain: bool = True,
    max_wait: float | None = None,
) -> int:
    """Block until every id in `ids` reaches a terminal schedule_run status;
    see docs/internals/cli.md for chain-following and max_wait semantics."""
    from lionagi.ln.concurrency import run_async
    from lionagi.state.db import StateDB, state_db_known_absent

    from ._logging import log_error
    from .status import EXIT_RUNNING, EXIT_UNKNOWN

    if state_db_known_absent():
        log_error("state.db not found — no schedule runs recorded yet")
        return EXIT_UNKNOWN

    interrupted = False

    def _handle_signal(signum: int, frame: Any) -> None:
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    def _sleep_interval(secs: float) -> None:
        # Sleep in small increments so SIGINT/SIGTERM stay responsive,
        # mirroring _watch_loop's own chunked sleep exactly.
        for _ in range(max(1, round(secs * 10))):
            if interrupted:
                break
            time.sleep(0.1)

    def _run_tick(coro: Any) -> Any:
        # run_async raises bare KeyboardInterrupt on SIGINT during its own
        # call (see run_async's docstring in ln/concurrency/utils.py).
        nonlocal interrupted
        try:
            return run_async(coro)
        except KeyboardInterrupt:
            interrupted = True
            return None

    schedule_names: dict[str, str] = {}

    async def _resolve() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
        async with StateDB() as db:
            return await _resolve_watched_runs(db, ids)

    try:
        resolved = _run_tick(_resolve())
    except AmbiguousIdError as exc:
        # Nothing to wait on: the caller named a prefix, not a run.
        log_error(str(exc))
        return EXIT_UNKNOWN
    if resolved is None:
        # Interrupted before resolving completed — "still in progress",
        # not success or failure.
        return EXIT_RUNNING
    pending, session_pending, unresolved = resolved
    for raw_id in unresolved:
        log_error(f"run id {raw_id!r} not found (checked schedule_runs and sessions)")

    total_watched = len(pending)
    total_sessions = len(session_pending)
    done: list[dict[str, Any]] = []
    session_done: list[dict[str, Any]] = []
    chain_state = _new_chain_state(pending, chain=chain)
    processed = 0

    async def _tick() -> None:
        nonlocal processed
        async with StateDB() as db:
            await _poll_pending_once(db, pending, schedule_names, done)
            await _poll_pending_sessions_once(db, session_pending, session_done)
            processed = await _advance_chains(
                db, pending, done, chain_state=chain_state, processed=processed
            )

    def _chain_open() -> bool:
        return bool(pending or session_pending or chain_state["awaiting_grace"])

    # max_wait bounds total wall-clock so a stuck session can't hang forever;
    # None falls back to the bounded default, 0/negative opts into unbounded.
    effective_max_wait = _DEFAULT_MAX_WAIT_SECONDS if max_wait is None else max_wait
    deadline = time.monotonic() + effective_max_wait if effective_max_wait > 0 else None

    def _wait_expired() -> bool:
        return deadline is not None and time.monotonic() >= deadline

    while _chain_open() and not interrupted and not _wait_expired():
        _run_tick(_tick())
        if not _chain_open() or interrupted or _wait_expired():
            break
        _sleep_interval(interval)

    resolved_roots = chain_state["resolved_roots"]
    chain_tail_exit = chain_state["chain_tail_exit"]
    from ._util import EXIT_CODE_BY_STATUS

    session_ok = all(EXIT_CODE_BY_STATUS.get(r["status"], 1) == 0 for r in session_done)
    if unresolved:
        exit_code = EXIT_UNKNOWN
    elif len(resolved_roots) < total_watched or len(session_done) < total_sessions:
        # Some watched chain (or session) never concluded before we stopped.
        exit_code = EXIT_RUNNING
    elif all(chain_tail_exit.get(root) == 0 for root in resolved_roots) and session_ok:
        # Final-link-wins: each resolved chain's most recently seen terminal
        # exit_code decides, not every link along the way.
        exit_code = 0
    else:
        exit_code = 1

    if follow and not interrupted:
        baseline = time.time()
        follow_pending: dict[str, dict[str, Any]] = {}

        async def _follow_tick(bl: float) -> float:
            async with StateDB() as db:
                new_rows = await _query_schedule_runs_since(db, bl)
                for row in new_rows:
                    follow_pending.setdefault(row["id"], row)
                if follow_pending:
                    await _poll_pending_once(db, follow_pending, schedule_names, [])
            if new_rows:
                bl = max(bl, *(r["created_at"] for r in new_rows))
            return bl

        while not interrupted:
            tick_result = _run_tick(_follow_tick(baseline))
            if tick_result is not None:
                baseline = tick_result
            if interrupted:
                break
            _sleep_interval(interval)
        # --follow has no natural end; exit code stays whatever the initial
        # bounded set resolved to — new runs print but don't feed the aggregate.

    return exit_code


def run_monitor_wait(argv: list[str]) -> int:
    """Entry point for `li monitor run <id> [<id2> ...] [--interval SECS]
    [--follow] [--no-chain]`."""
    parser = argparse.ArgumentParser(prog="li monitor run", add_help=True)
    parser.add_argument(
        "ids",
        nargs="+",
        help=(
            "schedule_run ids to wait for, full or by unique prefix, comma- or space-separated. "
            "An id matching nothing is reported as unresolved rather than holding up the wait."
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=3.0,
        metavar="SECS",
        help="Seconds between state.db polls while waiting (default 3). Must be positive.",
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help=(
            "Keep discovering schedule_runs created after the initial set drains, instead of "
            "exiting once it is empty. Never ends on its own — pair it with --max-wait."
        ),
    )
    parser.add_argument(
        "--no-chain",
        dest="chain",
        action="store_false",
        default=True,
        help=(
            "Watch only the literal id(s) given. By default a watched run "
            "going terminal follows any scheduler on_success/on_fail chain "
            "child the engine fires for it, so the wait tracks the chain to "
            "its final link instead of exiting on the first terminal run."
        ),
    )
    parser.add_argument(
        "--max-wait",
        type=float,
        default=None,
        metavar="SECS",
        help=(
            "Give up waiting after this many seconds and exit EXIT_RUNNING "
            "for whatever is still pending, instead of blocking forever on a "
            f"stuck session or run. Default: {_DEFAULT_MAX_WAIT_SECONDS:g}s. "
            "Pass 0 for unlimited."
        ),
    )
    args = parser.parse_args(argv)
    watched_ids = _split_watched_ids(args.ids)
    if not watched_ids:
        # Tokens can survive nargs="+" but not comma-splitting (e.g. a lone ",").
        parser.error("no schedule_run ids given (only empty/comma-only tokens)")
    return _dispatch_wait(
        watched_ids,
        interval=args.interval,
        follow=args.follow,
        chain=args.chain,
        max_wait=args.max_wait,
    )


# ── CLI registration ──────────────────────────────────────────────────────────


def add_monitor_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register `li monitor` with argparse."""
    mon = subparsers.add_parser(
        "monitor",
        aliases=["mon"],
        help="Observe play/agent/run progress in real-time.",
        description=(
            "Show all running entities (sessions, invocations, shows, plays) "
            "in a compact table, or drill into one entity by ID. "
            "Use --watch for live refresh."
        ),
    )
    mon.add_argument(
        "id",
        nargs="?",
        default=None,
        help=(
            "Run, session, play or show to open in the detail view — full id, or an unambiguous "
            "prefix. Omit it for the table of everything in flight."
        ),
    )
    mon.add_argument(
        "--watch",
        "-w",
        action="store_true",
        help=(
            "Redraw the view every --refresh seconds until interrupted, instead of printing one "
            "snapshot and exiting."
        ),
    )
    mon.add_argument(
        "--refresh",
        type=int,
        default=2,
        metavar="SECS",
        help="Seconds between redraws under --watch (default 2). Ignored without it.",
    )
    mon.add_argument(
        "--since",
        default=None,
        metavar="WINDOW",
        help=(
            "Only show entities updated within this time window. "
            "Format: 30m, 1h, 2d (minutes/hours/days). Default: all running."
        ),
    )
    mon.add_argument(
        "--type",
        "-t",
        dest="entity_type",
        default=None,
        choices=["session", "invocation", "show", "play"],
        help="Narrow the table to one kind of entity: session, invocation, show or play.",
    )
    mon.add_argument(
        "--project",
        "-p",
        default=None,
        help=(
            "Filter sessions and plays to one project name, matched exactly — the same name the "
            "table's project column shows."
        ),
    )
    mon.add_argument(
        "--run",
        dest="run_ids",
        default=None,
        metavar="ID[,ID...]",
        help=(
            "Wait for one or more schedule_run IDs to reach a terminal state, "
            "then exit (scripting primitive; see `li monitor run --help` for "
            "the positional form). Follows scheduler on_success/on_fail chain "
            "children by default (see --no-chain). "
            "Ignores id/--watch/--since/--type/--project."
        ),
    )
    mon.add_argument(
        "--interval",
        type=float,
        default=3.0,
        metavar="SECS",
        help=(
            "Seconds between state.db polls in --run mode (default 3). Independent of --refresh, "
            "which paces the table redraw."
        ),
    )
    mon.add_argument(
        "--follow",
        action="store_true",
        help=(
            "With --run: keep discovering schedule_runs created after the initial set drains, "
            "instead of exiting once it is empty. Never ends on its own — pair with --max-wait."
        ),
    )
    mon.add_argument(
        "--no-chain",
        dest="chain",
        action="store_false",
        default=True,
        help=(
            "With --run: watch only the literal id(s) given, instead of "
            "following scheduler on_success/on_fail chain children by default."
        ),
    )
    mon.add_argument(
        "--max-wait",
        type=float,
        default=None,
        metavar="SECS",
        help=(
            "With --run: give up waiting after this many seconds and exit "
            "EXIT_RUNNING for whatever is still pending, instead of blocking "
            f"forever on a stuck session or run. Default: {_DEFAULT_MAX_WAIT_SECONDS:g}s. "
            "Pass 0 for unlimited."
        ),
    )


@auto_register(
    area="monitor", cli=CliDeclaration(seed="monitor", parser_factory=add_monitor_subparser)
)
def run_monitor(args: argparse.Namespace) -> int:
    """Dispatch `li monitor` subcommand."""
    from lionagi.ln.concurrency import run_async

    if args.run_ids:
        watched_ids = _split_watched_ids([args.run_ids])
        if not watched_ids:
            # A truthy --run value can still split to nothing (e.g. ","),
            # which would otherwise dispatch an empty watch set and exit 0.
            from ._logging import log_error

            log_error("no schedule_run ids given (only empty/comma-only tokens)")
            return 2
        return _dispatch_wait(
            watched_ids,
            interval=args.interval,
            follow=args.follow,
            chain=args.chain,
            max_wait=args.max_wait,
        )

    since: float | None = None
    if args.since:
        try:
            since = _since_timestamp(args.since)
        except ValueError as exc:
            from ._logging import log_error

            log_error(str(exc))
            return 1

    entity_id: str | None = args.id
    entity_type: str | None = args.entity_type
    project: str | None = args.project

    if args.watch:
        return _watch_loop(
            args.refresh,
            entity_id,
            since_window=args.since or None,
            entity_type=entity_type,
            project=project,
        )

    if entity_id:
        try:
            output = run_async(_run_detail(entity_id))
        except AmbiguousIdError as exc:
            from ._logging import log_error
            from .status import EXIT_UNKNOWN

            log_error(str(exc))
            return EXIT_UNKNOWN
    else:
        output = run_async(_run_table(since=since, entity_type=entity_type, project=project))

    print(output)
    return 0


# ── machine result ────────────────────────────────────────────────────────────

# Every entity carries these; a caller reads `kind` and knows which of the
# per-kind blocks below it also has. Timestamps are epoch seconds exactly as the
# store holds them — an elapsed string would be this module's arithmetic against
# a clock the caller cannot see, and `observed_at` lets the caller do that sum
# itself against a moment it was told.
_ENTITY_CORE = ("id", "status", "started_at", "ended_at", "updated_at")

_ENTITY_EXTRA: dict[str, tuple[str, ...]] = {
    "session": (
        "project",
        "invocation_kind",
        # Describes the `ended_at` in the core block above. Withheld, the
        # caller receives a reconstructed end that is indistinguishable from a
        # measured one, which is the confusion the column was added to remove.
        # Session-only: no other entity kind has a reconstructed end.
        "ended_at_is_approximate",
        "agent_name",
        "playbook_name",
        "current_phase",
        "branch_count",
    ),
    "invocation": ("skill", "plugin"),
    "show": ("repo", "topic"),
    "play": ("name", "session_id", "show_id", "session_project", "branch_count"),
}


def _machine_entity(kind: str, row: dict[str, Any]) -> dict[str, Any]:
    entity: dict[str, Any] = {"kind": kind}
    for field in (*_ENTITY_CORE, *_ENTITY_EXTRA[kind]):
        entity[field] = row.get(field)
    return entity


async def _machine_monitor_data(
    *, since_window: str | None, entity_type: str | None, project: str | None
) -> dict[str, Any]:
    from .machine import available as _available
    from .machine import readonly_state_db

    since = _since_timestamp(since_window) if since_window else None
    filters = {
        "since": since_window,
        "since_epoch": since,
        "type": entity_type,
        "project": project,
    }
    async with readonly_state_db() as (db, why):
        if db is None:
            return {
                "entities": why,
                "filters": filters,
                "observed_at": time.time(),
            }
        entities = await _gather_entities(db, since=since, entity_type=entity_type, project=project)
    return {
        "entities": _available([_machine_entity(kind, row) for kind, row in entities]),
        "filters": filters,
        "observed_at": time.time(),
    }


def machine_result(argv: list[str]) -> dict[str, Any]:
    """`li monitor --machine` — the in-flight table, one entity per entry.

    The detail view is not reachable here: it answers about one entity with a
    different shape entirely, and a payload whose fields depend on whether an id
    was passed is one a caller cannot branch on. It stays a separate decision.
    """
    from lionagi.ln.concurrency import run_async

    from .machine import MachineError, machine_parser, parse_machine_argv

    parser = machine_parser("li monitor")
    parser.add_argument("--since", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--type", dest="entity_type", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--project", "-p", default=None, help=argparse.SUPPRESS)
    args = parse_machine_argv(parser, argv)

    if args.entity_type is not None and args.entity_type not in _ENTITY_EXTRA:
        raise MachineError(
            "invalid_input",
            f"--type must be one of {', '.join(sorted(_ENTITY_EXTRA))}; got {args.entity_type!r}",
        )
    try:
        return run_async(
            _machine_monitor_data(
                since_window=args.since, entity_type=args.entity_type, project=args.project
            )
        )
    except ValueError as exc:
        raise MachineError("invalid_input", str(exc)) from exc
