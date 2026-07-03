# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li o ctl pause|resume|msg` — enqueue session_controls rows for a running flow.

These commands are pure writers: they resolve the target session (accepting
a session id, an invocation id, or a play id — same id shapes `li o ctl
status` accepts, see lionagi/cli/status.py) and insert one row into
session_controls. They do not wait for the control to apply — a control
poller task running alongside the target flow's own heartbeat loop
(cli/orchestrate/flow.py `_execute_dag`) is the only consumer, and applies
each row against the live executor on its own poll cycle. Use `li o ctl
status <id>` to see whether a queued control has applied yet.

Only context-mode `msg` ships in this slice (ADR-0085 §3): the poller
deep-merges the message into the executor's flow workspace, visible to ops
that have not yet started. Op-mode (`--as-op`, injecting the message as a
first-class reactive DAG node) lands with a later slice.

See docs/adrs/ADR-0085-flow-control-plane.md sections 1-3.
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


async def _resolve_session_id(db: Any, entity_id: str) -> str | None:
    """Resolve a session/invocation/play id (or unambiguous prefix) to a
    backing session id, mirroring `li o ctl status`'s generic resolution."""
    target = await _resolve_any_target(db, entity_id)
    if target is None:
        return None
    entity_type, row = target
    primary = await _resolve_primary_session(db, entity_type, row)
    return primary["id"] if primary else None


async def _enqueue_control_inner(
    *, entity_id: str, verb: str, payload: dict[str, Any] | None
) -> tuple[str, int]:
    from lionagi.state.db import DEFAULT_DB_PATH, StateDB

    if not DEFAULT_DB_PATH.exists():
        return "state.db not found — no runs recorded yet", EXIT_UNKNOWN

    async with StateDB() as db:
        session_id = await _resolve_session_id(db, entity_id)
        if session_id is None:
            return f"no session/invocation/play found for id {entity_id!r}", EXIT_UNKNOWN
        control_id = await db.insert_session_control(
            session_id=session_id, verb=verb, payload=payload
        )

    return (
        f"queued {verb} (control {control_id[:8]}) for session {session_id[:8]} — "
        f"applies within {2:.0f}s; check `li o ctl status {session_id[:8]}`",
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
    """`li o ctl msg <id> "text"` — queue a context-mode operator message (ADR-0085 §3)."""
    return _dispatch_control(entity_id=args.id, verb="message", payload={"text": args.text})
