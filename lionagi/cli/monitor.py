# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li monitor` — observe play/agent/run progress in real-time."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path
from typing import Any

from ._runs import RUNS_ROOT
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


def _elapsed(started_at: float | None, ended_at: float | None = None) -> str:
    """Human-readable elapsed time.  Uses ended_at if present, else now."""
    if started_at is None:
        return "-"
    end = ended_at if ended_at is not None else time.time()
    secs = int(end - started_at)
    if secs < 0:
        return "0s"
    if secs < 60:
        return f"{secs}s"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m{secs:02d}s"
    hrs, mins = divmod(mins, 60)
    return f"{hrs}h{mins:02d}m"


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
    project: str | None = None,
) -> list[dict[str, Any]]:
    """Active only by default; with since, all statuses in the window.

    Shows carry their project under the `repo` column (see _show_to_row),
    not a `project` column, so project-scoping matches against `repo` — the
    same field the table already renders under the PROJECT header.
    """
    query = "SELECT * FROM shows WHERE 1=1"  # noqa: S608
    params: list[Any] = []
    if since is not None:
        query += " AND updated_at >= ?"
        params.append(since)
    else:
        query += " AND status = 'active'"
    if project:
        query += " AND repo = ?"
        params.append(project)
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

    The plays table has no project column, so project-scoping matches
    against the project of the play's linked session (session_id) — the
    same source of truth _query_running_sessions filters on. A play with
    no linked session has no project to match and is excluded.
    """
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
        running_statuses = ("running", "running_complete", "gated", "redoing", "prepared")
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
    """Resolve entity_id across all entity tables; returns (entity_type, row) or None."""
    searches = [
        ("session", "sessions"),
        ("invocation", "invocations"),
        ("show", "shows"),
        ("play", "plays"),
    ]
    for entity_type, table in searches:
        # Exact match
        row = await db.fetch_one(
            f"SELECT * FROM {table} WHERE id = ?",  # noqa: S608
            (entity_id,),
        )
        if row:
            return entity_type, row
        # Prefix match (user might type short prefix)
        row = await db.fetch_one(
            f"SELECT * FROM {table} WHERE id LIKE ?",  # noqa: S608
            (entity_id + "%",),
        )
        if row:
            return entity_type, row
    return None


# ── Run manifest helpers ──────────────────────────────────────────────────────


def _load_run_manifests(*, since: float | None = None) -> list[dict[str, Any]]:
    """Read run.json files from RUNS_ROOT, newest first."""
    if not RUNS_ROOT.exists():
        return []
    results: list[dict[str, Any]] = []
    for run_dir in sorted(RUNS_ROOT.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not run_dir.is_dir():
            continue
        manifest_path = run_dir / "run.json"
        if not manifest_path.exists():
            continue
        try:
            import json as _json

            manifest = _json.loads(manifest_path.read_text())
        except (OSError, ValueError):
            continue
        mtime = run_dir.stat().st_mtime
        if since is not None and mtime < since:
            continue
        manifest["_mtime"] = mtime
        manifest["_run_dir"] = str(run_dir)
        results.append(manifest)
    return results


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


def _format_table(rows: list[dict[str, Any]]) -> str:
    """Render a fixed-width table of entity rows.

    Expected keys per row: id, type, project, status, phase, elapsed, agents.
    """
    if not rows:
        return _dim("(no running entities)")

    # Column widths — uppercase name is intentional (table layout constant)
    col = {  # noqa: N806
        "id": 16,
        "type": 11,
        "project": 14,
        "status": 15,
        "phase": 18,
        "elapsed": 9,
        "agents": 7,
    }

    header_parts = [
        _bold(f"{'ID':<{col['id']}}"),
        _bold(f"{'TYPE':<{col['type']}}"),
        _bold(f"{'PROJECT':<{col['project']}}"),
        _bold(f"{'STATUS':<{col['status']}}"),
        _bold(f"{'PHASE':<{col['phase']}}"),
        _bold(f"{'ELAPSED':>{col['elapsed']}}"),
        _bold(f"{'AGENTS':>{col['agents']}}"),
    ]
    header = "  ".join(header_parts)
    separator = _dim("-" * (sum(col.values()) + 2 * (len(col) - 1)))

    lines = [header, separator]
    for row in rows:
        eid = _trunc(str(row.get("id", "")), col["id"])
        etype = _trunc(str(row.get("type", "")), col["type"])
        eproject = _trunc(str(row.get("project", "-")), col["project"])
        estatus = row.get("status", "")
        ephase = _trunc(str(row.get("phase", "-")), col["phase"])
        eelapsed = _trunc(str(row.get("elapsed", "-")), col["elapsed"])
        eagents = str(row.get("agents", "-"))

        coloured_status = _colour_status(estatus)
        # Pad status accounting for invisible ANSI codes
        visible_status = estatus
        pad = col["status"] - len(visible_status)
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
        "elapsed": _elapsed(sess.get("started_at"), sess.get("ended_at")),
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


async def _detail_session(db: Any, sess: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append(_bold(f"SESSION  {sess['id']}"))
    lines.append(f"  status:    {_colour_status(sess.get('status') or '?')}")
    lines.append(f"  kind:      {sess.get('invocation_kind') or '-'}")
    lines.append(f"  project:   {sess.get('project') or '-'}")
    lines.append(f"  model:     {sess.get('model') or '-'}")
    lines.append(f"  provider:  {sess.get('provider') or '-'}")
    lines.append(f"  effort:    {sess.get('effort') or '-'}")
    lines.append(f"  elapsed:   {_elapsed(sess.get('started_at'), sess.get('ended_at'))}")
    started = sess.get("started_at")
    if started:
        lines.append(f"  started:   {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(started))}")
    last_msg = sess.get("last_message_at")
    if last_msg:
        lines.append(f"  last_msg:  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_msg))}")

    # Branch count
    row = await db.fetch_one(
        "SELECT COUNT(*) AS n FROM branches WHERE session_id = ?", (sess["id"],)
    )
    lines.append(f"  branches:  {row['n'] if row else 0}")

    # Stream tail from run dir
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

    # List child sessions
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
    lines: list[str] = []
    lines.append(_bold(f"SHOW  {show['id']}"))
    lines.append(f"  topic:   {show.get('topic') or '-'}")
    lines.append(f"  status:  {_colour_status(show.get('status') or '?')}")
    lines.append(f"  repo:    {show.get('repo') or '-'}")
    lines.append(f"  branch:  {show.get('base_branch') or '-'}")
    lines.append(f"  goal:    {(show.get('goal') or '-')[:80]}")

    # Plays breakdown
    plays = await _query_plays_for_show(db, show["id"])
    if plays:
        lines.append("")
        lines.append(_dim("  -- plays --"))
        terminal = {"merged", "escalated", "gate_failed", "aborted_after_finish"}
        active = {"running", "running_complete", "gated", "redoing"}
        for play in plays:
            pstatus = play.get("status") or "?"
            if pstatus in terminal:
                marker = _dim("  [done]  ")
            elif pstatus in active:
                marker = _green("  [live]  ")
            else:
                marker = _yellow("  [wait]  ")
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

    # Gate result
    gp = play.get("gate_passed")
    if gp is not None:
        gate_str = _green("PASS") if gp else _red("FAIL")
        lines.append(f"  gate:     {gate_str}")
        feedback = play.get("gate_feedback")
        if feedback:
            lines.append(f"  feedback: {_trunc(str(feedback), 80)}")

    # Linked session details
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

            # Stream tail
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


async def _gather_table_rows(
    db: Any,
    *,
    since: float | None,
    entity_type: str | None,
    project: str | None,
) -> list[dict[str, Any]]:
    """Collect entity rows across all tables and convert to table-row dicts."""
    rows: list[dict[str, Any]] = []

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
        # drop that session only when the play row itself is being shown —
        # dedup against what this view actually fetched, never in SQL, so a
        # session view or a play row outside the window still renders the
        # session once.
        play_session_ids = {p["session_id"] for p in plays if p.get("session_id")}
        sessions = [s for s in sessions if s["id"] not in play_session_ids]

    rows.extend(_session_to_row(s) for s in sessions)

    if entity_type in (None, "invocation"):
        invocations = await _query_running_invocations(db, since=since)
        rows.extend(_invocation_to_row(i) for i in invocations)

    if entity_type in (None, "show"):
        shows = await _query_active_shows(db, since=since, project=project)
        rows.extend(_show_to_row(s) for s in shows)

    rows.extend(_play_to_row(p) for p in plays)

    return rows


# ── Async main routines ───────────────────────────────────────────────────────


async def _run_table(
    *,
    since: float | None,
    entity_type: str | None,
    project: str | None,
) -> str:
    try:
        from lionagi.state.db import DEFAULT_DB_PATH, StateDB

        if not DEFAULT_DB_PATH.exists():
            return _dim("(no state.db — run `li agent` at least once)")
        async with StateDB() as db:
            rows = await _gather_table_rows(
                db, since=since, entity_type=entity_type, project=project
            )
        return _format_table(rows)
    except Exception as exc:  # noqa: BLE001
        return _red(f"error reading state.db: {exc}")


async def _run_detail(entity_id: str) -> str:
    try:
        from lionagi.state.db import DEFAULT_DB_PATH, StateDB

        if not DEFAULT_DB_PATH.exists():
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
    except Exception as exc:  # noqa: BLE001
        return _red(f"error: {exc}")


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
    """Repeatedly clear screen and reprint; exit cleanly on SIGINT/SIGTERM.

    The since cutoff is re-derived from the window string every tick so the
    window slides with the clock; a cutoff frozen at launch would accumulate
    terminal rows for the life of the watch.
    """
    from lionagi.ln.concurrency import run_async

    interrupted = False

    def _handle_signal(signum: int, frame: Any) -> None:
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while not interrupted:
        since = _since_timestamp(since_window) if since_window else None
        if entity_id:
            output = run_async(_run_detail(entity_id))
        else:
            output = run_async(_run_table(since=since, entity_type=entity_type, project=project))
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

    if _IS_TTY:
        print()  # newline after Ctrl-C
    return 0


# ── Wait-for-terminal primitive (li monitor run / li monitor --run) ───────────
#
# A scripting primitive, not a view: append-only stdout lines (no screen
# clearing, no table), meant for a harness to poll `li monitor run <id>` as a
# background task rather than hand-rolling raw sqlite polling against the
# live WAL-mode state.db. Separate code path from _watch_loop above — that
# one is a human dashboard; this one blocks until specific schedule_runs go
# terminal, then exits with a meaningful code.
#
# Chain-following (default on; opt out with --no-chain): the scheduler can
# fire a child run from a terminal run's schedule on_success/on_fail (engine
# records chain_parent_id/chain_depth on the child — see
# lionagi/studio/scheduler/engine.py's _fire). A watched run going terminal
# is not necessarily the end of its chain, so once it lands we extend the
# watch frontier with any already-fired children and keep watching. A
# schedule that declares a chain action for the outcome but whose child
# hasn't fired yet gets a bounded grace window (_CHAIN_GRACE_TICKS poll
# ticks) before the chain is concluded on the parent's own exit code — a
# schedule with no matching chain action needs no grace wait at all. The
# aggregate exit code is final-link-wins: each chain's *last* link decides,
# not every link along the way (an on_fail recovery child that succeeds
# means the chain as a whole succeeded).

_TERMINAL_SCHEDULE_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "skipped"})
_CHAIN_GRACE_TICKS = 2


def _split_watched_ids(raw: list[str]) -> list[str]:
    """Flatten comma-separated and/or space-separated id tokens into one
    ordered, deduped list — `li monitor run a,b` and `li monitor run a b`
    (and any mix) must resolve identically."""
    seen: dict[str, None] = {}
    for token in raw:
        for piece in token.split(","):
            piece = piece.strip()
            if piece:
                seen.setdefault(piece, None)
    return list(seen)


async def _resolve_schedule_run(db: Any, raw_id: str) -> dict[str, Any] | None:
    """Exact match then prefix match, mirroring _find_entity's convention.
    schedule_run ids are 12-char hex (uuid4().hex[:12]), not 36-char UUIDs,
    so the length-36 prefix heuristic used elsewhere in this codebase
    (resolve_entity in _util.py) does not apply here.
    """
    row = await db.get_schedule_run(raw_id)
    if row:
        return row
    return await db.fetch_one(
        "SELECT * FROM schedule_runs WHERE id LIKE ?",
        (raw_id + "%",),
    )


async def _resolve_watched_runs(
    db: Any, ids: list[str]
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Resolve every requested id once, up front. schedule_run ids are
    already-fired-run identifiers a caller obtained elsewhere (e.g. `li
    schedule trigger`), not something that might come into existence later,
    so — unlike the --follow discovery scan below — resolution itself is
    not retried on every poll tick.
    """
    pending: dict[str, dict[str, Any]] = {}
    unresolved: list[str] = []
    for raw_id in ids:
        row = await _resolve_schedule_run(db, raw_id)
        if row is None:
            unresolved.append(raw_id)
        else:
            pending[row["id"]] = row  # canonical id as key: dedupes prefix collisions
    return pending, unresolved


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
    """Check every still-pending run once; print, record into `done`, and
    drop from `pending` any that are now terminal. Printing happens
    immediately per row, not batched, so a harness tailing stdout sees each
    result the moment it lands rather than waiting for the whole watched
    set to finish.

    `done` is mutated in place rather than returned: a coroutine only ever
    suspends at an `await`, and there is no `await` between the print/
    append/delete below, so that trio is atomic with respect to task
    cancellation — a row can never leave `pending` without also landing in
    `done`, even if the caller (run_async, see _dispatch_wait) discards
    this coroutine's actual return value because a SIGINT raced its
    completion.

    This is the primitive's testable inner tick: exercising "terminal
    across different poll iterations" only needs two direct calls to this
    function with a DB row mutated in between — no real sleeping required.
    """
    from ._logging import log_error

    for run_id in list(pending):
        row = await db.get_schedule_run(run_id)
        if row is None:
            # Existed at resolution time but is gone now (e.g. its parent
            # schedule was deleted, cascading the run) — resolve it as a
            # failure so the wait can't hang on state that never comes back.
            row = {**pending[run_id], "status": "failed", "exit_code": None}
            log_error(f"schedule_run {run_id!r} disappeared from state.db while waiting")
        elif row["status"] not in _TERMINAL_SCHEDULE_RUN_STATUSES:
            continue
        name = await _schedule_name(db, row["schedule_id"], cache=schedule_names)
        print(_format_wait_line(row, name))
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
    the outcome that just landed — i.e. the engine *will* attempt to fire a
    chain child, so a grace wait for it is warranted. A schedule with no
    matching action needs no grace wait at all."""
    if schedule_id not in cache:
        cache[schedule_id] = await db.get_schedule(schedule_id)
    sched = cache[schedule_id] or {}
    return bool(sched.get("on_success")) if exit_code == 0 else bool(sched.get("on_fail"))


def _new_chain_state(pending: dict[str, dict[str, Any]], *, chain: bool) -> dict[str, Any]:
    """Build the bookkeeping dict `_advance_chains` mutates tick over tick —
    each originally-watched id in `pending` starts out owning itself as its
    own root. Factored out so tests can drive `_advance_chains` directly the
    same way they drive `_poll_pending_once`, without duplicating its
    internal shape."""
    return {
        "root_of": {rid: {rid} for rid in pending},
        "chain_tail_exit": {},
        "awaiting_grace": {},
        "resolved_roots": set(),
        "schedule_cache": {},
        "chain": chain,
        # run ids that have already gone terminal and been folded once below
        # (printed by _poll_pending_once) — lets discovery tell an
        # already-processed child apart from one still in `pending`.
        "done_ids": set(),
        # run id -> exit_code observed when that run was folded — unlike
        # chain_tail_exit (keyed by root), this answers "what did run X
        # itself exit with" for any folded run, watched root or not.
        "exit_of": {},
        # parent run id -> the chain child its grace window handed off to.
        # Lets a later-discovering ancestor follow an already-processed
        # link forward to wherever the chain currently lives instead of
        # stopping at that link's own exit.
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
    """Fold the newly-terminal tail of `done` (index `processed` onward)
    into chain bookkeeping: extend `pending` with any already-fired
    children, resolve roots whose schedule has no matching chain action
    immediately, and start/advance a grace countdown for roots that do
    declare one but whose child hasn't fired yet. Returns the new
    `processed` index.

    Mutates `pending` in place exactly like `_poll_pending_once` mutates
    `done` — so a caller (the real dispatch loop, or a test) can drive this
    function tick-by-tick with direct DB mutations in between, no real
    sleeping required, the same way `_poll_pending_once` is tested above.

    `chain_state` keys: root_of (run_id -> set of originally-watched roots
    that run currently accounts for — a run can own more than one root when
    an overlapping watch set passes both a run and one of its own chain
    descendants as separate roots, see below), chain_tail_exit (root id ->
    most recent terminal exit_code seen for that chain), awaiting_grace
    (run_id -> {roots, ticks_left}), resolved_roots (root ids whose chain
    has concluded), schedule_cache (memoized `db.get_schedule` lookups),
    chain (bool — chain-following on/off; when off, every terminal row
    resolves its root immediately, matching --no-chain's "watch only the
    literal ids given"), done_ids (run ids already folded into the above
    once — lets discovery tell an already-processed child apart from one
    still waiting in `pending`), exit_of (run id -> its own folded
    exit_code), handoff (parent run id -> the chain child its grace window
    handed off to — followed forward when an ancestor discovers an
    already-processed link, see below).
    """
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
        if chain and await _schedule_declares_chain_action(
            db, row["schedule_id"], row.get("exit_code"), cache=schedule_cache
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
                    # A discovered child can itself already own a root — it
                    # may be a directly-watched id in an overlapping watch
                    # set (e.g. `li monitor run parent child` where child is
                    # parent's own chain_parent_id link). Union the parent's
                    # root(s) into whatever the child already owns instead
                    # of overwriting, so the child's own root still resolves
                    # once the child (now the chain's tail) goes terminal.
                    root_of.setdefault(child_id, set()).update(info["roots"])
                    handoff[parent_id] = child_id
                    if child_id in done_ids:
                        # The child already went terminal (possibly in the
                        # very same tick as its parent) and was already
                        # printed once by _poll_pending_once. Re-adding it
                        # to `pending` would print it again on the next
                        # tick, so fold the parent's root(s) into whatever
                        # bookkeeping the chain currently lives in instead —
                        # never touch `pending` for an already-printed run.
                        # The child's own grace window may itself have
                        # already handed off to a deeper link, so follow the
                        # handoff trail to the chain's current carrier
                        # first; stopping at this child would resolve the
                        # parent's root(s) with an intermediate exit code
                        # instead of the final link's.
                        carrier = child_id
                        seen = {carrier}
                        while carrier in handoff and handoff[carrier] not in seen:
                            carrier = handoff[carrier]
                            seen.add(carrier)
                        if carrier not in done_ids:
                            # The chain's tail is a still-live descendant in
                            # `pending` — the parent's root(s) ride on it and
                            # resolve when it goes terminal, like any other
                            # handed-off root.
                            root_of.setdefault(carrier, set()).update(info["roots"])
                        else:
                            carrier_exit = exit_of.get(carrier)
                            for root in info["roots"]:
                                chain_tail_exit[root] = carrier_exit
                            if carrier in awaiting_grace:
                                # The carrier's own schedule also declares a
                                # matching chain action — join its still-open
                                # grace window rather than spinning up
                                # separate bookkeeping for the same run.
                                awaiting_grace[carrier]["roots"].update(info["roots"])
                            else:
                                # The carrier resolved outright (no matching
                                # chain action of its own) — the parent's
                                # root(s) resolve right along with it.
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
    `baseline`, oldest first. Strict '>' (not '>=') is the same
    baseline-first, anti-backlog-replay discipline used by any other
    "watch for new stuff" loop in this codebase — a row already seen at
    exactly `baseline` must never be re-reported on the next tick.
    """
    return await db.fetch_all(
        "SELECT * FROM schedule_runs WHERE created_at > ? ORDER BY created_at ASC",
        (baseline,),
    )


def _dispatch_wait(ids: list[str], *, interval: float, follow: bool, chain: bool = True) -> int:
    """Block until every id in `ids` reaches a terminal schedule_run status,
    printing one line per run the moment it lands; with --follow, keep
    watching (tail -f style) for newly created runs after the initial set
    drains, exiting only on SIGINT/SIGTERM.

    Chain-following (default on; `chain=False` for --no-chain): a watched
    run going terminal extends the watch frontier with any scheduler
    on_success/on_fail chain children (see module comment above
    _TERMINAL_SCHEDULE_RUN_STATUSES and _advance_chains). The aggregate exit
    code is final-link-wins per chain, not every link along the way — with
    chain-following off, that collapses back to the original one-run-one-
    link behavior.

    Shaped like _watch_loop above (own signal handlers on the main thread,
    run_async per discrete tick, chunked time.sleep for cadence) rather
    than one run_async call wrapping a long-lived asyncio.sleep loop:
    run_async installs its own SIGINT handler for the duration of any call
    it wraps and never touches SIGTERM at all, so a single all-encompassing
    call cannot give this command the dual-signal clean exit it needs —
    per-tick calls, exactly like the dashboard's --watch, can. Unlike
    _watch_loop's default multi-second refresh, --interval is often
    sub-second here, so a much larger share of wall-clock time is spent
    inside individual run_async() calls rather than between them — a SIGINT
    landing mid-call surfaces as KeyboardInterrupt at the call site (see
    _run_tick below) far more often than it does for the dashboard, so that
    case is folded into the same clean-exit path instead of left to crash.
    """
    from lionagi.ln.concurrency import run_async
    from lionagi.state.db import DEFAULT_DB_PATH, StateDB

    from ._logging import log_error
    from .status import EXIT_RUNNING, EXIT_UNKNOWN

    if not DEFAULT_DB_PATH.exists():
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
        # run_async raises bare KeyboardInterrupt when SIGINT lands during
        # its own call (it installs a temporary handler for that duration —
        # see run_async's docstring in ln/concurrency/utils.py). Treat that
        # exactly like `interrupted` being set between ticks: no traceback,
        # no half-updated state, just the same clean stop.
        nonlocal interrupted
        try:
            return run_async(coro)
        except KeyboardInterrupt:
            interrupted = True
            return None

    schedule_names: dict[str, str] = {}

    async def _resolve() -> tuple[dict[str, dict[str, Any]], list[str]]:
        async with StateDB() as db:
            return await _resolve_watched_runs(db, ids)

    resolved = _run_tick(_resolve())
    if resolved is None:
        # Interrupted before resolving even completed — we don't yet know
        # which ids exist or what state they're in, so there is no aggregate
        # to report. This is "still in progress", not success or failure.
        return EXIT_RUNNING
    pending, unresolved = resolved
    for raw_id in unresolved:
        log_error(f"schedule_run {raw_id!r} not found")

    total_watched = len(pending)
    done: list[dict[str, Any]] = []
    chain_state = _new_chain_state(pending, chain=chain)
    processed = 0

    async def _tick() -> None:
        nonlocal processed
        async with StateDB() as db:
            await _poll_pending_once(db, pending, schedule_names, done)
            processed = await _advance_chains(
                db, pending, done, chain_state=chain_state, processed=processed
            )

    def _chain_open() -> bool:
        return bool(pending or chain_state["awaiting_grace"])

    while _chain_open() and not interrupted:
        _run_tick(_tick())
        if not _chain_open() or interrupted:
            break
        _sleep_interval(interval)

    resolved_roots = chain_state["resolved_roots"]
    chain_tail_exit = chain_state["chain_tail_exit"]
    if unresolved:
        exit_code = EXIT_UNKNOWN
    elif len(resolved_roots) < total_watched:
        # Some watched chain never concluded before we had to stop — either
        # still pending outright or still inside its grace window. `done`
        # (mutated in _poll_pending_once) and `chain_state` (mutated in
        # _advance_chains, see above) are the ground truth that survives a
        # tick being interrupted mid-flight.
        exit_code = EXIT_RUNNING
    elif all(chain_tail_exit.get(root) == 0 for root in resolved_roots):
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
        # --follow has no natural end, so its exit code is whatever the
        # *initial* bounded set already resolved to (see exit_code above)
        # — new runs discovered during the tail print their own lines but
        # don't feed the final aggregate.

    return exit_code


def run_monitor_wait(argv: list[str]) -> int:
    """Entry point for `li monitor run <id> [<id2> ...] [--interval SECS]
    [--follow] [--no-chain]`."""
    parser = argparse.ArgumentParser(prog="li monitor run", add_help=True)
    parser.add_argument(
        "ids",
        nargs="+",
        help="schedule_run ID(s) (or short prefixes) to wait for. Comma- or space-separated.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=3.0,
        metavar="SECS",
        help="Poll interval in seconds (default 3).",
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help="Keep watching for new schedule_runs after the initial set drains.",
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
    args = parser.parse_args(argv)
    watched_ids = _split_watched_ids(args.ids)
    if not watched_ids:
        # nargs="+" only guarantees argv had at least one token, not that any
        # of them survive comma-splitting (e.g. a lone "," or ""). Without
        # this check that degenerates into a silent, instant, exit-0 no-op.
        parser.error("no schedule_run ids given (only empty/comma-only tokens)")
    return _dispatch_wait(watched_ids, interval=args.interval, follow=args.follow, chain=args.chain)


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
        help="Entity ID (or prefix) to show detail view for. Omit for table view.",
    )
    mon.add_argument(
        "--watch",
        "-w",
        action="store_true",
        help="Live-refresh the view every REFRESH seconds (default 2).",
    )
    mon.add_argument(
        "--refresh",
        type=int,
        default=2,
        metavar="SECS",
        help="Refresh interval for --watch mode (default 2).",
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
        help="Filter table to a single entity type.",
    )
    mon.add_argument(
        "--project",
        "-p",
        default=None,
        help="Filter sessions and plays by project name.",
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
        help="Poll interval in seconds for --run mode (default 3). Independent of --refresh.",
    )
    mon.add_argument(
        "--follow",
        action="store_true",
        help="With --run: keep watching for new schedule_runs after the initial set drains.",
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
            watched_ids, interval=args.interval, follow=args.follow, chain=args.chain
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
        output = run_async(_run_detail(entity_id))
    else:
        output = run_async(_run_table(since=since, entity_type=entity_type, project=project))

    print(output)
    return 0
