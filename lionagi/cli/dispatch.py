# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li dispatch` — inspect and acknowledge durable dispatch_outbox rows (ADR-0092).

Enqueue is not a CLI verb here: dispatches are produced by schedule actions and
the delivery loop, both already running inside the daemon process. The
read/ack verbs below follow `li monitor`'s direct-DB-read discipline (not
`li schedule`'s daemon-HTTP-only discipline): if `li dispatch ack` required the
daemon to be up, a daemon restart window would strand acks, defeating the
point of a durable outbox. Every write here is a single-row guarded
compare-and-swap inside `BEGIN IMMEDIATE` (via `StateDB._tx()` /
`lionagi.state.transitions.transition()`).
"""

from __future__ import annotations

import argparse
import time

__all__ = (
    "add_dispatch_subparser",
    "run_dispatch",
)


def _format_time(ts: float | None) -> str:
    if ts is None:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


async def _cmd_ls(*, status: str | None, limit: int) -> int:
    from lionagi.dispatch import list_dispatches
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        rows = await list_dispatches(db, status=status, limit=limit)

    if not rows:
        print("(no dispatches)")
        return 0

    header = f"{'ID':<32}  {'KIND':<16}  {'DELIVER_TO':<20}  {'STATUS':<12}  {'ATTEMPT':>7}  {'CREATED':<20}"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['id']:<32}  {row['kind']:<16}  {row['deliver_to']:<20}  "
            f"{row['status']:<12}  {row['attempt']:>7}  {_format_time(row['created_at']):<20}"
        )
    return 0


async def _cmd_show(dispatch_id: str) -> int:
    import json

    from lionagi.dispatch import get_dispatch
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        row = await get_dispatch(db, dispatch_id)

    if row is None:
        print(f"dispatch not found: {dispatch_id}")
        return 1

    for key, value in row.items():
        if key == "payload":
            print(f"{key}: {json.dumps(value, indent=2)}")
        else:
            print(f"{key}: {value}")
    return 0


async def _cmd_ack(dispatch_id: str, ack_token: str) -> int:
    from lionagi.dispatch import ack_dispatch
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        applied = await ack_dispatch(db, dispatch_id, ack_token)

    if applied:
        print(f"acked {dispatch_id}")
        return 0
    print(f"ack rejected for {dispatch_id} (status changed concurrently)")
    return 1


async def _cmd_retry(dispatch_id: str) -> int:
    from lionagi.dispatch import retry_dispatch
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        applied = await retry_dispatch(db, dispatch_id)

    if applied:
        print(f"retrying {dispatch_id}")
        return 0
    print(f"retry rejected for {dispatch_id} (status changed concurrently)")
    return 1


async def _cmd_purge(dispatch_id: str) -> int:
    from lionagi.dispatch import purge_dispatch
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        deleted = await purge_dispatch(db, dispatch_id, actor="li_dispatch_purge")

    if deleted:
        print(f"purged {dispatch_id}")
        return 0
    print(f"dispatch not found: {dispatch_id}")
    return 1


async def _cmd_purge_bulk(*, status: str | None, before: float | None, dry_run: bool) -> int:
    from lionagi.dispatch import purge_dispatches
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        result = await purge_dispatches(
            db, status=status, before=before, dry_run=dry_run, actor="li_dispatch_purge"
        )

    verb = "would purge" if dry_run else "purged"
    by_status = {k: v for k, v in result.items() if k not in ("total", "dry_run")}
    detail = ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())) or "(none matched)"
    print(f"{verb} {result['total']} dispatch(es): {detail}")
    return 0


def add_dispatch_subparser(subparsers: argparse._SubParsersAction) -> None:
    dispatch = subparsers.add_parser(
        "dispatch",
        help="Inspect and acknowledge durable dispatch_outbox rows.",
        description=(
            "Read and acknowledge rows in the durable dispatch outbox (ADR-0092). "
            "Dispatches are enqueued by schedule actions and delivered by the "
            "Studio daemon's scheduler tick; there is no `enqueue` verb here."
        ),
    )
    dispatch_sub = dispatch.add_subparsers(dest="dispatch_command", required=True)

    ls = dispatch_sub.add_parser("ls", help="List dispatches.")
    ls.add_argument("--status", default=None, help="Filter by status.")
    ls.add_argument("--limit", type=int, default=50, help="Max rows (default 50).")

    show = dispatch_sub.add_parser("show", help="Show one dispatch in full.")
    show.add_argument("id", help="Dispatch id.")

    ack = dispatch_sub.add_parser("ack", help="Acknowledge an ack_required dispatch.")
    ack.add_argument("id", help="Dispatch id.")
    ack.add_argument("token", help="ack_token presented by the consumer.")

    retry = dispatch_sub.add_parser(
        "retry",
        help="Force an immediate retry of a dead_letter/expired dispatch.",
    )
    retry.add_argument("id", help="Dispatch id.")

    purge = dispatch_sub.add_parser(
        "purge",
        help="Delete a dispatch row, or bulk-delete by criteria.",
        description=(
            "With ID: delete that one row (any status), auditable via admin_events "
            "action=dispatch_purge. Without ID: bulk-delete by --status/--before "
            "(at least one required, so a bare `purge` cannot mass-delete); "
            "--dry-run reports counts without deleting. An explicit --status is "
            "honored exactly as given, including pending/delivering (naming an "
            "in-flight status is deliberate operator intent). A bare --before with "
            "no --status is scoped to terminal statuses only "
            "(delivered/acked/dead_letter/expired) and never touches "
            "pending/delivering rows."
        ),
    )
    purge.add_argument("id", nargs="?", default=None, help="Dispatch id (single-row purge).")
    purge.add_argument(
        "--status",
        default=None,
        help=(
            "Bulk purge: match this status exactly, including pending/delivering "
            "(explicit status is deliberate operator intent)."
        ),
    )
    purge.add_argument(
        "--before",
        type=float,
        default=None,
        help=(
            "Bulk purge: match rows with updated_at <= this epoch-seconds value. "
            "Without --status, this is scoped to terminal statuses only "
            "(delivered/acked/dead_letter/expired) and never sweeps "
            "pending/delivering rows."
        ),
    )
    purge.add_argument(
        "--dry-run",
        action="store_true",
        help="Bulk purge: report what would be deleted without deleting.",
    )


def run_dispatch(args: argparse.Namespace) -> int:
    from lionagi.ln.concurrency import run_async

    if args.dispatch_command == "ls":
        return run_async(_cmd_ls(status=args.status, limit=args.limit))
    if args.dispatch_command == "show":
        return run_async(_cmd_show(args.id))
    if args.dispatch_command == "ack":
        return run_async(_cmd_ack(args.id, args.token))
    if args.dispatch_command == "retry":
        return run_async(_cmd_retry(args.id))
    if args.dispatch_command == "purge":
        if args.id is not None:
            return run_async(_cmd_purge(args.id))
        if args.status is None and args.before is None:
            print("purge: specify an id, or --status/--before for a bulk purge")
            return 2
        return run_async(
            _cmd_purge_bulk(status=args.status, before=args.before, dry_run=args.dry_run)
        )
    return 1
