# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Human-approved Studio Operator adapters for live run controls.

These tools enqueue the same ``session_controls`` verbs as ``li o ctl``.
They do not call an in-memory executor directly: the live run remains the
single consumer, and the existing queue keeps pause/resume idempotence and
message claim semantics intact.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .redact import public_project, scrub_text
from .run_progress import resolve_run
from .store import OperatorNotFoundError, OperatorStore

PAUSE_RUN_COMMAND_TYPE = "pause_run"
RELEASE_RUN_PAUSE_COMMAND_TYPE = "release_run_pause"
STEER_RUN_COMMAND_TYPE = "steer_run"

PAUSE_RUN_DESCRIPTION = (
    "Propose a soft pause for one live flow or play. In-flight operations finish, "
    "then the run stops admitting new operations at its existing pause gate. The "
    "proposal requires explicit human approval and never cancels the run. Agent "
    "runs cannot be paused because they have no pause seam inside a model turn."
)
RELEASE_RUN_PAUSE_DESCRIPTION = (
    "Propose releasing one live flow or play's pause gate. This resumes the same "
    "live run; it is deliberately different from resume_run, which starts a new "
    "invocation from a finished run. Requires explicit human approval."
)
STEER_RUN_DESCRIPTION = (
    "Propose delivering one bounded steering message to a live flow, play, or "
    "LionAGI-owned agent run at its next supported turn boundary. Requires explicit "
    "human approval; mirrored/imported sessions with no control consumer are refused."
)

_COMMAND_TYPE_BY_VERB = {
    "pause": PAUSE_RUN_COMMAND_TYPE,
    "resume": RELEASE_RUN_PAUSE_COMMAND_TYPE,
    "message": STEER_RUN_COMMAND_TYPE,
}
_TOOL_BY_VERB = {
    "pause": "pause_run",
    "resume": "release_run_pause",
    "message": "steer_run",
}
_CONSUMER_KINDS_BY_VERB: dict[str, frozenset[str]] = {
    "pause": frozenset({"flow", "play"}),
    "resume": frozenset({"flow", "play"}),
    "message": frozenset({"flow", "play", "agent"}),
}
_TERMINAL_PROPOSAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "expired", "conflict"})


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PauseRunInput(_StrictInput):
    run: str = Field(min_length=1, max_length=200)


class ReleaseRunPauseInput(_StrictInput):
    run: str = Field(min_length=1, max_length=200)


class SteerRunInput(_StrictInput):
    run: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=8_000)


class MissingOwnerContextError(ValueError):
    """The calling turn has no durable project mapping to authorize against.

    A turn whose identity is present but whose conversation names no project
    must never be treated as authorized for every project's runs. A separate
    small copy of ``run_progress.py``'s and ``cancel_run.py``'s, kept local for
    the same reason theirs are: this module's identity and store handling stays
    self-contained.
    """


async def _allowed_project(store: OperatorStore, request_id: str) -> str:
    """The project this Operator turn is scoped to.

    Read from the conversation the turn belongs to, never from the turn's own
    ``context``. That context is whatever the turn request body carried, so a
    caller naming another project in it would be authorizing itself for that
    project's runs. A conversation's ``project`` is written once when the
    conversation is created and ``update_conversation`` exposes no parameter to
    change it, which is what makes it the durable half of the pair.

    Raises rather than returning a sentinel, which is the difference that
    matters for a control. ``run_progress.py``'s copy returns ``None`` when the
    turn identity is absent entirely, so read paths fall open for direct calls;
    a control mutates the run it names, so there is no version of "no scope" it
    can safely proceed under. A turn whose conversation is gone lands here too:
    it has no owner left to check against, which is the same refusal.
    """
    turn = await store.get_turn(request_id)
    conversation_id = turn.get("conversationId")
    project: Any = None
    if isinstance(conversation_id, str) and conversation_id:
        try:
            project = (await store.get_conversation(conversation_id)).get("project")
        except OperatorNotFoundError:
            project = None
    if not isinstance(project, str) or not project:
        raise MissingOwnerContextError(
            "operator turn's conversation declares no project -- refusing to "
            "propose a control against a run it cannot prove it owns"
        )
    return project


def _identity() -> tuple[OperatorStore, str, str]:
    import os

    db_path = os.environ.get("LIONAGI_OPERATOR_DB_PATH")
    conversation_id = os.environ.get("LIONAGI_OPERATOR_CONVERSATION_ID")
    request_id = os.environ.get("LIONAGI_OPERATOR_REQUEST_ID")
    if not db_path or not conversation_id or not request_id:
        raise RuntimeError("Studio application bridge is missing its durable turn identity")
    return OperatorStore(db_path), conversation_id, request_id


def _runner_drains_controls(session: dict[str, Any]) -> bool:
    metadata = session.get("node_metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            return False
    return bool(metadata.get("drains_controls")) if isinstance(metadata, dict) else False


def session_has_control_consumer(session: dict[str, Any]) -> bool:
    """Whether a control queued against this session would ever be drained.

    Only agent sessions can fail this. The agent runner stamps ``run_id`` on
    the sessions it creates and declares ``drains_controls`` when it starts, so
    an agent row missing either is a mirrored or imported session that no
    lionagi run owns. A control admitted for one sits pending forever with
    nobody to deliver or close it.

    Exported because the read surfaces have to answer the same question the
    admission below answers. A Studio client that offers a steer this would
    refuse gets a control that can never queue, so the offer and the refusal
    read from one rule rather than from two that can drift apart.
    """
    if session.get("invocation_kind") != "agent":
        return True
    return bool(session.get("run_id")) and _runner_drains_controls(session)


def _admission_refusal(session: dict[str, Any], verb: str) -> str | None:
    """Return a stable refusal code, or ``None`` when a consumer exists."""

    if session.get("status") != "running":
        return "not_running"
    kind = session.get("invocation_kind")
    if kind not in _CONSUMER_KINDS_BY_VERB.get(verb, frozenset()):
        return "unsupported_kind"
    if not session_has_control_consumer(session):
        return "no_consumer"
    return None


async def _load_run(session_id: str) -> dict[str, Any] | None:
    from lionagi.state.db import StateDB

    async with StateDB(readonly=True) as db:
        row = await db.fetch_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
    return dict(row) if row is not None else None


def _proposal_summary(session: dict[str, Any], verb: str, payload: dict[str, Any] | None) -> str:
    action = {"pause": "Pause", "resume": "Release pause for", "message": "Steer"}[verb]
    parts = [f"{action} run {session['id'][:12]}"]
    if project := public_project(session.get("project")):
        parts.append(f"project {project}")
    if name := session.get("name"):
        parts.append(f"named '{scrub_text(str(name))[:160]}'")
    if verb == "message" and payload:
        parts.append(f"message '{scrub_text(str(payload['text']))[:160]}'")
    return " -- ".join(parts)


def _tool_result(proposal: dict[str, Any], session_id: str, verb: str) -> dict[str, Any]:
    if proposal["status"] != "succeeded":
        reason = "denied" if proposal["status"] == "cancelled" else proposal["status"]
        return {"queued": False, "reason": reason, "id": session_id}
    raw_result = proposal.get("result")
    result = raw_result if isinstance(raw_result, dict) else {}
    if result.get("status") != "queued":
        return {
            "queued": False,
            "reason": result.get("status", "unknown"),
            "id": session_id,
        }
    return {
        "queued": True,
        "status": "queued",
        "id": session_id,
        "verb": verb,
        "controlId": result.get("control_id"),
    }


async def _propose_run_control(
    *, run: str, verb: Literal["pause", "resume", "message"], payload: dict[str, Any] | None
) -> dict[str, Any]:
    # Establish the turn's authority BEFORE resolving a reference, because
    # resolution and authorization are not the same question and this path
    # answers the harder one. ``resolve_run`` deliberately lets a turn with no
    # declared project resolve an exact full UUID: a bare id identifies at most
    # one row and cannot enumerate, which makes it safe for a read. A control
    # MUTATES the run it names, so "cannot enumerate" stops being the relevant
    # property -- an unscoped turn holding one foreign id could act on that run.
    # Requiring the project scope here keeps the read hatch where it belongs
    # without widening it to writes.
    #
    # It also has to happen before resolution rather than after, or the two
    # refusals become an oracle: "missing_owner_context" would mean the id
    # resolved and "not_found" would mean it did not, which tells an unscoped
    # turn exactly which run ids exist in other projects.
    store, conversation_id, request_id = _identity()
    try:
        allowed_project = await _allowed_project(store, request_id)
    except MissingOwnerContextError:
        return {"queued": False, "reason": "missing_owner_context"}

    resolution = await resolve_run(run)
    if not resolution["found"]:
        return {"queued": False, "reason": "not_found"}
    if resolution.get("ambiguous"):
        return {
            "queued": False,
            "reason": "ambiguous_reference",
            "candidates": resolution["candidates"],
            "truncated": resolution.get("truncated", False),
        }

    session_id = resolution["session_id"]
    session = await _load_run(session_id)
    if session is None:
        return {"queued": False, "reason": "not_found"}
    refusal = _admission_refusal(session, verb)
    if refusal is not None:
        return {
            "queued": False,
            "reason": refusal,
            "id": session_id,
            "kind": session.get("invocation_kind"),
        }

    project = session.get("project")
    if not isinstance(project, str) or not project:
        # Keep the command envelope strict so execution never interprets null
        # as "all projects".
        return {"queued": False, "reason": "missing_owner_context", "id": session_id}
    if project != allowed_project:
        # Reported as absence, matching execute_run_control_command: a distinct
        # refusal here would turn the proposal path into a probe for which run
        # ids exist in other projects.
        #
        # A second fence. resolve_run already scopes every arm for a turn that
        # declares a project, so nothing reaches this line today -- disabling
        # it reddens no test, which is the honest description of it. It stays
        # because ownership for a control belongs to the path that mutates the
        # run: if resolve_run's scoping is ever relaxed, this refuses instead
        # of silently widening what a control can reach.
        return {"queued": False, "reason": "not_found"}

    command = {
        "session_id": session_id,
        "verb": verb,
        "payload": payload,
        "project": project,
    }
    command_type = _COMMAND_TYPE_BY_VERB[verb]
    stable = store.canonical_hash(
        {"requestId": request_id, "tool": _TOOL_BY_VERB[verb], "command": command}
    )
    proposal = await store.create_proposal(
        conversation_id,
        request_id,
        command_type=command_type,
        command=command,
        risk="mutate",
        summary=_proposal_summary(session, verb, payload),
        idempotency_key=f"operator-app:{stable}",
    )
    while True:
        proposal = await store.get_proposal(proposal["id"])
        status = proposal["status"]
        if status == "pending" and proposal["expiresAt"] <= time.time():
            proposal = await store.expire_proposal(proposal["id"])
            status = proposal["status"]
        if status in _TERMINAL_PROPOSAL_STATUSES:
            return _tool_result(proposal, session_id, verb)
        await asyncio.sleep(0.1)


async def pause_run(arguments: dict[str, Any]) -> dict[str, Any]:
    args = PauseRunInput.model_validate(arguments)
    return await _propose_run_control(run=args.run, verb="pause", payload=None)


async def release_run_pause(arguments: dict[str, Any]) -> dict[str, Any]:
    args = ReleaseRunPauseInput.model_validate(arguments)
    return await _propose_run_control(run=args.run, verb="resume", payload=None)


async def steer_run(arguments: dict[str, Any]) -> dict[str, Any]:
    args = SteerRunInput.model_validate(arguments)
    return await _propose_run_control(
        run=args.run,
        verb="message",
        payload={"text": args.message},
    )


def _validated_command(
    command: dict[str, Any], *, expected_verb: str | None
) -> tuple[str, str, dict[str, Any] | None, str]:
    session_id = command.get("session_id")
    verb = command.get("verb")
    payload = command.get("payload")
    project = command.get("project")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("run-control command is missing session_id")
    if verb not in _CONSUMER_KINDS_BY_VERB:
        raise ValueError("run-control command has an unsupported verb")
    if expected_verb is not None and verb != expected_verb:
        raise ValueError("run-control command type does not match its verb")
    if not isinstance(project, str) or not project:
        raise ValueError("run-control command is missing project ownership")
    if verb == "message":
        if (
            not isinstance(payload, dict)
            or set(payload) != {"text"}
            or not isinstance(payload.get("text"), str)
            or not payload["text"].strip()
            or len(payload["text"]) > 8_000
        ):
            raise ValueError("steer command has an invalid message payload")
    elif payload is not None:
        raise ValueError("pause-gate commands do not accept a payload")
    return session_id, verb, payload, project


async def execute_run_control_command(
    command: dict[str, Any], *, expected_verb: str | None = None
) -> dict[str, Any]:
    """Revalidate the approved target, then atomically enqueue its control."""

    from lionagi.state.db import StateDB

    session_id, verb, payload, project = _validated_command(command, expected_verb=expected_verb)
    async with StateDB() as db:
        row = await db.fetch_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
        if row is None or row.get("project") != project:
            # Project mismatches are indistinguishable from absence so an
            # approved command cannot be used to probe a foreign run id.
            raise ValueError("run not found")
        session = dict(row)
        refusal = _admission_refusal(session, verb)
        if refusal == "not_running":
            raise ValueError("run is no longer running")
        if refusal is not None:
            raise ValueError(f"run cannot consume this control ({refusal})")
        control_id = await db.insert_session_control(
            session_id=session_id,
            verb=verb,
            payload=payload,
            project=project,
        )
    if control_id is None:
        # insert_session_control's own admission condition is the final
        # decision and closes two races at once: the run terminalizing, and
        # the run being reassigned to another project between the check above
        # and this insert. The check above still runs, because it is what
        # distinguishes the refusal reasons for the caller; it is not what
        # enforces ownership.
        raise ValueError("run is no longer running")
    return {
        "status": "queued",
        "session_id": session_id,
        "verb": verb,
        "control_id": control_id,
    }


async def execute_pause_run_command(command: dict[str, Any]) -> dict[str, Any]:
    return await execute_run_control_command(command, expected_verb="pause")


async def execute_release_run_pause_command(command: dict[str, Any]) -> dict[str, Any]:
    return await execute_run_control_command(command, expected_verb="resume")


async def execute_steer_run_command(command: dict[str, Any]) -> dict[str, Any]:
    return await execute_run_control_command(command, expected_verb="message")
