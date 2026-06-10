# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li state` — inspect and migrate lionagi state.db."""

from __future__ import annotations

import argparse
import json

# Import pure helpers from split modules (public surface preserved).
from ._runs import RUNS_ROOT
from ._state_db_import import (
    _EXIT_CODE_STATUS_MAP,
    _STATUS_MAP,
    _derive_import_status,
    _derive_timestamps,
    _import_one_run,
    _msg_from_collection_entry,
    _mtime_as_float,
)

# Re-export DB operations helpers (public surface preserved).
from ._state_db_ops import (
    _checkpoint,
    _doctor,
    _format_bytes,
    _list_sessions,
    _print_stats,
    _prune,
    _vacuum,
)

__all__ = [
    # import helpers
    "RUNS_ROOT",
    "_mtime_as_float",
    "_msg_from_collection_entry",
    "_import_runs",
    "_STATUS_MAP",
    "_EXIT_CODE_STATUS_MAP",
    "_derive_import_status",
    "_derive_timestamps",
    "_import_one_run",
    "_import_teams",
    # ops helpers
    "_format_bytes",
    "_list_sessions",
    "_print_stats",
    "_checkpoint",
    "_vacuum",
    "_prune",
    "_doctor",
    # CLI entrypoints
    "add_state_subparser",
    "run_state",
]


async def _import_runs() -> dict[str, int]:
    """Scan RUNS_ROOT and import every run with a run.json manifest."""
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


async def _import_teams() -> dict[str, int]:
    """Backfill ~/.lionagi/teams/*.json into the teams + team_messages tables."""
    import uuid

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
                        (content[:200] + "…") if len(content) > 200 else None,
                        json.dumps(read_by_arr),
                        None,
                    ),
                )
                counts["messages"] += 1

        await db.db.commit()

    return counts


def add_state_subparser(subparsers: argparse._SubParsersAction) -> None:
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
