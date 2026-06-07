# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li state` — inspect and migrate lionagi state.db.

Subcommands:
    li state import   Import all runs from ~/.lionagi/runs/ into state.db.
    li state ls       List sessions in state.db.
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

from ._runs import RUNS_ROOT

# ── helpers ──────────────────────────────────────────────────────────────────


def _mtime_as_float(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        import time

        return time.time()


def _msg_from_collection_entry(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert a branch-collection message dict to the DB insert shape.

    branch JSON uses ``metadata`` for the node metadata dict; the DB layer
    expects the key ``node_metadata``.  Everything else passes through.
    """
    return {
        "id": raw["id"],
        "created_at": raw["created_at"],
        "node_metadata": raw.get("metadata"),  # rename metadata → node_metadata
        "content": raw.get("content", {}),
        "embedding": raw.get("embedding"),
        "sender": raw.get("sender"),
        "recipient": raw.get("recipient"),
        "channel": raw.get("channel"),
        "role": raw["role"],
    }


# ── async import logic ────────────────────────────────────────────────────────


async def _import_runs() -> dict[str, int]:
    """Scan RUNS_ROOT and import every run that has a run.json manifest.

    Returns counts: {sessions, branches, messages}.
    """
    from lionagi.state.db import StateDB

    counts = {"sessions": 0, "branches": 0, "messages": 0, "skipped": 0, "errors": 0}

    if not RUNS_ROOT.exists():
        print(f"runs directory not found: {RUNS_ROOT}")
        return counts

    run_dirs = [p for p in RUNS_ROOT.iterdir() if p.is_dir()]
    run_dirs.sort(key=lambda p: p.stat().st_mtime)

    print(f"scanning {len(run_dirs)} run directories in {RUNS_ROOT} ...")

    async with StateDB() as db:
        for run_dir in run_dirs:
            manifest_path = run_dir / "run.json"
            if not manifest_path.exists():
                continue

            try:
                manifest = json.loads(manifest_path.read_text())
            except Exception as exc:
                print(f"  [error] {run_dir.name}: failed to read run.json — {exc}")
                counts["errors"] += 1
                continue

            run_id = manifest.get("run_id") or run_dir.name

            # Idempotent: skip runs already in the DB.
            existing = await db.get_session(run_id)
            if existing is not None:
                counts["skipped"] += 1
                continue

            try:
                session_count, branch_count, msg_count = await _import_one_run(
                    db, run_id, run_dir, manifest
                )
            except Exception as exc:
                print(f"  [error] {run_dir.name}: {exc}")
                counts["errors"] += 1
                continue

            counts["sessions"] += session_count
            counts["branches"] += branch_count
            counts["messages"] += msg_count

    return counts


_STATUS_MAP = {
    "running": "running",
    "completed": "completed",
    "failed": "failed",
    "aborted": "aborted",
    "timed_out": "timed_out",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    # common aliases that may appear in run.json
    "success": "completed",
    "error": "failed",
    "timeout": "timed_out",
}

_EXIT_CODE_STATUS_MAP = {
    0: "completed",
    1: "failed",
    124: "timed_out",
    130: "aborted",
    143: "cancelled",
}


def _derive_import_status(manifest: dict[str, Any]) -> str:
    """Derive session status from run.json per ADR-0025.

    1. If manifest has "status" field → map to session vocabulary.
    2. If manifest has "exit_code" → map via ADR-0025 exit code table.
    3. Otherwise → completed (conservative default).
    """
    raw_status = manifest.get("status")
    if raw_status is not None:
        return _STATUS_MAP.get(str(raw_status).lower(), "completed")

    exit_code = manifest.get("exit_code")
    if exit_code is not None:
        return _EXIT_CODE_STATUS_MAP.get(exit_code, "failed")

    return "completed"


def _derive_timestamps(
    manifest: dict[str, Any],
    run_dir: Path,
) -> tuple[float, float]:
    """Return (started_at, ended_at) as floats.

    Prefer manifest fields; fall back to filesystem ctime / mtime.
    """
    import time as _time

    started_at = manifest.get("started_at")
    ended_at = manifest.get("ended_at")

    try:
        stat = run_dir.stat()
        fs_ctime = stat.st_birthtime if hasattr(stat, "st_birthtime") else stat.st_ctime
        fs_mtime = stat.st_mtime
    except OSError:
        now = _time.time()
        fs_ctime = now
        fs_mtime = now

    if started_at is None:
        started_at = fs_ctime
    if ended_at is None:
        ended_at = fs_mtime

    # If the values came from manifest they may be ISO strings; coerce to float.
    if isinstance(started_at, str):
        import datetime

        try:
            started_at = datetime.datetime.fromisoformat(started_at).timestamp()
        except ValueError:
            started_at = fs_ctime
    if isinstance(ended_at, str):
        import datetime

        try:
            ended_at = datetime.datetime.fromisoformat(ended_at).timestamp()
        except ValueError:
            ended_at = fs_mtime

    return float(started_at), float(ended_at)


async def _import_one_run(
    db: Any,
    run_id: str,
    run_dir: Path,
    manifest: dict[str, Any],
) -> tuple[int, int, int]:
    """Import a single run into the DB.  Returns (sessions, branches, messages) imported."""
    created_at = _mtime_as_float(run_dir)
    session_name = manifest.get("kind") or "agent"

    status = _derive_import_status(manifest)
    started_at, ended_at = _derive_timestamps(manifest, run_dir)

    # Create session-level progression (empty for now; updated after branches).
    session_prog_id = str(uuid.uuid4())
    await db.create_progression(session_prog_id)

    # Provenance enrichment (ADR-0012 §52-54): derive invocation_kind
    # from manifest "kind" so imported runs are filterable by the same
    # vocabulary live runs write. Map "agent" / "play" / "flow" /
    # "fanout" literally; pre-show legacy kinds map to "agent" as the
    # safest default. Unrecognized kinds → NULL (won't pass the
    # invocation_kind enum check otherwise).
    raw_kind = (manifest.get("kind") or "").lower()
    legacy_kind_map = {
        "agent": "agent",
        "play": "play",
        "flow": "flow",
        "fanout": "fanout",
    }
    invocation_kind = legacy_kind_map.get(raw_kind)

    # artifacts_path: prefer manifest field, fall back to run_dir/artifacts
    # if present on disk. None if neither.
    artifacts_path = manifest.get("artifact_root") or manifest.get("artifacts_path")
    if artifacts_path is None:
        candidate = run_dir / "artifacts"
        if candidate.exists():
            artifacts_path = str(candidate)

    # Session must exist before branches can reference it via FK.
    await db.create_session(
        {
            "id": run_id,
            "created_at": created_at,
            "node_metadata": None,
            "name": session_name,
            "user": None,
            "progression_id": session_prog_id,
            "first_msg_id": None,
            "last_msg_id": None,
            # ADR-0012 enriched provenance — written so imported rows are
            # queryable by the same fields live runs use.
            "invocation_kind": invocation_kind,
            "playbook_name": manifest.get("playbook_name") or manifest.get("playbook"),
            "agent_name": manifest.get("agent_name") or manifest.get("agent"),
            "artifacts_path": artifacts_path,
            "source_kind": "imported_fs",
            "status": status,
            "started_at": started_at,
            "ended_at": ended_at,
        }
    )

    branches_dir = run_dir / "branches"

    branch_files: list[Path] = []
    if branches_dir.exists():
        branch_files = list(branches_dir.glob("*.json"))

    total_branches = 0
    total_messages = 0
    session_msg_ids: list[str] = []

    for branch_file in sorted(branch_files, key=lambda p: p.stat().st_mtime):
        try:
            branch_data = json.loads(branch_file.read_text())
        except Exception as exc:
            print(f"    [warn] {branch_file.name}: failed to read — {exc}")
            continue

        branch_id = branch_data.get("id") or branch_file.stem
        branch_created_at = branch_data.get("created_at") or _mtime_as_float(branch_file)

        # Extract messages and ordering from Pile format.
        messages_pile = branch_data.get("messages", {})
        raw_collection: list[dict] = messages_pile.get("collections", [])
        progression_info = messages_pile.get("progression", {})
        order: list[str] = progression_info.get("order", [])

        # Build an id→raw map, fall back to collection order if no explicit order.
        by_id: dict[str, dict] = {m["id"]: m for m in raw_collection if "id" in m}
        if order:
            ordered_msgs = [by_id[mid] for mid in order if mid in by_id]
        else:
            ordered_msgs = raw_collection

        # Detect system message (first message with role == "system").
        system_msg_id: str | None = None
        for raw_msg in ordered_msgs:
            if raw_msg.get("role") == "system":
                system_msg_id = raw_msg["id"]
                break

        # Insert all messages.
        branch_msg_ids: list[str] = []
        for raw_msg in ordered_msgs:
            msg = _msg_from_collection_entry(raw_msg)
            await db.insert_message(msg)
            branch_msg_ids.append(msg["id"])
            total_messages += 1

        # Create branch progression with ordered message IDs.
        branch_prog_id = str(uuid.uuid4())
        await db.create_progression(branch_prog_id, branch_msg_ids)

        # Derive branch metadata from manifest branches list.
        manifest_branch_meta = {}
        for mb in manifest.get("branches", []):
            if mb.get("id") == branch_id:
                manifest_branch_meta = mb
                break

        node_meta: dict[str, Any] = {}
        provider = manifest_branch_meta.get("provider") or manifest.get("provider")
        model = manifest_branch_meta.get("model") or manifest.get("model")
        if provider:
            node_meta["provider"] = provider
        if model:
            node_meta["model"] = model
        branch_name = manifest_branch_meta.get("name") or manifest.get("kind")

        await db.create_branch(
            {
                "id": branch_id,
                "created_at": branch_created_at,
                "node_metadata": node_meta or None,
                "user": branch_data.get("user"),
                "name": branch_name,
                "session_id": run_id,
                "progression_id": branch_prog_id,
                "system_msg_id": system_msg_id,
            }
        )

        session_msg_ids.extend(branch_msg_ids)
        total_branches += 1

    # Back-fill session progression with all message IDs collected from branches.
    if session_msg_ids:
        await db.db.execute(
            "UPDATE progressions SET collection = ? WHERE id = ?",
            (json.dumps(session_msg_ids), session_prog_id),
        )
        await db.db.commit()
        await db.update_session(
            run_id,
            first_msg_id=session_msg_ids[0],
            last_msg_id=session_msg_ids[-1],
        )

    print(f"  imported {run_id}: {total_branches} branch(es), {total_messages} message(s)")
    return 1, total_branches, total_messages


# ── teams import (ADR-0019) ─────────────────────────────────────────────────


async def _import_teams() -> dict[str, int]:
    """Backfill ~/.lionagi/teams/*.json into the teams + team_messages tables.

    Idempotent: teams already present in ``teams`` (by id) are skipped along
    with their messages. JSON files remain the runtime's primary write path
    until the dual-write layer ships.
    """
    from lionagi.state.db import StateDB

    teams_dir = (RUNS_ROOT.parent / "teams").resolve()
    counts = {"teams": 0, "messages": 0, "skipped_teams": 0, "errors": 0}
    if not teams_dir.exists():
        return counts

    json_files = sorted(teams_dir.glob("*.json"))
    if not json_files:
        return counts

    async with StateDB() as db:
        for path in json_files:
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                counts["errors"] += 1
                continue
            team_id = data.get("id")
            if not team_id:
                counts["errors"] += 1
                continue

            # Idempotency: check before inserting.
            cur = await db.db.execute("SELECT 1 FROM teams WHERE id = ? LIMIT 1", (team_id,))
            if await cur.fetchone() is not None:
                counts["skipped_teams"] += 1
                continue

            members = data.get("members") or []
            created_at = _mtime_as_float(path)
            await db.db.execute(
                """INSERT INTO teams
                   (id, name, created_at, updated_at, member_count, members, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    team_id,
                    data.get("name") or team_id,
                    created_at,
                    created_at,
                    len(members),
                    json.dumps(members),
                    "active",
                ),
            )
            counts["teams"] += 1

            for msg in data.get("messages") or []:
                msg_id = msg.get("id") or uuid.uuid4().hex[:12]
                to = msg.get("to") or []
                if isinstance(to, str):
                    recipient = to or "all"
                else:
                    recipient = "all" if to == ["*"] else ",".join(to) or "all"
                content = msg.get("content") or ""
                ts_raw = msg.get("timestamp")
                # JSON stores ISO timestamps; the DB stores REAL epoch
                # seconds. Best-effort parse; fall back to file mtime so
                # ordering is at least monotonic per file.
                try:
                    from datetime import datetime

                    created = datetime.fromisoformat(ts_raw).timestamp()
                except (TypeError, ValueError):
                    created = created_at
                read_by = msg.get("read_by") or {}
                if isinstance(read_by, dict):
                    read_by_arr = sorted(read_by.keys())
                elif isinstance(read_by, list):
                    read_by_arr = list(read_by)
                else:
                    read_by_arr = []
                await db.db.execute(
                    """INSERT INTO team_messages
                       (id, team_id, created_at, sender, recipient, content,
                        summary, read_by, session_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        msg_id,
                        team_id,
                        created,
                        msg.get("from") or "_unknown",
                        recipient,
                        content,
                        # Only set summary for long content; leave NULL otherwise
                        # so the Studio teams page knows to show raw inline.
                        (content[:200] + "…") if len(content) > 200 else None,
                        json.dumps(read_by_arr),
                        None,
                    ),
                )
                counts["messages"] += 1

        await db.db.commit()

    return counts


# ── async ls logic ────────────────────────────────────────────────────────────


async def _list_sessions(*, limit: int = 50, status: str | None = None) -> None:
    """Print a simple table of sessions in state.db, paginated."""
    import time

    from lionagi.state.db import StateDB

    async with StateDB() as db:
        if status:
            cur = await db.db.execute(
                "SELECT id, name, status, updated_at FROM sessions "
                "WHERE status = ? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            cur = await db.db.execute(
                "SELECT id, name, status, updated_at FROM sessions "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cur.fetchall()

        if not rows:
            print("(no sessions in state.db)")
            return

        header = (
            f"{'ID':<36}  {'NAME':<16}  {'STATUS':<10}  "
            f"{'BRANCHES':>8}  {'MESSAGES':>8}  {'UPDATED':<20}"
        )
        print(header)
        print("-" * len(header))
        for row in rows:
            sid = row["id"]
            name = (row["name"] or "")[:16]
            sstat = (row["status"] or "")[:10]
            updated = row["updated_at"]
            updated_str = (
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(updated)) if updated else ""
            )

            branch_cur = await db.db.execute(
                "SELECT COUNT(*) AS n FROM branches WHERE session_id = ?", (sid,)
            )
            bc = (await branch_cur.fetchone())["n"]

            prog_cur = await db.db.execute(
                "SELECT progression_id FROM sessions WHERE id = ?", (sid,)
            )
            prog_row = await prog_cur.fetchone()
            msg_count = 0
            if prog_row and prog_row["progression_id"]:
                prog_data = await db.get_progression(prog_row["progression_id"])
                msg_count = len(prog_data)

            print(f"{sid:<36}  {name:<16}  {sstat:<10}  {bc:>8}  {msg_count:>8}  {updated_str:<20}")


# ── Maintenance commands: stats / checkpoint / vacuum / prune ───────────────


async def _print_stats() -> None:
    """Print DB/WAL size, row counts, and SQLite pragma settings."""
    from lionagi.state.db import DEFAULT_DB_PATH, StateDB

    db_path = DEFAULT_DB_PATH
    db_size = db_path.stat().st_size if db_path.exists() else 0
    wal_path = db_path.with_name(db_path.name + "-wal")
    wal_size = wal_path.stat().st_size if wal_path.exists() else 0

    print(f"state.db path:   {db_path}")
    print(f"state.db size:   {_format_bytes(db_size)}")
    print(f"state.db-wal:    {_format_bytes(wal_size)}")
    print()

    if not db_path.exists():
        print("(no state.db yet — first run will create it)")
        return

    async with StateDB() as db:
        # Row counts per table.
        print("Row counts:")
        for table in (
            "messages",
            "progressions",
            "sessions",
            "branches",
            "definitions",
            "shows",
            "plays",
        ):
            cur = await db.db.execute(
                f"SELECT COUNT(*) AS n FROM {table}"  # noqa: S608
            )
            row = await cur.fetchone()
            print(f"  {table:<14} {row['n']:>10}")
        print()

        # Session status distribution.
        cur = await db.db.execute(
            "SELECT COALESCE(status, '(null)') AS s, COUNT(*) AS n "
            "FROM sessions GROUP BY status ORDER BY n DESC"
        )
        print("Sessions by status:")
        for row in await cur.fetchall():
            print(f"  {row['s']:<14} {row['n']:>10}")
        print()

        # PRAGMAs that affect operational behavior.
        print("PRAGMAs:")
        for pragma in (
            "journal_mode",
            "wal_autocheckpoint",
            "busy_timeout",
            "synchronous",
            "foreign_keys",
        ):
            cur = await db.db.execute(f"PRAGMA {pragma}")
            row = await cur.fetchone()
            val = row[0] if row else "?"
            print(f"  {pragma:<22} {val}")


async def _checkpoint(mode: str) -> str:
    """Run wal_checkpoint and return a summary string."""
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        cur = await db.db.execute(f"PRAGMA wal_checkpoint({mode})")
        row = await cur.fetchone()
        if not row:
            return "(no result)"
        # SQLite returns (busy, log_pages, checkpointed_pages)
        return f"busy={row[0]}, log_pages={row[1]}, checkpointed={row[2]}"


async def _vacuum() -> None:
    """Run VACUUM. Holds exclusive lock for the duration."""
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        await db.db.execute("VACUUM")
        await db.db.commit()


async def _prune(
    *,
    keep_days: int,
    keep_n: int,
    dry_run: bool,
) -> dict[str, int]:
    """Delete sessions older than ``keep_days``, preserving the most
    recent ``keep_n``. Returns counts of what was (or would be) deleted.
    """
    import time as _time

    from lionagi.state.db import StateDB

    cutoff = _time.time() - (keep_days * 86400)

    async with StateDB() as db:
        # Sessions to keep: top N most recent OR newer than cutoff.
        cur = await db.db.execute(
            """SELECT id FROM sessions
               WHERE id NOT IN (
                 SELECT id FROM sessions
                 ORDER BY updated_at DESC LIMIT ?
               )
               AND (updated_at < ? OR updated_at IS NULL)""",
            (keep_n, cutoff),
        )
        rows = await cur.fetchall()
        victim_ids = [r["id"] for r in rows]

        if not victim_ids:
            return {"sessions": 0, "branches": 0, "messages": 0}

        # Count cascaded branches up front.
        placeholders = ",".join("?" * len(victim_ids))
        cur = await db.db.execute(
            f"SELECT COUNT(*) AS n FROM branches "  # noqa: S608
            f"WHERE session_id IN ({placeholders})",
            victim_ids,
        )
        branch_count = (await cur.fetchone())["n"]

        # Orphan messages: ones whose id isn't in any surviving progression.
        # Cheap estimate via message count delta — not exact, but useful.
        cur = await db.db.execute("SELECT COUNT(*) AS n FROM messages")
        msgs_before = (await cur.fetchone())["n"]

        if dry_run:
            return {
                "sessions": len(victim_ids),
                "branches": branch_count,
                "messages": 0,  # can't preview without doing the delete
            }

        # Delete sessions — branches cascade via FK ON DELETE CASCADE.
        # Messages are NOT cascaded (they're referenced by progression
        # JSON arrays, not FK columns), so we sweep orphans below.
        await db.db.execute(
            f"DELETE FROM sessions WHERE id IN ({placeholders})",  # noqa: S608
            victim_ids,
        )
        await db.db.commit()

        # Sweep messages no longer referenced by any progression.
        await db.db.execute(
            """DELETE FROM messages
               WHERE id NOT IN (
                 SELECT value FROM progressions, json_each(progressions.collection)
               )"""
        )
        await db.db.commit()

        cur = await db.db.execute("SELECT COUNT(*) AS n FROM messages")
        msgs_after = (await cur.fetchone())["n"]

        return {
            "sessions": len(victim_ids),
            "branches": branch_count,
            "messages": msgs_before - msgs_after,
        }


async def _doctor(
    *,
    stale_hours: int,
    dry_run: bool,
    new_status: str = "aborted",
) -> dict[str, int]:
    """Sweep sessions stuck at ``status='running'`` whose ``started_at``
    is older than ``stale_hours``.

    A SIGKILL or process death between session-open and teardown leaves
    the session row at ``status='running'`` forever — the next
    ``StateDB.open()`` only applies pragmas/schema; it does not sweep.
    This command is the operator-explicit recovery path.

    Conservative: we only touch sessions whose ``status = 'running'``
    AND ``(started_at IS NULL OR started_at < cutoff)`` — and the same
    predicate is folded into the UPDATE so a session that completes
    after victim selection but before the UPDATE is NOT overwritten
    (R6 select-then-update race). ``swept`` returns the rowcount of
    the UPDATE, not the count of pre-selected victims.

    Returns ``{"running": N, "swept": M, "skipped": K}``:
    - running: total sessions currently at status='running'
    - swept: rows actually updated (post-race-check)
    - skipped: running sessions younger than threshold at select time
    """
    import time as _time

    from lionagi.state.db import StateDB

    cutoff = _time.time() - (stale_hours * 3600)

    async with StateDB() as db:
        cur = await db.db.execute("SELECT id, started_at FROM sessions WHERE status = 'running'")
        rows = await cur.fetchall()
        total = len(rows)
        victims: list[str] = []
        skipped = 0
        for row in rows:
            started = row["started_at"]
            # No started_at on a 'running' row is itself a corruption
            # signal — treat as stale.
            if started is None or started < cutoff:
                victims.append(row["id"])
            else:
                skipped += 1

        swept_count = 0
        if dry_run:
            swept_count = len(victims)
        elif victims:
            # Race-safe: re-assert status='running' AND stale predicate
            # in the UPDATE itself so a session that finished between
            # select and update is NOT overwritten.
            placeholders = ",".join("?" * len(victims))
            params = [new_status, _time.time(), cutoff, *victims]
            cur = await db.db.execute(
                f"UPDATE sessions SET status = ?, ended_at = ? "  # noqa: S608
                f"WHERE status = 'running' "
                f"  AND (started_at IS NULL OR started_at < ?) "
                f"  AND id IN ({placeholders})",
                params,
            )
            swept_count = cur.rowcount or 0
            await db.db.commit()

        return {"running": total, "swept": swept_count, "skipped": skipped}


def _format_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


# ── CLI wiring ────────────────────────────────────────────────────────────────


def add_state_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register `li state` with its subcommands."""
    state = subparsers.add_parser(
        "state",
        help="Inspect and migrate lionagi state.db.",
        description="Manage the lionagi SQLite state database.",
    )
    state_sub = state.add_subparsers(dest="state_command", required=True)

    # li state import
    state_sub.add_parser(
        "import",
        help="Import all runs from ~/.lionagi/runs/ into state.db.",
        description=(
            "Scan ~/.lionagi/runs/ for run directories with run.json manifests "
            "and load their sessions, branches, and messages into state.db. "
            "Already-imported sessions are skipped (idempotent)."
        ),
    )

    # li state import-teams (ADR-0019)
    state_sub.add_parser(
        "import-teams",
        help="Backfill team JSON files (~/.lionagi/teams/*.json) into state.db.",
        description=(
            "Scan ~/.lionagi/teams/*.json and INSERT each team + its messages "
            "into the `teams` and `team_messages` tables (ADR-0019). Idempotent: "
            "existing rows (matched by team id) are left alone. Run once after "
            "upgrading; the runtime can keep using JSON until the dual-write "
            "path ships."
        ),
    )

    # li state ls
    ls = state_sub.add_parser(
        "ls",
        help="List sessions in state.db.",
        description="Print a table of sessions stored in state.db.",
    )
    ls.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max sessions to list (default 50).",
    )
    ls.add_argument(
        "--status",
        default=None,
        help="Filter by session status (running|completed|failed|aborted).",
    )

    # li state stats
    state_sub.add_parser(
        "stats",
        help="Print DB/WAL size, row counts, and lifecycle health.",
        description=(
            "Report state.db + state.db-wal sizes, per-table row counts, "
            "session status distribution, and SQLite PRAGMAs (journal_mode, "
            "wal_autocheckpoint, busy_timeout). Use to spot growth and "
            "lock contention."
        ),
    )

    # li state checkpoint
    cp = state_sub.add_parser(
        "checkpoint",
        help="Force a WAL checkpoint (frees disk if no readers active).",
        description=(
            "Run PRAGMA wal_checkpoint(TRUNCATE|PASSIVE|RESTART|FULL). "
            "Default is TRUNCATE — most aggressive, frees the WAL file if "
            "no readers are active."
        ),
    )
    cp.add_argument(
        "--mode",
        default="TRUNCATE",
        choices=["PASSIVE", "FULL", "RESTART", "TRUNCATE"],
        help="Checkpoint mode (default TRUNCATE).",
    )

    # li state vacuum
    state_sub.add_parser(
        "vacuum",
        help="Rebuild the DB file to reclaim free pages.",
        description=(
            "Run VACUUM — rebuilds the entire DB file, reclaiming pages "
            "freed by previous deletes. Holds an exclusive lock for the "
            "duration. Run after `li state prune`."
        ),
    )

    # li state prune
    prune = state_sub.add_parser(
        "prune",
        help="Delete old sessions (and their branches/messages).",
        description=(
            "Delete sessions older than --keep-days (default 30), keeping "
            "the most recent --keep-n (default 100). Foreign key cascades "
            "drop branches; messages are dropped if no other session "
            "references them via progression. Use --dry-run to preview."
        ),
    )
    prune.add_argument(
        "--keep-days",
        type=int,
        default=30,
        help="Keep sessions updated within the last N days (default 30).",
    )
    prune.add_argument(
        "--keep-n",
        type=int,
        default=100,
        help="Always keep the N most recent sessions (default 100).",
    )
    prune.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what WOULD be deleted, but don't actually delete.",
    )

    # li state doctor — sweep stale 'running' sessions
    doctor = state_sub.add_parser(
        "doctor",
        help="Sweep sessions stuck at status='running' after a crash.",
        description=(
            "A SIGKILL or unclean exit between session-open and teardown "
            "leaves the session row at status='running' forever. This "
            "command resets such rows (older than --stale-hours, default "
            "24) to --new-status (default 'aborted'). Conservative: only "
            "sessions whose started_at is older than the threshold are "
            "swept, so an actively-running CLI process is left alone. "
            "Use --dry-run first."
        ),
    )
    doctor.add_argument(
        "--stale-hours",
        type=int,
        default=24,
        help="Threshold in hours since started_at (default 24).",
    )
    doctor.add_argument(
        "--new-status",
        default="aborted",
        choices=["aborted", "failed"],
        help="Status to assign swept sessions (default 'aborted').",
    )
    doctor.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what WOULD be swept, but don't update rows.",
    )


def run_state(args: argparse.Namespace) -> int:
    """Dispatch `li state` subcommands."""
    from lionagi.ln.concurrency import run_async

    if args.state_command == "import":
        counts = run_async(_import_runs())
        print(
            f"\nimported {counts['sessions']} session(s), "
            f"{counts['branches']} branch(es), "
            f"{counts['messages']} message(s) "
            f"[skipped={counts['skipped']}, errors={counts['errors']}]"
        )
        return 0 if counts["errors"] == 0 else 1

    if args.state_command == "import-teams":
        counts = run_async(_import_teams())
        print(
            f"\nimported {counts['teams']} team(s), "
            f"{counts['messages']} team message(s) "
            f"[skipped_teams={counts['skipped_teams']}, errors={counts['errors']}]"
        )
        return 0 if counts["errors"] == 0 else 1

    if args.state_command == "ls":
        run_async(
            _list_sessions(
                limit=args.limit,
                status=args.status,
            )
        )
        return 0

    if args.state_command == "stats":
        run_async(_print_stats())
        return 0

    if args.state_command == "checkpoint":
        freed = run_async(_checkpoint(args.mode))
        print(f"checkpoint({args.mode}) → {freed}")
        return 0

    if args.state_command == "vacuum":
        run_async(_vacuum())
        print("vacuum complete")
        return 0

    if args.state_command == "prune":
        result = run_async(
            _prune(
                keep_days=args.keep_days,
                keep_n=args.keep_n,
                dry_run=args.dry_run,
            )
        )
        prefix = "(dry-run) would delete" if args.dry_run else "deleted"
        print(
            f"{prefix} {result['sessions']} session(s), "
            f"{result['branches']} branch(es), "
            f"{result['messages']} orphan message(s)"
        )
        return 0

    if args.state_command == "doctor":
        result = run_async(
            _doctor(
                stale_hours=args.stale_hours,
                dry_run=args.dry_run,
                new_status=args.new_status,
            )
        )
        prefix = "(dry-run) would sweep" if args.dry_run else "swept"
        print(
            f"running={result['running']}, "
            f"{prefix}={result['swept']} → {args.new_status}, "
            f"skipped_recent={result['skipped']} "
            f"(threshold: {args.stale_hours}h)"
        )
        return 0

    return 1
