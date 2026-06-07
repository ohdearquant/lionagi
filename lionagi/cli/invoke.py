# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li invoke` — ADR-0020 skill-level orchestration tracking.

Skills (Claude Code markdown files like ``/show`` or ``/codex-pr-review``)
spawn many sessions over minutes-to-hours. ``li invoke`` is the
opt-in handshake that lets them group those sessions into a single
parent record so the Studio dashboard and the runs list can collapse
"14 sessions" into one ``/show "resolve issues"`` row.

Usage::

    INV=$(li invoke start --skill show --prompt "resolve lionagi issues")
    li play backend ... --invocation "$INV"
    li play frontend ... --invocation "$INV"
    li invoke end "$INV" --status completed

Without ``--invocation`` the spawned sessions have ``invocation_id = NULL``,
the same behavior as before this command existed.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

from ._logging import log_error

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

    async with StateDB() as db:
        existing = await db.get_invocation(invocation_id)
        if existing is None:
            return None
        fields: dict = {"status": status, "ended_at": time.time()}
        if metadata is not None:
            # Merge: preserve any metadata the skill wrote during the
            # run, overwrite per-key with the closer's payload.
            current = existing.get("node_metadata") or {}
            if isinstance(current, str):
                try:
                    current = json.loads(current)
                except json.JSONDecodeError:
                    current = {}
            fields["node_metadata"] = {**current, **metadata}
        await db.update_invocation(invocation_id, **fields)
        return await db.get_invocation(invocation_id)


async def _list_invocations(*, skill: str | None, status: str | None, limit: int) -> list[dict]:
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        return await db.list_invocations(skill=skill, status=status, limit=limit)


# ── parser + dispatch ────────────────────────────────────────────────────────


def add_invoke_subparser(subparsers: argparse._SubParsersAction) -> None:
    invoke = subparsers.add_parser(
        "invoke",
        help="Track a skill-level orchestration (ADR-0020).",
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
        help="Skill name: 'show', 'codex-pr-review', 'reprompt', etc.",
    )
    start.add_argument(
        "--plugin",
        default=None,
        help="Marketplace plugin packaging the skill (optional).",
    )
    start.add_argument(
        "--prompt",
        default=None,
        help="The user's input that triggered the skill (free text).",
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
    end.add_argument("invocation_id", help="The id printed by `li invoke start`.")
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
        help="Terminal status (ADR-0025 vocabulary).",
    )
    end.add_argument(
        "--metadata",
        default=None,
        help="Optional JSON to merge into the invocation's node_metadata.",
    )

    ls = inv_sub.add_parser("list", help="List recent invocations.")
    ls.add_argument("--skill", default=None, help="Filter by skill name.")
    ls.add_argument("--status", default=None, help="Filter by status (one of the 6 values).")
    ls.add_argument("--limit", type=int, default=20, help="Max rows to print (default 20).")


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
            print("(no invocations)", file=sys.stderr)
            return 0
        for r in rows:
            prompt = (r.get("prompt") or "").replace("\n", " ")[:60]
            print(
                f"{r['id']}  {r['skill']:<20}  {r['status']:<10}  {r['session_count']:>3}  {prompt}"
            )
        return 0

    return 1
