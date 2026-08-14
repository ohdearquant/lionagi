# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Studio Operator lifecycle service/adapter: ``resume_run``.

Delegates to the real, supported ``POST /runs/{run_id}/resume`` surface
(`lionagi/studio/services/run_resume.py::resume_run`) for the actual launch
decision -- this adapter never picks an argv or a resume path itself. It
does read the run's recorded ``invocation_kind`` before creating a proposal,
so the proposal's summary says the true thing the dispatcher is about to do
and an argument combination the dispatcher would reject (e.g. an
instruction for a checkpoint-replay kind) is refused before a human ever
sees an approvable proposal for it. Gated on the same durable human
allow/deny proposal flow ``cancel_run``/``launch_playbook`` use, since it
starts a new process. See docs/internals/studio.md ("Turn identity and the
propose/poll/execute pattern") for why this is a distinct operation from
un-pausing a paused or cancelled run, and accepts a run in any status.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .redact import scrub_text
from .run_progress import resolve_run
from .store import OperatorStore

RESUME_RUN_COMMAND_TYPE = "resume"

RESUME_RUN_DESCRIPTION = (
    "Continue a run through the same path a human resuming from the CLI or "
    "Studio uses. An 'agent' run continues its existing conversation branch "
    "with a new instruction, through `li agent -r`; requires an instruction, "
    "and resolves to exactly one branch unless a branch id is given "
    "explicitly. A 'play', 'flow', or 'show-play' run instead replays its "
    "persisted checkpoint through `li o flow --resume` -- the checkpoint "
    "owns the plan, so instruction and branch are rejected for those kinds; "
    "set allow_degraded_context only to proceed past a refusal that exists "
    "to protect conversational context. This is not an un-pause of a paused "
    "or cancelled run: the run lifecycle has no edge back out of a terminal "
    "status for that, so this always launches a new invocation rather than "
    "reopening the old run's status. Works on a run in any status, including "
    "completed, failed, or cancelled ones. Goes through a human approval "
    "flow; it is never automatic, and a denied proposal starts nothing. "
    "Accepts a run UUID, an 8+ hex id prefix, a name substring (minimum 3 "
    "characters), or 'current' for the run open when this instruction was "
    "sent. Ambiguous references return candidates rather than guessing."
)


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ResumeRunInput(_StrictInput):
    run: str = Field(min_length=1, max_length=200)
    # Required for an 'agent' run; must be omitted for a checkpoint-replay
    # run (play/flow/show-play) -- the service enforces that split.
    instruction: str | None = Field(default=None, min_length=1, max_length=32_768)
    branch: str | None = Field(default=None, min_length=1, max_length=200)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    # Only meaningful for a checkpoint-replay run; never defaulted to true
    # automatically. See run_resume.py::_resume_flow_run.
    allow_degraded_context: bool = False


def _kind_argument_mismatch(
    run_id: str, kind: str | None, args: ResumeRunInput
) -> tuple[str, str] | None:
    """Mirror ``run_resume.py::_dispatch_resume_by_kind``'s argument-vs-kind
    rules so a combination the dispatcher would refuse never gets far enough
    to become a proposal. Returns ``None`` when the combination is fine, or
    an ``(reason, message)`` pair matching the ``error``/``message`` shape
    ``execute_resume_command`` would have produced for the same rejection --
    the only thing this changes is when the caller learns it, not what they
    are told.
    """
    from lionagi.studio.services.run_resume import FLOW_RESUME_KINDS

    if kind == "agent":
        if args.instruction is None:
            return "invalid_input", "instruction is required to resume an agent run"
        return None
    if kind in FLOW_RESUME_KINDS:
        if args.instruction is not None:
            return (
                "invalid_input",
                f"invocation_kind {kind!r} replays the persisted checkpoint plan; "
                "instruction is not accepted",
            )
        if args.branch is not None:
            return (
                "invalid_input",
                f"invocation_kind {kind!r} replays the persisted checkpoint plan; "
                "branch_id is not accepted",
            )
        if args.model is not None:
            return (
                "invalid_input",
                f"invocation_kind {kind!r} replays the persisted checkpoint plan; "
                "model is not accepted",
            )
        return None
    return (
        "conflict",
        f"Run {run_id!r} has invocation_kind {kind!r}, which does not support resume.",
    )


def _identity() -> tuple[OperatorStore, str, str]:
    import os

    db_path = os.environ.get("LIONAGI_OPERATOR_DB_PATH")
    conversation_id = os.environ.get("LIONAGI_OPERATOR_CONVERSATION_ID")
    request_id = os.environ.get("LIONAGI_OPERATOR_REQUEST_ID")
    if not db_path or not conversation_id or not request_id:
        raise RuntimeError("Studio application bridge is missing its durable turn identity")
    return OperatorStore(db_path), conversation_id, request_id


def _redacted_resume_result(proposal: dict[str, Any], run_id: str) -> dict[str, Any]:
    if proposal["status"] != "succeeded":
        reason = "denied" if proposal["status"] == "cancelled" else proposal["status"]
        return {"resumed": False, "reason": reason, "id": run_id}
    raw = proposal.get("result")
    result = raw if isinstance(raw, dict) else {}
    error = result.get("error")
    if error:
        return {
            "resumed": False,
            "reason": error,
            "id": run_id,
            "message": scrub_text(str(result.get("message", ""))),
        }
    # An agent-kind resume returns branch_id; a checkpoint-replay resume
    # (play/flow/show-play) returns invocation_kind + checkpoint_run_id
    # instead -- include only whichever the service actually reported.
    response: dict[str, Any] = {
        "resumed": True,
        "id": run_id,
        "invocationId": result.get("invocation_id"),
    }
    if "branch_id" in result:
        response["branchId"] = result["branch_id"]
    if "invocation_kind" in result:
        response["invocationKind"] = result["invocation_kind"]
    if "checkpoint_run_id" in result:
        response["checkpointRunId"] = result["checkpoint_run_id"]
    return response


async def resume_run(arguments: dict[str, Any]) -> dict[str, Any]:
    """MCP tool handler: resolve -> durable proposal -> poll -> result.

    Mirrors `cancel_run.py::cancel_run`'s shape exactly: this function only
    creates the proposal and waits for it to leave "pending". The actual
    resume happens in `execute_resume_command`, invoked by the coordinator
    once a human allows the proposal.
    """
    args = ResumeRunInput.model_validate(arguments)
    store, conversation_id, request_id = _identity()

    resolution = await resolve_run(args.run)
    if not resolution["found"]:
        return {
            "resumed": False,
            "reason": "not_found",
            "detail": resolution.get("reason"),
        }
    if resolution.get("ambiguous"):
        return {
            "resumed": False,
            "reason": "ambiguous_reference",
            "candidates": resolution["candidates"],
            "truncated": resolution.get("truncated", False),
        }

    run_id = resolution["session_id"]

    from lionagi.state.db import StateDB, read_only_open_supported

    async with StateDB(readonly=read_only_open_supported()) as db:
        session = await db.get_session(run_id)
    kind = session.get("invocation_kind") if session is not None else None

    mismatch = _kind_argument_mismatch(run_id, kind, args)
    if mismatch is not None:
        reason, message = mismatch
        return {"resumed": False, "reason": reason, "id": run_id, "message": message}

    command = {
        "run_id": run_id,
        "instruction": args.instruction,
        "branch_id": args.branch,
        "model": args.model,
        "allow_degraded_context": args.allow_degraded_context,
    }
    stable = store.canonical_hash(
        {"requestId": request_id, "tool": "resume_run", "command": command}
    )
    # The mismatch check above already confirmed kind matches the argument
    # shape (agent+instruction, or a flow kind with none), so the summary
    # can be phrased from the run's own recorded kind rather than from
    # argument presence -- the two can no longer disagree.
    summary = (
        f"Resume run {run_id[:12]} with a new instruction"
        if kind == "agent"
        else f"Resume run {run_id[:12]} by replaying its checkpoint"
    )
    proposal = await store.create_proposal(
        conversation_id,
        request_id,
        command_type=RESUME_RUN_COMMAND_TYPE,
        command=command,
        risk="execute",
        summary=summary,
        idempotency_key=f"operator-app:{stable}",
    )
    while True:
        proposal = await store.get_proposal(proposal["id"])
        status = proposal["status"]
        if status == "pending" and proposal["expiresAt"] <= time.time():
            proposal = await store.expire_proposal(proposal["id"])
            status = proposal["status"]
        if status in {"succeeded", "failed", "cancelled", "expired", "conflict"}:
            return _redacted_resume_result(proposal, run_id)
        await asyncio.sleep(0.1)


async def execute_resume_command(command: dict[str, Any]) -> dict[str, Any]:
    """The real state-changing act -- the adapter's other half.

    Wire this into `OperatorCoordinator`'s ``command_executor`` for
    ``command_type == "resume"`` (see `coordinator.py::_execute_application_command`).
    Never raises for an expected service outcome: every real error the
    service defines is caught and reported as a structured, specific
    ``error`` code rather than letting the coordinator collapse it into a
    generic masked "service_failure" -- the whole point of wiring the real
    surface instead of a stub is that the human sees the real reason a
    resume could not proceed.
    """
    from lionagi.studio.services import launches as _launches
    from lionagi.studio.services.run_resume import (
        RunBranchConflictError,
        RunBranchMembershipError,
        RunNotFoundError,
        RunResumeCheckpointError,
        RunResumeInProgressError,
        RunResumeUnavailableError,
        RunResumeUnsupportedKindError,
    )
    from lionagi.studio.services.run_resume import resume_run as _service_resume_run

    run_id = command.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("resume command is missing run_id")
    instruction = command.get("instruction")
    if instruction is not None and not isinstance(instruction, str):
        raise ValueError("resume command instruction must be a string when present")
    branch_id = command.get("branch_id")
    model = command.get("model")
    allow_degraded_context = bool(command.get("allow_degraded_context", False))

    try:
        result = await _service_resume_run(
            run_id,
            instruction=instruction,
            branch_id=branch_id,
            model=model,
            allow_degraded_context=allow_degraded_context,
        )
    except RunNotFoundError as exc:
        return {"error": "not_found", "message": scrub_text(str(exc))}
    except RunResumeCheckpointError as exc:
        return {"error": exc.reason, "message": scrub_text(str(exc))}
    except (
        RunBranchConflictError,
        RunResumeInProgressError,
        RunResumeUnavailableError,
        RunResumeUnsupportedKindError,
    ) as exc:
        return {"error": "conflict", "message": scrub_text(str(exc))}
    except (RunBranchMembershipError, ValueError) as exc:
        return {"error": "invalid_input", "message": scrub_text(str(exc))}
    except _launches.TooManyLaunchesError as exc:
        return {"error": "rate_limited", "message": scrub_text(str(exc))}
    except _launches.LiExecutableUnavailableError as exc:
        return {"error": "unavailable", "message": scrub_text(str(exc))}
    return {k: v for k, v in result.items() if k != "run_id"}
