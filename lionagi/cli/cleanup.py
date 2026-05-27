# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li state cleanup` — remove stale lionagi state files.

Cleans up expired run directories, orphaned team JSON files, and old log
files.  All operations are non-destructive by default: pass ``--dry-run``
to preview what would be removed.

Subcommand registered by :func:`add_cleanup_subcommand`.  Invoked via
:func:`run_cleanup`.
"""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

from ._logging import hint, log_error, progress, warn

# ── constants ──────────────────────────────────────────────────────────────────

_SECS_PER_DAY = 86_400


def _default_older_than() -> int:
    return 30


# ── helpers ────────────────────────────────────────────────────────────────────


def _dir_size(path: Path) -> int:
    """Return total byte size of a directory tree (best-effort)."""
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _file_size(path: Path) -> int:
    """Return file size in bytes (best-effort)."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _format_bytes(n: int) -> str:
    """Human-readable byte count."""
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n //= 1024
    return f"{n:.1f} TiB"


def _mtime(path: Path) -> float:
    """Return mtime as a float; fall back to now so nothing is accidentally old."""
    try:
        return path.stat().st_mtime
    except OSError:
        return time.time()


def _ask_confirm(prompt: str) -> bool:
    """Prompt the user for y/N confirmation; returns True on 'y'/'yes'."""
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


# ── cleanup functions ──────────────────────────────────────────────────────────


def cleanup_runs(
    runs_root: Path,
    *,
    older_than_days: int,
    dry_run: bool,
) -> dict[str, int]:
    """Remove run directories older than *older_than_days*.

    A run directory qualifies for removal when its mtime (last write) is
    older than the cutoff.  Directories without a ``run.json`` are treated
    as orphans and are also eligible.

    Returns ``{"removed": N, "bytes_freed": B, "errors": E}``.
    """
    counts: dict[str, int] = {"removed": 0, "bytes_freed": 0, "errors": 0}

    if not runs_root.exists():
        progress(f"runs directory not found: {runs_root} — nothing to clean")
        return counts

    cutoff = time.time() - older_than_days * _SECS_PER_DAY

    for entry in sorted(runs_root.iterdir()):
        if not entry.is_dir():
            continue
        if _mtime(entry) >= cutoff:
            continue

        size = _dir_size(entry)
        if dry_run:
            print(f"  [dry-run] would remove run: {entry.name}  ({_format_bytes(size)})")
            counts["removed"] += 1
            counts["bytes_freed"] += size
        else:
            try:
                shutil.rmtree(entry)
                counts["removed"] += 1
                counts["bytes_freed"] += size
            except OSError as exc:
                warn(f"could not remove {entry}: {exc}")
                counts["errors"] += 1

    return counts


def cleanup_teams(
    teams_root: Path,
    *,
    dry_run: bool,
) -> dict[str, int]:
    """Remove orphaned team JSON files.

    A team file is considered orphaned when it cannot be parsed as valid
    JSON or when it has no ``id`` field.  Well-formed files are kept — the
    caller must decide whether to purge active teams via ``li state prune``.

    Returns ``{"removed": N, "bytes_freed": B, "errors": E}``.
    """
    import json

    counts: dict[str, int] = {"removed": 0, "bytes_freed": 0, "errors": 0}

    if not teams_root.exists():
        progress(f"teams directory not found: {teams_root} — nothing to clean")
        return counts

    for entry in sorted(teams_root.glob("*.json")):
        if not entry.is_file():
            continue

        orphaned = False
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
            if not data.get("id"):
                orphaned = True
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            orphaned = True

        if not orphaned:
            continue

        size = _file_size(entry)
        if dry_run:
            print(
                f"  [dry-run] would remove orphaned team file: {entry.name}  ({_format_bytes(size)})"
            )
            counts["removed"] += 1
            counts["bytes_freed"] += size
        else:
            try:
                entry.unlink()
                counts["removed"] += 1
                counts["bytes_freed"] += size
            except OSError as exc:
                warn(f"could not remove {entry}: {exc}")
                counts["errors"] += 1

    return counts


def cleanup_logs(
    logs_root: Path,
    *,
    older_than_days: int,
    dry_run: bool,
) -> dict[str, int]:
    """Remove log files older than *older_than_days*.

    Scans *logs_root* recursively for ``*.log`` and ``*.jsonl`` files and
    removes those whose mtime predates the cutoff.

    Returns ``{"removed": N, "bytes_freed": B, "errors": E}``.
    """
    counts: dict[str, int] = {"removed": 0, "bytes_freed": 0, "errors": 0}

    if not logs_root.exists():
        progress(f"logs directory not found: {logs_root} — nothing to clean")
        return counts

    cutoff = time.time() - older_than_days * _SECS_PER_DAY
    patterns = ("*.log", "*.jsonl")

    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(logs_root.rglob(pattern))
    candidates.sort()

    for entry in candidates:
        if not entry.is_file():
            continue
        if _mtime(entry) >= cutoff:
            continue

        size = _file_size(entry)
        if dry_run:
            print(f"  [dry-run] would remove log: {entry}  ({_format_bytes(size)})")
            counts["removed"] += 1
            counts["bytes_freed"] += size
        else:
            try:
                entry.unlink()
                counts["removed"] += 1
                counts["bytes_freed"] += size
            except OSError as exc:
                warn(f"could not remove {entry}: {exc}")
                counts["errors"] += 1

    return counts


def cleanup_db(
    db_path: Path,
    *,
    older_than_days: int,
    dry_run: bool,
) -> dict[str, int]:
    """Vacuum the SQLite state.db and optionally delete old session records.

    The DB file is NEVER deleted.  Only VACUUM is run (when not dry-run)
    plus an optional soft-delete of sessions whose ``updated_at`` is older
    than *older_than_days*.

    Returns ``{"sessions_deleted": N, "vacuum": 1|0}``.
    """
    from lionagi.ln.concurrency import run_async

    counts: dict[str, int] = {"sessions_deleted": 0, "vacuum": 0}

    if not db_path.exists():
        progress(f"state.db not found at {db_path} — skipping DB cleanup")
        return counts

    async def _do_cleanup() -> dict[str, int]:
        import time as _time

        from lionagi.state.db import StateDB

        inner: dict[str, int] = {"sessions_deleted": 0, "vacuum": 0}
        cutoff = _time.time() - older_than_days * _SECS_PER_DAY

        async with StateDB() as db:
            cur = await db.db.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE updated_at < ? OR updated_at IS NULL",
                (cutoff,),
            )
            row = await cur.fetchone()
            eligible = row["n"] if row else 0

            if dry_run:
                print(
                    f"  [dry-run] would delete {eligible} session record(s) "
                    f"older than {older_than_days} day(s) from state.db"
                )
                print("  [dry-run] would run VACUUM on state.db")
                inner["sessions_deleted"] = eligible
            else:
                if eligible:
                    await db.db.execute(
                        "DELETE FROM sessions WHERE updated_at < ? OR updated_at IS NULL",
                        (cutoff,),
                    )
                    # Sweep orphaned messages (not referenced by any progression).
                    await db.db.execute(
                        """DELETE FROM messages
                           WHERE id NOT IN (
                             SELECT value
                             FROM progressions, json_each(progressions.collection)
                           )"""
                    )
                    await db.db.commit()
                    inner["sessions_deleted"] = eligible

                await db.db.execute("VACUUM")
                await db.db.commit()
                inner["vacuum"] = 1

        return inner

    try:
        result = run_async(_do_cleanup())
        counts.update(result)
    except Exception as exc:  # noqa: BLE001
        log_error(f"DB cleanup failed: {exc}")

    return counts


# ── CLI wiring ────────────────────────────────────────────────────────────────


def add_cleanup_subcommand(state_sub: argparse._SubParsersAction) -> None:
    """Register ``li state cleanup`` under the existing ``state`` subparsers."""
    cleanup = state_sub.add_parser(
        "cleanup",
        help="Remove stale run directories, orphaned team files, and old logs.",
        description=(
            "Operational hygiene command: delete old run directories, orphaned "
            "team JSON files, and stale log files. Pass --dry-run to preview "
            "what would be deleted without touching anything."
        ),
    )

    cleanup.add_argument(
        "--older-than",
        metavar="DAYS",
        type=int,
        default=_default_older_than(),
        dest="older_than",
        help="Only remove items older than N days (default: 30).",
    )
    cleanup.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be removed, but do not delete anything.",
    )
    cleanup.add_argument(
        "--runs",
        action="store_true",
        default=False,
        help="Clean old run directories (~/.lionagi/runs/).",
    )
    cleanup.add_argument(
        "--teams",
        action="store_true",
        default=False,
        help="Clean orphaned team files (~/.lionagi/teams/).",
    )
    cleanup.add_argument(
        "--logs",
        action="store_true",
        default=False,
        help="Clean old log files (~/.lionagi/logs/).",
    )
    cleanup.add_argument(
        "--all",
        action="store_true",
        default=False,
        dest="clean_all",
        help="Clean everything (runs + teams + logs + DB vacuum). Default when no scope flag given.",
    )
    cleanup.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation prompt before deleting.",
    )


def run_cleanup(args: argparse.Namespace) -> int:
    """Execute ``li state cleanup``."""
    from lionagi._paths import LIONAGI_HOME

    older_than: int = args.older_than
    dry_run: bool = args.dry_run
    force: bool = args.force

    # Determine scope: if no specific flag is set, default to --all.
    do_runs: bool = args.runs
    do_teams: bool = args.teams
    do_logs: bool = args.logs
    do_all: bool = args.clean_all

    if not (do_runs or do_teams or do_logs or do_all):
        do_all = True

    if do_all:
        do_runs = do_teams = do_logs = True

    runs_root = LIONAGI_HOME / "runs"
    teams_root = LIONAGI_HOME / "teams"
    logs_root = LIONAGI_HOME / "logs"
    db_path = LIONAGI_HOME / "state.db"

    # Confirmation gate (skip in dry-run — nothing actually happens).
    if not dry_run and not force:
        parts: list[str] = []
        if do_runs:
            parts.append(f"run directories older than {older_than}d")
        if do_teams:
            parts.append("orphaned team files")
        if do_logs:
            parts.append(f"log files older than {older_than}d")
        if do_all:
            parts.append("DB vacuum")
        scope = ", ".join(parts)
        if not _ask_confirm(f"This will delete: {scope}.  Continue?"):
            print("aborted.")
            return 0

    total_removed = 0
    total_bytes = 0
    total_errors = 0

    if do_runs:
        progress(f"cleaning run directories older than {older_than} day(s)...")
        r = cleanup_runs(runs_root, older_than_days=older_than, dry_run=dry_run)
        total_removed += r["removed"]
        total_bytes += r["bytes_freed"]
        total_errors += r["errors"]

    if do_teams:
        progress("cleaning orphaned team files...")
        r = cleanup_teams(teams_root, dry_run=dry_run)
        total_removed += r["removed"]
        total_bytes += r["bytes_freed"]
        total_errors += r["errors"]

    if do_logs:
        progress(f"cleaning log files older than {older_than} day(s)...")
        r = cleanup_logs(logs_root, older_than_days=older_than, dry_run=dry_run)
        total_removed += r["removed"]
        total_bytes += r["bytes_freed"]
        total_errors += r["errors"]

    if do_all:
        progress("vacuuming state.db...")
        r = cleanup_db(db_path, older_than_days=older_than, dry_run=dry_run)
        total_removed += r["sessions_deleted"]

    # Summary line.
    prefix = "(dry-run) would remove" if dry_run else "removed"
    print(
        f"{prefix} {total_removed} item(s),  "
        f"space freed: {_format_bytes(total_bytes)}"
        + (f"  [errors: {total_errors}]" if total_errors else "")
    )

    if total_errors:
        hint("some items could not be removed — check warnings above")

    return 0 if total_errors == 0 else 1
