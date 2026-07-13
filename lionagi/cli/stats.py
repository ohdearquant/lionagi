# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li stats` — read-only aggregate reporting over lionagi's StateDB."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from typing import Any

from .monitor import _since_timestamp

__all__ = (
    "GROUP_BY_COLUMNS",
    "add_stats_subparser",
    "run_stats",
)

# --group-by KEY -> sessions column. Closed vocabulary; see _query_run_stats.
GROUP_BY_COLUMNS: dict[str, str] = {
    "project": "project",
    "kind": "invocation_kind",
    "agent": "agent_name",
    "model": "model",
    "status": "status",
}

_DEFAULT_GROUP_BY = "project,kind"


def _validate_group_by(raw: str) -> list[str]:
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    valid = ", ".join(GROUP_BY_COLUMNS)
    if not keys:
        raise ValueError(f"--group-by must name at least one key. Valid keys: {valid}")
    invalid = [k for k in keys if k not in GROUP_BY_COLUMNS]
    if invalid:
        raise ValueError(f"Unknown --group-by key(s): {', '.join(invalid)}. Valid keys: {valid}")
    return keys


def _reject_non_positive_since(window: str) -> None:
    """Reject a `--since` window that isn't strictly positive.

    See docs/internals/cli.md for why this tightens Monitor's shared parser.
    """
    try:
        value = int(window[:-1])
    except (ValueError, IndexError):
        return
    if value <= 0:
        raise ValueError(f"--since window must be positive; got {window!r}. Format: 30m, 1h, 7d.")


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


async def _query_run_stats(
    db: Any,
    *,
    since: float,
    group_by: list[str],
) -> list[dict[str, Any]]:
    """Aggregate sessions rows updated within the window, grouped by the requested keys.

    See docs/internals/cli.md for the group_by validate-before-interpolate contract.
    """
    select_cols = ", ".join(f"{GROUP_BY_COLUMNS[k]} AS {k}" for k in group_by)
    group_cols = ", ".join(GROUP_BY_COLUMNS[k] for k in group_by)
    query = (
        f"SELECT {select_cols}, "  # noqa: S608
        "COUNT(*) AS run_count, "
        "SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed, "
        "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed, "
        "MIN(started_at) AS first_at, "
        "MAX(started_at) AS last_at "
        "FROM sessions WHERE updated_at >= ? "
        f"GROUP BY {group_cols} "
        f"ORDER BY {group_cols}"
    )
    return await db.fetch_all(query, [since])


async def _run_stats_runs(*, since: float, group_by: list[str]) -> list[dict[str, Any]]:
    from lionagi.state.db import DEFAULT_DB_PATH, StateDB

    if not DEFAULT_DB_PATH.exists():
        return []
    # readonly=True: a reporting command must never write to the DB it
    # reports on, even implicitly via schema-reconcile. See docs/internals/cli.md.
    async with StateDB(readonly=True) as db:
        return await _query_run_stats(db, since=since, group_by=group_by)


def _rows_for_json(rows: list[dict[str, Any]], group_by: list[str]) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        out.append(
            {
                **{k: row.get(k) for k in group_by},
                "run_count": row["run_count"],
                "completed": row["completed"] or 0,
                "failed": row["failed"] or 0,
                "first_at": _iso(row["first_at"]),
                "last_at": _iso(row["last_at"]),
            }
        )
    return out


def _format_stats_table(rows: list[dict[str, Any]], group_by: list[str]) -> str:
    if not rows:
        return "(no runs in this window)"

    headers = [k.upper() for k in group_by] + [
        "RUN_COUNT",
        "COMPLETED",
        "FAILED",
        "FIRST_AT",
        "LAST_AT",
    ]
    data_rows: list[list[str]] = []
    for row in rows:
        values = [str(row.get(k)) if row.get(k) not in (None, "") else "(none)" for k in group_by]
        values.append(str(row["run_count"]))
        values.append(str(row["completed"] or 0))
        values.append(str(row["failed"] or 0))
        values.append(_iso(row["first_at"]) or "-")
        values.append(_iso(row["last_at"]) or "-")
        data_rows.append(values)

    widths = [max(len(headers[i]), *(len(r[i]) for r in data_rows)) for i in range(len(headers))]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True))]
    lines.append("  ".join("-" * w for w in widths))
    for r in data_rows:
        lines.append("  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)))
    return "\n".join(lines)


def add_stats_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register `li stats` with argparse."""
    stats = subparsers.add_parser(
        "stats",
        help="Read-only aggregate reporting over lionagi's StateDB.",
        description="Aggregate views over state.db (the same substrate `li monitor` reads).",
    )
    stats_sub = stats.add_subparsers(dest="stats_command", required=True)

    runs = stats_sub.add_parser(
        "runs",
        help="Aggregate run counts by project/kind/agent/model/status.",
        description=(
            "Aggregate sessions rows updated within --since into run_count, "
            "completed, and failed counts per group. Read-only: no writes, "
            "no schema changes, no PRAGMA mutations."
        ),
    )
    runs.add_argument(
        "--since",
        default="7d",
        metavar="WINDOW",
        help="Only include runs updated within this window. Format: 30m, 1h, 7d. Default: 7d.",
    )
    runs.add_argument(
        "--group-by",
        default=_DEFAULT_GROUP_BY,
        metavar="KEY[,KEY...]",
        help=(
            "Comma-separated group keys from "
            f"{{{', '.join(GROUP_BY_COLUMNS)}}}. Default: {_DEFAULT_GROUP_BY}."
        ),
    )
    runs.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON array of row objects instead of an aligned table.",
    )


def run_stats(args: argparse.Namespace) -> int:
    """Dispatch `li stats` subcommand."""
    from lionagi.ln.concurrency import run_async

    from ._logging import log_error

    if args.stats_command != "runs":
        return 1

    try:
        group_by = _validate_group_by(args.group_by)
    except ValueError as exc:
        log_error(str(exc))
        return 2

    try:
        _reject_non_positive_since(args.since)
        since = _since_timestamp(args.since)
    except ValueError as exc:
        log_error(str(exc))
        return 2

    rows = run_async(_run_stats_runs(since=since, group_by=group_by))

    if args.json:
        print(json.dumps(_rows_for_json(rows, group_by)))
    else:
        print(_format_stats_table(rows, group_by))
    return 0
