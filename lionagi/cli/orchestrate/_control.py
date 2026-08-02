# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""`li o ctl pause|resume|msg` — enqueue session_controls rows for a running flow.

Pure writers: resolve the target session (id/invocation id/play id/run id,
same shapes `li o ctl status` accepts) and insert one row into
session_controls. They do not wait for the control to apply — the poller in
cli/orchestrate/flow.py `_execute_dag` (flow/play) and the turn-end drain in
cli/agent.py (agent) are the consumers; use `li o ctl status <id>` to check
whether it landed.

Only context-mode `msg` is currently supported: the poller appends the message
to shared flow context for operations not yet rendered. Operation-mode messages
are unsupported. See ADR-0069 D1 and D3.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from .._logging import log_error
from .._util import AmbiguousIdError
from ..status import EXIT_UNKNOWN, _resolve_any_target, _resolve_primary_session

__all__ = (
    "run_ctl_pause",
    "run_ctl_resume",
    "run_ctl_msg",
)

# Mirrors status.py's _DB_BUSY_TIMEOUT_S — bounds a single enqueue's total DB
# time so a stuck write fails fast instead of hanging indefinitely.
_DB_BUSY_TIMEOUT_S = 10.0


# Session kinds with a live consumer for each control verb. Flow and playbook
# runs ("flow", "play") run the control poller and consume all three verbs.
# Agent runs ("agent") drain `message` controls at turn end — a steer lands as
# a warm continuation turn — but have no pause seam inside a single operate()
# call, so pause/resume stay refused for them. A kind with no consumer for the
# requested verb is refused at enqueue: a queued control nobody reads would sit
# pending forever.
_CONSUMER_KINDS_BY_VERB: dict[str, frozenset[str]] = {
    "pause": frozenset({"flow", "play"}),
    "resume": frozenset({"flow", "play"}),
    "message": frozenset({"flow", "play", "agent"}),
}


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
    from lionagi.state.db import StateDB, state_db_known_absent

    if state_db_known_absent():
        return "state.db not found — no runs recorded yet", EXIT_UNKNOWN

    async with StateDB() as db:
        entity_id = entity_id.strip()
        try:
            session = await _resolve_session(db, entity_id)
        except AmbiguousIdError as exc:
            return str(exc), EXIT_UNKNOWN
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
        allowed = _CONSUMER_KINDS_BY_VERB.get(verb, frozenset())
        # Mirrored/imported sessions are agent-kind and can sit at status
        # "running" (claude_mirror and codex_mirror both write
        # invocation_kind="agent"), but no lionagi runner owns them, so
        # nothing would ever drain the steer. The agent runner always stamps
        # run_id on the sessions it creates; an agent-kind row without one has
        # no drain consumer. Fail closed — refusing beats a steer that can
        # never land.
        if kind == "agent" and not session.get("run_id"):
            return (
                f"session {session_id[:8]} is a mirrored/imported agent "
                "session (no lionagi run owns it), so no runner would ever "
                "deliver the steer",
                EXIT_UNKNOWN,
            )
        if kind not in allowed:
            if kind == "agent":
                # Reachable only for pause/resume: message is consumable.
                return (
                    f"session {session_id[:8]} is agent-kind — agent runs "
                    f"consume `msg` steers at turn end but have no {verb} "
                    "seam inside a running turn",
                    EXIT_UNKNOWN,
                )
            return (
                f"session {session_id[:8]} is {kind or 'unknown'}-kind — "
                f"no consumer reads {verb} controls for this session kind, "
                "so the control would sit pending forever",
                EXIT_UNKNOWN,
            )
        control_id = await db.insert_session_control(
            session_id=session_id, verb=verb, payload=payload
        )

    # Landing time is a property of the consumer, not the verb: a flow/play
    # poller renders context before the next op (~2s poll interval), an agent
    # leg drains at its next turn boundary — which can be much later than 2s
    # into a long provider call. Stating the flow-poller number for an agent
    # steer would tell the operator to expect delivery well before it lands.
    landing = (
        "lands as a continuation turn once the run's current turn ends"
        if kind == "agent"
        else f"applies within ~{2:.0f}s while the flow is live"
    )
    return (
        f"queued {verb} (control {control_id[:8]}) for session {session_id[:8]} — "
        f"{landing}; check `li o ctl status {session_id[:8]}`",
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
