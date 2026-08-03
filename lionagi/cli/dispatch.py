# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li dispatch` — inspect and acknowledge durable dispatch_outbox rows (ADR-0059).

See docs/internals/cli.md for why enqueue isn't a CLI verb and the CAS write discipline.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

__all__ = (
    "add_dispatch_subparser",
    "run_dispatch",
    "machine_result",
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


async def _ack_outcome(db: Any, dispatch_id: str, ack_token: str) -> dict[str, Any]:
    """What acknowledging this row did, as data rather than as a printed line.

    The write itself stays the library's: this classifies its refusal instead of
    pre-checking the same conditions, so there is no second copy of the
    preconditions here to drift from the ones that are enforced.

    Classification is a re-read, not a parse of the exception message. The
    message is prose for a person, and a reword would silently move a caller
    from one outcome to another.
    """
    from lionagi.dispatch import ack_dispatch, get_dispatch

    try:
        applied = await ack_dispatch(db, dispatch_id, ack_token)
    except LookupError:
        return {"outcome": "not_found"}
    except ValueError:
        row = await get_dispatch(db, dispatch_id)
        if row is None:
            # Deleted between the library's read and this one.
            return {"outcome": "not_found"}
        if not row["ack_required"]:
            return {"outcome": "not_ack_required", "status": row["status"]}
        return {"outcome": "token_mismatch", "status": row["status"]}

    if applied:
        return {"outcome": "acked"}
    # Re-read the row's current status rather than describe the refusal: a row
    # gone by re-read time is reported as `not_found`, not `status_changed`
    # with a null status, so the two cases stay distinguishable to the caller.
    row = await get_dispatch(db, dispatch_id)
    if row is None:
        return {"outcome": "not_found"}
    return {"outcome": "status_changed", "status": row["status"]}


async def _retry_outcome(db: Any, dispatch_id: str) -> dict[str, Any]:
    """What forcing a retry of this row did. Same discipline as _ack_outcome."""
    from lionagi.dispatch import get_dispatch, retry_dispatch

    try:
        applied = await retry_dispatch(db, dispatch_id)
    except LookupError:
        return {"outcome": "not_found"}
    except ValueError:
        row = await get_dispatch(db, dispatch_id)
        if row is None:
            return {"outcome": "not_found"}
        return {"outcome": "not_retryable", "status": row["status"]}

    if applied:
        return {"outcome": "retrying"}
    row = await get_dispatch(db, dispatch_id)
    if row is None:
        return {"outcome": "not_found"}
    return {"outcome": "status_changed", "status": row["status"]}


# The human paths below deliberately keep raising on a precondition the library
# refuses: a mismatched ack_token reaches a person as a traceback naming the
# reason, which is a poor report but is the behaviour this command has and is
# asserted as such. Making it a printed line is a change to this surface, not
# part of giving the machine channel a seam, so it is left alone here.
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


async def _cmd_purge(dispatch_id: str, *, dry_run: bool = False) -> int:
    from lionagi.dispatch import get_dispatch, purge_dispatch
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        if dry_run:
            row = await get_dispatch(db, dispatch_id)
            if row is None:
                print(f"dispatch not found: {dispatch_id}")
                return 1
            await db.insert_admin_event(
                action="dispatch_purge",
                target_id=dispatch_id,
                details={
                    "dispatch_id": dispatch_id,
                    "dry_run": True,
                    "status": row["status"],
                    "total": 1,
                },
                actor="li_dispatch_purge",
            )
            print(f"would purge {dispatch_id} (status={row['status']})")
            return 0
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
            "Read and acknowledge rows in the durable dispatch outbox (ADR-0059 D6). "
            "Dispatches are enqueued by schedule actions and delivered by the "
            "Studio daemon's scheduler tick; there is no `enqueue` verb here."
        ),
    )
    dispatch_sub = dispatch.add_subparsers(dest="dispatch_command", required=True)

    ls = dispatch_sub.add_parser("ls", help="List dispatches.")
    ls.add_argument(
        "--status",
        default=None,
        help=(
            "Show only rows in this status: pending, delivering, delivered, acked, dead_letter "
            "or expired. Omit to list every status."
        ),
    )
    ls.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Maximum rows to print, newest first (default 50).",
    )

    show = dispatch_sub.add_parser("show", help="Show one dispatch in full.")
    show.add_argument("id", help="Id of the dispatch row to show, as listed by `li dispatch ls`.")

    ack = dispatch_sub.add_parser("ack", help="Acknowledge an ack_required dispatch.")
    ack.add_argument("id", help="Id of the dispatch to acknowledge, as listed by `li dispatch ls`.")
    ack.add_argument(
        "token",
        help=(
            "The ack_token this dispatch was delivered with, reported by `li dispatch show`. A "
            "mismatched token is refused, so an acknowledgement cannot be guessed."
        ),
    )

    retry = dispatch_sub.add_parser(
        "retry",
        help="Force an immediate retry of a dead_letter/expired dispatch.",
    )
    retry.add_argument("id", help="Id of the dispatch to re-queue for delivery.")

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
    purge.add_argument(
        "id",
        nargs="?",
        default=None,
        help=(
            "Id of a single dispatch to delete, whatever its status. Omit it to bulk-delete by "
            "--status/--before instead; one of the two is then required."
        ),
    )
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
        help="Report what would be deleted without deleting (single-row and bulk).",
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
            return run_async(_cmd_purge(args.id, dry_run=args.dry_run))
        if args.status is None and args.before is None:
            print("purge: specify an id, or --status/--before for a bulk purge")
            return 2
        return run_async(
            _cmd_purge_bulk(status=args.status, before=args.before, dry_run=args.dry_run)
        )
    return 1


# ── machine result ────────────────────────────────────────────────────────────

# A listing reports the routing and delivery bookkeeping of each row. The
# payload itself is not here: it is unbounded caller data and a listing of fifty
# rows would carry fifty of them. `dispatch show` names one row and returns it.
_LIST_FIELDS = (
    "id",
    "kind",
    "deliver_to",
    "dedup_key",
    "status",
    "attempt",
    "max_attempts",
    "next_attempt_at",
    "ack_required",
    "session_id",
    "schedule_run_id",
    "last_error",
    "created_at",
    "expires_at",
    "updated_at",
)

# One row in full, plus the payload. `ack_token` is included: it is the value a
# caller needs to acknowledge the row, `li dispatch show` is where a human reads
# it, and withholding it here while printing it there would be a difference
# between the two surfaces that neither one states.
_SHOW_FIELDS = (*_LIST_FIELDS, "ack_token", "payload")


async def _machine_ls_data(*, status: str | None, limit: int) -> dict[str, Any]:
    from lionagi.dispatch import list_dispatches

    from .machine import available, readonly_state_db

    result: dict[str, Any] = {"filters": {"status": status}, "limit": limit}
    async with readonly_state_db() as (db, why):
        if db is None:
            result["dispatches"] = why
            return result
        rows = await list_dispatches(db, status=status, limit=limit)
    result["dispatches"] = available(
        [{field: row.get(field) for field in _LIST_FIELDS} for row in rows]
    )
    return result


async def _machine_show_data(dispatch_id: str) -> dict[str, Any]:
    from lionagi.dispatch import get_dispatch

    from .machine import MachineError, readonly_state_db, store_unreachable

    async with readonly_state_db() as (db, why):
        if db is None:
            raise store_unreachable(why, f"no dispatch {dispatch_id!r}")
        row = await get_dispatch(db, dispatch_id)
    if row is None:
        raise MachineError("not_found", f"no dispatch with id {dispatch_id!r}")
    return {"dispatch": {field: row.get(field) for field in _SHOW_FIELDS}}


def _machine_ls(argv: list[str]) -> dict[str, Any]:
    from lionagi.ln.concurrency import run_async

    from .machine import MachineError, machine_parser, parse_machine_argv

    parser = machine_parser("li dispatch ls")
    parser.add_argument("--status", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--limit", type=int, default=50, help=argparse.SUPPRESS)
    args = parse_machine_argv(parser, argv)
    if args.limit < 1:
        raise MachineError("invalid_input", "--limit must be at least 1")
    return run_async(_machine_ls_data(status=args.status, limit=args.limit))


def _machine_show(argv: list[str]) -> dict[str, Any]:
    from lionagi.ln.concurrency import run_async

    from .machine import machine_parser, parse_machine_argv

    parser = machine_parser("li dispatch show")
    parser.add_argument("id", help=argparse.SUPPRESS)
    args = parse_machine_argv(parser, argv)
    return run_async(_machine_show_data(args.id))


# ── the writes ────────────────────────────────────────────────────────────────


@asynccontextmanager
async def _writable_state_db() -> AsyncIterator[Any]:
    """The store, open for writing, or a refusal naming why it could not be.

    A write has no availability wrapper to report unavailability inside: it
    either happened or it did not, so an unreachable store is a refusal rather
    than a reported field. The absent case is `not_found` and says so
    definitively — with no store there is no such row to act on — while a store
    that exists and will not open says nothing about what it holds and must not
    be reported as an absent row.

    The guard covers the open alone. A failure inside the caller's body is that
    body's bug and surfaces as the crash it is.
    """
    from lionagi.state.db import StateDB, state_db_known_absent
    from lionagi.state.engine import mask_credentials, mask_db_url

    from .machine import MachineError

    # Asked of the configured store, not the default path: the open honours
    # LIONAGI_STATE_DB_URL, so consulting the file would answer about a store
    # this command would not have written to.
    if state_db_known_absent():
        raise MachineError(
            "not_found",
            f"{mask_db_url(StateDB().url)} does not exist; there are no dispatches to act on",
        )
    async with AsyncExitStack() as stack:
        try:
            db = await stack.enter_async_context(StateDB())
            # The engine connects on the first statement, so without this a
            # refusal would surface mid-write, where it is indistinguishable
            # from the write itself being wrong.
            await db.fetch_all("SELECT 1")
        except MachineError:
            raise
        except Exception as exc:  # noqa: BLE001 — an unopenable store is a refusal, not a crash
            # The message goes in `detail` rather than into the refusal text,
            # so what a reader parses today is unchanged and the discriminator
            # between two failures sharing a type is still there. Masked on
            # both channels: the message can quote the store as readily as the
            # URL field does.
            raise MachineError(
                "unavailable",
                f"{mask_db_url(StateDB().url)}: {type(exc).__name__}",
                {"cause": mask_credentials(str(exc))},
            ) from exc
        yield db


# What each refusing outcome is, as a contract error. A lost race is a `conflict`
# rather than a success carrying `acked: false`, because a caller that branches on
# `ok` alone must not read "the row moved under me" as "done" — the one direction
# of this that is unsafe.
_ACK_REFUSALS: dict[str, tuple[str, str]] = {
    "not_found": ("not_found", "no dispatch with id {id!r}"),
    "not_ack_required": (
        "conflict",
        "dispatch {id!r} does not require ack (ack_required is not set)",
    ),
    # The caller's own argument was wrong, which is input, not state. The expected
    # token is not echoed: `dispatch.show` is where a caller reads it, and an error
    # that hands it over turns a failed attempt into a successful one.
    "token_mismatch": ("invalid_input", "the ack_token given for {id!r} does not match"),
    "status_changed": (
        "conflict",
        "dispatch {id!r} left the status it was read in before the acknowledgement "
        "was applied; it is now {status!r}",
    ),
}

_RETRY_REFUSALS: dict[str, tuple[str, str]] = {
    "not_found": ("not_found", "no dispatch with id {id!r}"),
    "not_retryable": (
        "conflict",
        "dispatch {id!r} is {status!r}; retry applies to dead_letter or expired rows",
    ),
    "status_changed": (
        "conflict",
        "dispatch {id!r} left the status it was read in before the retry was applied; "
        "it is now {status!r}",
    ),
}


def _refuse(refusals: dict[str, tuple[str, str]], outcome: dict[str, Any], dispatch_id: str):
    from .machine import MachineError

    kind, template = refusals[outcome["outcome"]]
    detail: dict[str, Any] = {"dispatch_id": dispatch_id, "outcome": outcome["outcome"]}
    # A row that is gone has no status, and carrying a null one would say it has a
    # status that could not be determined. The two are different answers, and the
    # caller reading this detail is the one that cannot tell them apart.
    if "status" in outcome:
        detail["status"] = outcome["status"]
    return MachineError(
        kind,
        template.format(id=dispatch_id, status=outcome.get("status")),
        detail,
    )


async def _machine_ack_data(dispatch_id: str, ack_token: str) -> dict[str, Any]:
    async with _writable_state_db() as db:
        outcome = await _ack_outcome(db, dispatch_id, ack_token)
    if outcome["outcome"] != "acked":
        raise _refuse(_ACK_REFUSALS, outcome, dispatch_id)
    # The transition carries a fixed idempotency key, so presenting the same
    # token again is absorbed rather than doubling anything. Said here because a
    # caller that times out mid-call needs to know retrying is safe.
    return {"dispatch_id": dispatch_id, "acked": True, "status": "acked", "idempotent": True}


async def _machine_retry_data(dispatch_id: str) -> dict[str, Any]:
    async with _writable_state_db() as db:
        outcome = await _retry_outcome(db, dispatch_id)
    if outcome["outcome"] != "retrying":
        raise _refuse(_RETRY_REFUSALS, outcome, dispatch_id)
    # `pending` is what the row is now, not a claim that delivery happened or
    # that a daemon is running to attempt it.
    return {
        "dispatch_id": dispatch_id,
        "requeued": True,
        "status": "pending",
        "attempt": 0,
    }


async def _machine_purge_data(dispatch_id: str, *, dry_run: bool) -> dict[str, Any]:
    from lionagi.dispatch import get_dispatch, purge_dispatch

    from .machine import MachineError

    async with _writable_state_db() as db:
        if dry_run:
            row = await get_dispatch(db, dispatch_id)
            if row is None:
                raise MachineError("not_found", f"no dispatch with id {dispatch_id!r}")
            # The audit row is written for the preview too, matching what the
            # human path records: the question having been asked is itself the
            # thing an operator later wants to find.
            await db.insert_admin_event(
                action="dispatch_purge",
                target_id=dispatch_id,
                details={
                    "dispatch_id": dispatch_id,
                    "dry_run": True,
                    "status": row["status"],
                    "total": 1,
                },
                actor="li_dispatch_purge",
            )
            return {
                "dispatch_id": dispatch_id,
                "purged": False,
                "dry_run": True,
                "would_purge": True,
                "status": row["status"],
            }
        # Read before deleting so the reported status is the one the row had.
        # After the delete there is nothing left to ask.
        row = await get_dispatch(db, dispatch_id)
        deleted = await purge_dispatch(db, dispatch_id, actor="li_dispatch_purge")
    if not deleted:
        raise MachineError("not_found", f"no dispatch with id {dispatch_id!r}")
    return {
        "dispatch_id": dispatch_id,
        "purged": True,
        "dry_run": False,
        "status": None if row is None else row["status"],
    }


def _machine_ack(argv: list[str]) -> dict[str, Any]:
    from lionagi.ln.concurrency import run_async

    from .machine import machine_parser, parse_machine_argv

    parser = machine_parser("li dispatch ack")
    parser.add_argument("id", help=argparse.SUPPRESS)
    parser.add_argument("token", help=argparse.SUPPRESS)
    args = parse_machine_argv(parser, argv)
    return run_async(_machine_ack_data(args.id, args.token))


def _machine_retry(argv: list[str]) -> dict[str, Any]:
    from lionagi.ln.concurrency import run_async

    from .machine import machine_parser, parse_machine_argv

    parser = machine_parser("li dispatch retry")
    parser.add_argument("id", help=argparse.SUPPRESS)
    args = parse_machine_argv(parser, argv)
    return run_async(_machine_retry_data(args.id))


def _machine_purge(argv: list[str]) -> dict[str, Any]:
    from lionagi.ln.concurrency import run_async

    from .machine import CONTRACT_VERSION, MachineError, machine_parser, parse_machine_argv

    parser = machine_parser("li dispatch purge")
    parser.add_argument("id", nargs="?", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--status", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--before", type=float, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    args = parse_machine_argv(parser, argv)

    # Bulk purge is reachable from a terminal and not from here. One id is a
    # deliberate act against a row the caller has read; a criteria sweep deletes
    # rows it never named, and the result would report a count for rows nobody
    # can now inspect. Refused by name rather than ignored, so a caller that
    # passes criteria is told they did not apply instead of watching one row go.
    if args.status is not None or args.before is not None:
        raise MachineError(
            "unavailable",
            f"bulk purge has no machine result in contract version {CONTRACT_VERSION}: "
            "--status/--before delete rows the caller never named. Purge one id at a "
            "time, or run the sweep from a terminal.",
        )
    if args.id is None:
        raise MachineError("invalid_input", "purge needs the id of the dispatch to delete")
    return run_async(_machine_purge_data(args.id, dry_run=args.dry_run))


def machine_result(argv: list[str]) -> dict[str, Any]:
    """`li dispatch <sub> --machine`."""
    from .machine import machine_subcommand

    return machine_subcommand(
        "dispatch",
        argv,
        {
            "ls": _machine_ls,
            "show": _machine_show,
            "ack": _machine_ack,
            "retry": _machine_retry,
            "purge": _machine_purge,
        },
        without_seam={},
    )
