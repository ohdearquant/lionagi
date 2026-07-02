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
    query = (
        "SELECT sessions.*, "
        "(SELECT COUNT(*) FROM branches WHERE session_id = sessions.id) AS branch_count "
        "FROM sessions WHERE status = 'running'"  # noqa: S608
    )
    params: list[Any] = []
    if since is not None:
        query += " AND updated_at >= ?"
        params.append(since)
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
    query = "SELECT * FROM invocations WHERE status = 'running'"
    params: list[Any] = []
    if since is not None:
        query += " AND updated_at >= ?"
        params.append(since)
    query += " ORDER BY started_at DESC"
    rows = await db.fetch_all(query, params)
    return rows


async def _query_active_shows(
    db: Any,
    *,
    since: float | None = None,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM shows WHERE status = 'active'"
    params: list[Any] = []
    if since is not None:
        query += " AND updated_at >= ?"
        params.append(since)
    query += " ORDER BY updated_at DESC"
    rows = await db.fetch_all(query, params)
    return rows


async def _query_running_plays(
    db: Any,
    *,
    since: float | None = None,
) -> list[dict[str, Any]]:
    running_statuses = ("running", "running_complete", "gated", "redoing", "prepared")
    placeholders = ",".join("?" * len(running_statuses))
    query = (
        f"SELECT plays.*, "  # noqa: S608
        f"(SELECT COUNT(*) FROM branches WHERE session_id = plays.session_id) AS branch_count "
        f"FROM plays WHERE status IN ({placeholders})"
    )
    params: list[Any] = list(running_statuses)
    if since is not None:
        query += " AND updated_at >= ?"
        params.append(since)
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
        "project": "-",
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

    if entity_type in (None, "session", "agent", "play_session", "play"):
        # For specific type filters, restrict to sessions whose invocation_kind matches.
        # "session" (no filter) and None (all) show every running session.
        inv_kind = entity_type if entity_type not in (None, "session") else None
        sessions = await _query_running_sessions(
            db, since=since, project=project, invocation_kind=inv_kind
        )
        rows.extend(_session_to_row(s) for s in sessions)

    if entity_type in (None, "invocation"):
        invocations = await _query_running_invocations(db, since=since)
        rows.extend(_invocation_to_row(i) for i in invocations)

    if entity_type in (None, "show"):
        shows = await _query_active_shows(db, since=since)
        rows.extend(_show_to_row(s) for s in shows)

    if entity_type in (None, "play"):
        plays = await _query_running_plays(db, since=since)
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
    since: float | None,
    entity_type: str | None,
    project: str | None,
) -> int:
    """Repeatedly clear screen and reprint; exit cleanly on SIGINT/SIGTERM."""
    from lionagi.ln.concurrency import run_async

    interrupted = False

    def _handle_signal(signum: int, frame: Any) -> None:
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while not interrupted:
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

_TERMINAL_SCHEDULE_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "skipped"})


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
) -> list[dict[str, Any]]:
    """Check every still-pending run once; print (and drop from `pending`)
    any that are now terminal. Printing happens immediately per row, not
    batched, so a harness tailing stdout sees each result the moment it
    lands rather than waiting for the whole watched set to finish.

    This is the primitive's testable inner tick: exercising "terminal
    across different poll iterations" only needs two direct calls to this
    function with a DB row mutated in between — no real sleeping required.
    """
    from ._logging import log_error

    resolved: list[dict[str, Any]] = []
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
        resolved.append(row)
        del pending[run_id]
    return resolved


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


def _dispatch_wait(ids: list[str], *, interval: float, follow: bool) -> int:
    """Block until every id in `ids` reaches a terminal schedule_run status,
    printing one line per run the moment it lands; with --follow, keep
    watching (tail -f style) for newly created runs after the initial set
    drains, exiting only on SIGINT/SIGTERM.

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
    pending, unresolved = resolved if resolved is not None else ({}, [])
    for raw_id in unresolved:
        log_error(f"schedule_run {raw_id!r} not found")

    async def _tick() -> list[dict[str, Any]]:
        async with StateDB() as db:
            return await _poll_pending_once(db, pending, schedule_names)

    done: list[dict[str, Any]] = []
    while pending and not interrupted:
        tick_result = _run_tick(_tick())
        if tick_result is not None:
            done.extend(tick_result)
        if not pending or interrupted:
            break
        _sleep_interval(interval)

    if unresolved:
        exit_code = EXIT_UNKNOWN
    elif pending:  # only reachable via interruption mid-wait
        exit_code = EXIT_RUNNING
    elif all(r.get("exit_code") == 0 for r in done):
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
                    await _poll_pending_once(db, follow_pending, schedule_names)
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
        # --follow has no natural end — its exit code reflects whether the
        # *initial* watched set was resolvable, not the open-ended tail.
        return EXIT_UNKNOWN if unresolved else 0

    return exit_code


def run_monitor_wait(argv: list[str]) -> int:
    """Entry point for `li monitor run <id> [<id2> ...] [--interval SECS] [--follow]`."""
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
    args = parser.parse_args(argv)
    return _dispatch_wait(_split_watched_ids(args.ids), interval=args.interval, follow=args.follow)


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
        help="Filter sessions by project name.",
    )
    mon.add_argument(
        "--run",
        dest="run_ids",
        default=None,
        metavar="ID[,ID...]",
        help=(
            "Wait for one or more schedule_run IDs to reach a terminal state, "
            "then exit (scripting primitive; see `li monitor run --help` for "
            "the positional form). Ignores id/--watch/--since/--type/--project."
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


def run_monitor(args: argparse.Namespace) -> int:
    """Dispatch `li monitor` subcommand."""
    from lionagi.ln.concurrency import run_async

    if args.run_ids:
        return _dispatch_wait(
            _split_watched_ids([args.run_ids]), interval=args.interval, follow=args.follow
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
            since=since,
            entity_type=entity_type,
            project=project,
        )

    if entity_id:
        output = run_async(_run_detail(entity_id))
    else:
        output = run_async(_run_table(since=since, entity_type=entity_type, project=project))

    print(output)
    return 0
