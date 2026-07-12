# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li o ctl pause|resume|msg` — enqueue session_controls rows for a running flow.

Pure writers: resolve the target session (id/invocation id/play id, same
shapes `li o ctl status` accepts) and insert one row into session_controls.
They do not wait for the control to apply — the poller in
cli/orchestrate/flow.py `_execute_dag` is the only consumer; use
`li o ctl status <id>` to check whether it landed.

Only context-mode `msg` is currently supported: the poller appends the message
to shared flow context for operations not yet rendered. Operation-mode messages
are unsupported. See ADR-0069 D1 and D3.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from .._logging import log_error
from ..status import EXIT_UNKNOWN, _resolve_any_target, _resolve_primary_session

__all__ = (
    "run_ctl_pause",
    "run_ctl_resume",
    "run_ctl_msg",
)

# Mirrors status.py's _DB_BUSY_TIMEOUT_S — bounds a single enqueue's total DB
# time so a stuck write fails fast instead of hanging indefinitely.
_DB_BUSY_TIMEOUT_S = 10.0


# Only these session kinds ever run the control poller (`li o flow` sets
# "flow", playbook runs set "play"); anything else has no consumer, so a
# queued control would sit pending forever.
_POLLER_KINDS = frozenset({"flow", "play"})


async def _prefix_is_ambiguous(db: Any, entity_id: str) -> bool:
    """True when a short prefix matches 2+ rows in the first table that
    matches at all (same sessions→invocations→plays order the resolver
    sweeps, so cross-table shadowing stays intentional)."""
    if len(entity_id) >= 36:
        return False
    for table in ("sessions", "invocations", "plays"):
        rows = await db.fetch_all(
            f"SELECT id FROM {table} WHERE id LIKE ? LIMIT 2",  # noqa: S608
            (entity_id + "%",),
        )
        if rows:
            return len(rows) > 1
    return False


async def _resolve_session(db: Any, entity_id: str) -> dict[str, Any] | None:
    """Resolve a session/invocation/play id (or unambiguous prefix) to the
    backing session row, mirroring `li o ctl status`'s generic resolution."""
    target = await _resolve_any_target(db, entity_id)
    if target is None:
        return None
    entity_type, row = target
    return await _resolve_primary_session(db, entity_type, row)


async def _enqueue_control_inner(
    *, entity_id: str, verb: str, payload: dict[str, Any] | None
) -> tuple[str, int]:
    from lionagi.state.db import DEFAULT_DB_PATH, StateDB

    if not DEFAULT_DB_PATH.exists():
        return "state.db not found — no runs recorded yet", EXIT_UNKNOWN

    async with StateDB() as db:
        entity_id = entity_id.strip()
        if await _prefix_is_ambiguous(db, entity_id):
            return (
                f"ambiguous id prefix {entity_id!r} — matches more than one "
                "record; use a longer prefix or the full id",
                EXIT_UNKNOWN,
            )
        session = await _resolve_session(db, entity_id)
        if session is None:
            return f"no session/invocation/play found for id {entity_id!r}", EXIT_UNKNOWN
        session_id = session["id"]
        status = session.get("status")
        if status != "running":
            return (
                f"session {session_id[:8]} is {status or 'unknown'} — controls "
                "apply only while the target flow is running",
                EXIT_UNKNOWN,
            )
        kind = session.get("invocation_kind")
        if kind not in _POLLER_KINDS:
            return (
                f"session {session_id[:8]} is {kind or 'unknown'}-kind — "
                "`li o ctl` targets `li o flow` / playbook runs (no control "
                "poller runs for other session kinds)",
                EXIT_UNKNOWN,
            )
        control_id = await db.insert_session_control(
            session_id=session_id, verb=verb, payload=payload
        )

    return (
        f"queued {verb} (control {control_id[:8]}) for session {session_id[:8]} — "
        f"applies within ~{2:.0f}s while the flow is live; "
        f"check `li o ctl status {session_id[:8]}`",
        0,
    )


async def _enqueue_control(
    *, entity_id: str, verb: str, payload: dict[str, Any] | None
) -> tuple[str, int]:
    try:
        return await asyncio.wait_for(
            _enqueue_control_inner(entity_id=entity_id, verb=verb, payload=payload),
            timeout=_DB_BUSY_TIMEOUT_S,
        )
    except (TimeoutError, asyncio.TimeoutError):  # 3.10 support: not aliased until 3.11
        return (
            f"state.db busy (no write within {_DB_BUSY_TIMEOUT_S:.0f}s) — "
            "another writer may be holding a long transaction; try again",
            EXIT_UNKNOWN,
        )


def _dispatch_control(*, entity_id: str, verb: str, payload: dict[str, Any] | None) -> int:
    from lionagi.ln.concurrency import run_async

    output, exit_code = run_async(_enqueue_control(entity_id=entity_id, verb=verb, payload=payload))
    if exit_code == EXIT_UNKNOWN:
        log_error(output)
    else:
        print(output)
    return exit_code


# ── CLI entry points ─────────────────────────────────────────────────────────


def run_ctl_pause(args: argparse.Namespace) -> int:
    """`li o ctl pause <id>` — queue a pause; applied at the running flow's next op boundary."""
    return _dispatch_control(entity_id=args.id, verb="pause", payload=None)


def run_ctl_resume(args: argparse.Namespace) -> int:
    """`li o ctl resume <id>` — queue a resume; releases a pending pause gate."""
    return _dispatch_control(entity_id=args.id, verb="resume", payload=None)


def run_ctl_msg(args: argparse.Namespace) -> int:
    """`li o ctl msg <id> "text"` — queue a context-mode operator message (ADR-0069 D3)."""
    return _dispatch_control(entity_id=args.id, verb="message", payload={"text": args.text})
