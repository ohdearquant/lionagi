# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li invoke` — skill-level orchestration tracking (opt-in session grouping)."""

from __future__ import annotations

import argparse
import json
import time
import uuid
from typing import Any

from lionagi._auto import CliDeclaration, auto_register

from ._logging import hint, log_error

# ── async helpers ─────────────────────────────────────────────────────────────


async def _start_invocation(
    *,
    skill: str,
    plugin: str | None,
    prompt: str | None,
    metadata: dict | None,
) -> str:
    from lionagi.state.db import StateDB

    inv_id = uuid.uuid4().hex[:12]
    # No pid markers here — invocation is a PID-less umbrella; see
    # docs/internals/cli.md for the recycled-PID hazard this avoids.
    async with StateDB() as db:
        await db.create_invocation(
            {
                "id": inv_id,
                "skill": skill,
                "plugin": plugin,
                "prompt": prompt,
                "started_at": time.time(),
                "status": "running",
                "node_metadata": metadata,
            }
        )
    return inv_id


async def _end_invocation(invocation_id: str, *, status: str, metadata: dict | None) -> dict | None:
    from lionagi.state.db import StateDB
    from lionagi.state.reasons import RunReasons

    reason_by_status = {
        "completed": RunReasons.COMPLETED_OK,
        "failed": RunReasons.FAILED_EXCEPTION,
        "timed_out": RunReasons.TIMED_OUT_DEADLINE,
        "aborted": RunReasons.ABORTED_USER,
        "cancelled": RunReasons.CANCELLED_SYSTEM,
    }

    async with StateDB() as db:
        existing = await db.get_invocation(invocation_id)
        if existing is None:
            return None
        await db.update_status(
            "invocation",
            invocation_id,
            new_status=status,
            reason_code=reason_by_status[status],
            reason_summary=f"Invocation {status}.",
            source="executor",
            actor=invocation_id,
            extra_fields={"ended_at": time.time()},
        )
        if metadata is not None:
            # Merge: preserve any metadata the skill wrote during the
            # run, overwrite per-key with the closer's payload.
            current = existing.get("node_metadata") or {}
            if isinstance(current, str):
                try:
                    current = json.loads(current)
                except json.JSONDecodeError:
                    current = {}
            await db.update_invocation(
                invocation_id,
                node_metadata={**current, **metadata},
            )
        return await db.get_invocation(invocation_id)


async def _list_invocations(*, skill: str | None, status: str | None, limit: int) -> list[dict]:
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        return await db.list_invocations(skill=skill, status=status, limit=limit)


# ── parser + dispatch ────────────────────────────────────────────────────────


def add_invoke_subparser(subparsers: argparse._SubParsersAction) -> None:
    invoke = subparsers.add_parser(
        "invoke",
        help="Track a skill-level orchestration.",
        description=(
            "Group sessions spawned by a skill (e.g. /show, /codex-pr-review) "
            "into a single parent invocation record. Opt-in: sessions spawned "
            "without --invocation continue to work exactly as before."
        ),
    )
    inv_sub = invoke.add_subparsers(dest="invoke_command", required=True)

    start = inv_sub.add_parser("start", help="Open a new invocation. Prints the id to stdout.")
    start.add_argument(
        "--skill",
        required=True,
        help=(
            "Name of the orchestration being recorded, e.g. 'show', 'codex-pr-review' or "
            "'reprompt'. Every session spawned under this id groups by it."
        ),
    )
    start.add_argument(
        "--plugin",
        default=None,
        help="Marketplace plugin packaging that skill, when the skill came from one.",
    )
    start.add_argument(
        "--prompt",
        default=None,
        help="The request that triggered this work, stored as free text for later reading.",
    )
    start.add_argument(
        "--metadata",
        default=None,
        help=(
            "Skill-specific JSON to attach (e.g. show plan, review rounds). "
            "Stored verbatim; rendered as-is on the invocation detail page."
        ),
    )

    end = inv_sub.add_parser("end", help="Close an invocation.")
    end.add_argument(
        "invocation_id",
        help=(
            "The id printed by `li invoke start`. An invocation never closed stays 'running' "
            "forever."
        ),
    )
    end.add_argument(
        "--status",
        default="completed",
        choices=[
            "completed",
            "failed",
            "timed_out",
            "aborted",
            "cancelled",
        ],
        help=(
            "How the work ended, which also fixes the reason stored with it: 'completed' for "
            "success, 'failed' for an exception, 'timed_out' for a deadline, 'aborted' for a "
            "user interrupt, 'cancelled' for a system cancellation. Later listings and "
            "dashboards filter on this, so leaving the default on work that did not succeed "
            "records it as a success."
        ),
    )
    end.add_argument(
        "--metadata",
        default=None,
        help=(
            "JSON merged key-by-key into the invocation's existing metadata, so anything written "
            "during the run survives unless this overwrites that key."
        ),
    )

    ls = inv_sub.add_parser("list", help="List recent invocations.")
    ls.add_argument(
        "--skill",
        default=None,
        help="Show only invocations of this skill name, matched exactly.",
    )
    ls.add_argument(
        "--status",
        default=None,
        help=(
            "Show only invocations in this status: running, completed, failed, timed_out, "
            "aborted or cancelled."
        ),
    )
    ls.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum rows to print, newest first (default 20).",
    )


def _parse_metadata(raw: str | None) -> dict | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--metadata: invalid JSON ({exc})") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("--metadata must be a JSON object")
    return parsed


@auto_register(
    area="invoke", cli=CliDeclaration(seed="invoke", parser_factory=add_invoke_subparser)
)
def run_invoke(args: argparse.Namespace) -> int:
    from lionagi.ln.concurrency import run_async

    if args.invoke_command == "start":
        try:
            metadata = _parse_metadata(args.metadata)
        except SystemExit as exc:
            log_error(str(exc))
            return 1
        inv_id = run_async(
            _start_invocation(
                skill=args.skill,
                plugin=args.plugin,
                prompt=args.prompt,
                metadata=metadata,
            )
        )
        # The id is the contract — print it on its own line to stdout so
        # `$(li invoke start ...)` captures cleanly.
        print(inv_id)
        return 0

    if args.invoke_command == "end":
        try:
            metadata = _parse_metadata(args.metadata)
        except SystemExit as exc:
            log_error(str(exc))
            return 1
        result = run_async(
            _end_invocation(args.invocation_id, status=args.status, metadata=metadata)
        )
        if result is None:
            log_error(f"invocation not found: {args.invocation_id}")
            return 1
        print(f"{args.invocation_id}: {result['status']} ({result['session_count']} session(s))")
        return 0

    if args.invoke_command == "list":
        rows = run_async(_list_invocations(skill=args.skill, status=args.status, limit=args.limit))
        if not rows:
            hint("(no invocations)")
            return 0
        for r in rows:
            prompt = (r.get("prompt") or "").replace("\n", " ")[:60]
            print(
                f"{r['id']}  {r['skill']:<20}  {r['status']:<10}  {r['session_count']:>3}  {prompt}"
            )
        return 0

    return 1


# ── machine result ────────────────────────────────────────────────────────────

# What a listing reports about one invocation. `node_metadata` is deliberately
# not here: the store holds it as a JSON document for some rows and as the text
# of one for others, so a caller could not tell which it had been handed without
# guessing, and a listing is not where that ambiguity should be settled.
_INVOCATION_FIELDS = (
    "id",
    "skill",
    "plugin",
    "status",
    "prompt",
    "started_at",
    "ended_at",
    "updated_at",
    "session_count",
    "project",
    "project_source",
)


async def _machine_list_data(
    *, skill: str | None, status: str | None, limit: int
) -> dict[str, Any]:
    from .machine import available, readonly_state_db

    result: dict[str, Any] = {
        "filters": {"skill": skill, "status": status},
        "limit": limit,
    }
    async with readonly_state_db() as (db, why):
        if db is None:
            result["invocations"] = why
            return result
        rows = await db.list_invocations(skill=skill, status=status, limit=limit)
    result["invocations"] = available(
        [{field: row.get(field) for field in _INVOCATION_FIELDS} for row in rows]
    )
    return result


def _machine_list(argv: list[str]) -> dict[str, Any]:
    from lionagi.ln.concurrency import run_async

    from .machine import MachineError, machine_parser, parse_machine_argv

    parser = machine_parser("li invoke list")
    parser.add_argument("--skill", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--status", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--limit", type=int, default=20, help=argparse.SUPPRESS)
    args = parse_machine_argv(parser, argv)
    if args.limit < 1:
        raise MachineError("invalid_input", "--limit must be at least 1")
    return run_async(_machine_list_data(skill=args.skill, status=args.status, limit=args.limit))


def machine_result(argv: list[str]) -> dict[str, Any]:
    """`li invoke <sub> --machine`."""
    from .machine import machine_subcommand

    return machine_subcommand(
        "invoke",
        argv,
        {"list": _machine_list},
        without_seam={
            "start": "it opens an invocation record, which is a write to the store",
            "end": "it closes an invocation record, which is a write to the store",
        },
    )
