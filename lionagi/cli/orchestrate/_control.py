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
import json
from typing import Any

from .._logging import log_error
from .._util import AmbiguousIdError
from ..status import EXIT_UNKNOWN, _resolve_any_target, _resolve_primary_session

__all__ = (
    "run_ctl_pause",
    "run_ctl_resume",
    "run_ctl_msg",
    "run_ctl_resolve",
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


def _runner_drains_controls(session: dict[str, Any]) -> bool:
    """Whether this session's runner declared that it consumes operator controls.

    The declaration is written into node_metadata when the run starts. Absence
    reads as False on purpose: a runner that never said it drains is exactly the
    case this exists to catch, and a control admitted for it would be delivered
    by nobody.
    """
    meta = session.get("node_metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            return False
    return bool(meta.get("drains_controls")) if isinstance(meta, dict) else False


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
        # Owning a run is not the same as consuming controls. The check above
        # uses run_id as a proxy for "a lionagi runner owns this", and that
        # proxy held only while the CLI agent runner was the sole caller that
        # stamped one. Other callers persist through the same path, write the
        # same kind, and supply their own run_id, so they pass that check while
        # having no drain at all — the control would be admitted, never
        # delivered, and never closed. Ask about the capability instead, and
        # let the runner declare it when it starts the session.
        #
        # This refuses one row that used to be admitted and deserved to be: a
        # CLI agent leg whose session row predates the declaration. It has a
        # run_id and a real turn-end drain, and its control would have landed.
        # That is deliberate and it is not a migration that was skipped.
        #
        # Which transitions replace an absent declaration, and when that
        # refusal ends, are not described here on purpose: every attempt to
        # state it in prose has been wrong, in a different way each time, while
        # the meaning below has held. Read the resume cases in
        # tests/cli/test_agent_steer.py instead. They are where that behaviour
        # is stated in a form that fails when it stops being true, which a
        # comment cannot do; they are not a claim that every transition is
        # covered.
        #
        # Absence is still the right reading. Fields that happen to differ
        # between producers are not capability: the embedded runner's run_id
        # carries a distinguishing prefix today, but a naming convention is not
        # a contract, and keying admission on the shape of an id is the same
        # move as keying it on the presence of one — the proxy this check
        # exists to retire. There is no field on a pre-existing row that says
        # what the runner will DO, and inventing one from a prefix would
        # re-admit the orphaned controls as soon as a producer renamed a run.
        if kind == "agent" and not _runner_drains_controls(session):
            return (
                f"session {session_id[:8]} is run by something that does not "
                "consume operator controls, so the control would sit pending "
                "forever with nobody to deliver or close it",
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
        if control_id is None:
            # The status check above and the insert are two statements, and the
            # run can reach its terminal status between them. The insert carries
            # the same running-session condition so that it, not the earlier
            # read, decides: refusing here is the alternative to leaving a
            # control queued against a run whose consumer has already gone.
            return (
                f"session {session_id[:8]} reached a terminal status while the "
                "control was being queued — nothing would consume it",
                EXIT_UNKNOWN,
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


def _resolving_actor(explicit: str | None = None) -> str:
    """Who is recorded as having resolved a claim.

    An operator action that leaves no operator identity recreates the problem
    it is closing one level up: the row would say a human decided, and not
    which one. The OS account is a real identity and is available without
    asking, so it is the default rather than a placeholder; --by overrides it
    for the case where the person running the command is not the account.
    """
    if explicit and explicit.strip():
        return explicit.strip()
    import getpass

    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 — no controlling user (daemon, odd container)
        return "an unnamed operator"


async def _resolve_control_inner(
    *, control_id: str, outcome: str, actor: str | None = None
) -> tuple[str, int]:
    from lionagi.state.db import StateDB, state_db_known_absent

    if state_db_known_absent():
        return "state.db not found — no runs recorded yet", EXIT_UNKNOWN

    async with StateDB() as db:
        stored = await db.resolve_claimed_session_control(
            control_id.strip(), outcome=outcome, actor=_resolving_actor(actor)
        )
    if stored is None:
        # One message rather than a guess between the four ways to get here,
        # because the remedy differs and this command must not invent which one
        # applies: the id may not exist, the row may already carry a terminal
        # result, it may be pending but unclaimed (which the run's own teardown
        # sweep is what closes), or its consumer may have reported back between
        # this command's read and its write.
        return (
            f"control {control_id[:8]} is not a claimed row — `li o ctl status` "
            "shows which controls are claimed and by whom; only those can be "
            "resolved by hand",
            EXIT_UNKNOWN,
        )
    return f"resolved control {control_id[:8]} as {outcome}: {stored}", 0


def run_ctl_resolve(args: argparse.Namespace) -> int:
    """`li o ctl resolve <control-id> --as applied|abandoned` — close a wedged claim.

    A control whose consumer claimed it and then died leaves a row nothing can
    honestly finalize, because whether the message reached the model is not
    recoverable from anything the system kept. This is how a human who has found
    out puts the answer on the record.
    """
    from lionagi.ln.concurrency import run_async

    output, exit_code = run_async(
        asyncio.wait_for(
            _resolve_control_inner(
                control_id=args.control_id,
                outcome=args.outcome,
                actor=getattr(args, "actor", None),
            ),
            timeout=_DB_BUSY_TIMEOUT_S,
        )
    )
    if exit_code == EXIT_UNKNOWN:
        log_error(output)
    else:
        print(output)
    return exit_code
