# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Resume an existing Studio run through the durable ``li agent`` path."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import tempfile
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, Field

from lionagi._spec_limits import MAX_SPEC_PROMPT_CHARS
from lionagi.state.db import StateDB, read_only_open_supported, state_db_known_absent

from ..registry import studio_route
from ..scheduler import subprocess as _subprocess
from . import launches as _launches
from .schedules import (
    _svc_validate_action_model,
    _svc_validate_identifier,
    _svc_validate_prompt,
)

_log = logging.getLogger(__name__)


class RunNotFoundError(LookupError):
    """The requested run/session does not exist."""


class RunBranchConflictError(RuntimeError):
    """The run does not resolve to exactly one resumable branch."""


class RunBranchMembershipError(ValueError):
    """An explicitly requested branch does not belong to the run."""


class RunResumeUnavailableError(RuntimeError):
    """The branch exists in StateDB but its resumable snapshot is unavailable."""


class RunResumeInProgressError(RuntimeError):
    """Another queued or executing resume already owns this branch/target."""


class RunResumeUnsupportedKindError(RuntimeError):
    """The run's invocation_kind is NULL or not a resumable value."""


class RunResumeCheckpointError(RuntimeError):
    """A flow-kind resume could not be preflighted to a usable checkpoint.

    ``reason`` is a stable machine-readable code distinguishing WHY a
    checkpoint is not available (target/session/run-id lookup failure, no
    checkpoint.json, or an empty persisted plan) from a launch failure — the
    caller must be able to tell "there is nothing to resume" apart from
    "resume was attempted and failed".
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class RunResumeRequest(BaseModel):
    # Optional: required for invocation_kind="agent", rejected for the
    # checkpoint-replay kinds (play/flow/show-play), where the checkpoint —
    # not a new instruction — supplies the plan.
    instruction: str | None = Field(default=None, max_length=MAX_SPEC_PROMPT_CHARS)
    branch_id: str | None = None
    model: str | None = None
    # Only meaningful for the checkpoint-replay kinds; never defaulted to
    # true automatically. See _resume_flow_run.
    allow_degraded_context: bool = False
    retry_failed: bool = False


# invocation_kind values that replay a checkpointed flow instead of
# reopening a single agent branch. Kept separate from the DB CHECK
# constraint's vocabulary (schema.sql) so a new kind must be classified
# here explicitly before it can be resumed at all.
#
# fanout is deliberately excluded: `_run_fanout` (cli/orchestrate/fanout.py)
# never stamps a run_id into node_metadata and never instantiates a
# CheckpointWriter, so a real fanout session can never satisfy
# _resolve_flow_checkpoint's prerequisites — there is no future in which one
# does. Routing it through the checkpoint-resolution path anyway would only
# ever fail with flow-specific wording ("...or never reached _build_dag")
# that misdescribes why. Treating it as unsupported instead is the honest,
# structurally-correct answer, decided by what the kind can ever produce, not
# by kind membership in a set built for a different execution shape.
FLOW_RESUME_KINDS = frozenset({"play", "flow", "show-play"})


_resume_admission_lock = asyncio.Lock()


def _validate_resume_inputs(
    instruction: str,
    *,
    branch_id: str | None,
    model: str | None,
) -> None:
    if not instruction.strip():
        raise ValueError("instruction must contain non-whitespace text")
    if len(instruction) > MAX_SPEC_PROMPT_CHARS:
        raise ValueError(f"instruction exceeds the {MAX_SPEC_PROMPT_CHARS}-character prompt limit")
    _svc_validate_prompt(instruction)

    if branch_id is not None:
        if not branch_id:
            raise ValueError("branch_id must be non-empty when provided")
        _svc_validate_identifier(branch_id, "branch_id")

    if model is not None:
        if not model:
            raise ValueError("model must be non-empty when provided")
        _svc_validate_action_model(model)


async def _resolve_branch(run_id: str, requested_branch_id: str | None) -> str:
    """Resolve one branch owned by *run_id* without hydrating its messages."""
    _svc_validate_identifier(run_id, "run_id")
    if state_db_known_absent():
        raise RunNotFoundError(f"Run {run_id!r} not found")

    async with StateDB(readonly=read_only_open_supported()) as db:
        session = await db.get_session(run_id)
        if session is None:
            raise RunNotFoundError(f"Run {run_id!r} not found")
        branches = await db.list_branches(run_id)

    branch_ids = [str(branch["id"]) for branch in branches]
    if requested_branch_id is not None:
        if requested_branch_id not in branch_ids:
            raise RunBranchMembershipError(
                f"Branch {requested_branch_id!r} does not belong to run {run_id!r}"
            )
        return requested_branch_id

    if not branch_ids:
        raise RunBranchConflictError(f"Run {run_id!r} has no branch to resume")
    if len(branch_ids) > 1:
        raise RunBranchConflictError(
            f"Run {run_id!r} has {len(branch_ids)} branches; branch_id is required"
        )
    return branch_ids[0]


async def _run_status(run_id: str) -> str:
    async with StateDB(readonly=read_only_open_supported()) as db:
        session = await db.get_session(run_id)
    if session is None:
        raise RunNotFoundError(f"Run {run_id!r} not found")
    return str(session.get("status") or "")


async def _active_resume_for_branch(branch_id: str) -> dict[str, Any] | None:
    async with StateDB(readonly=read_only_open_supported()) as db:
        rows = await db.list_invocations(
            skill="resume:agent",
            status="running",
            limit=200,
            offset=0,
        )
    for row in rows:
        metadata = row.get("node_metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = None
        if isinstance(metadata, dict) and metadata.get("branch_id") == branch_id:
            return {**row, "node_metadata": metadata}
    return None


async def _ensure_branch_snapshot_available(branch_id: str) -> None:
    """Require the exact branch snapshot that ``li agent -r`` will reopen."""
    from lionagi.cli._runs import find_branch
    from lionagi.cli._util import AmbiguousIdError

    try:
        _snapshot_run_id, snapshot_path = await asyncio.to_thread(find_branch, branch_id)
    except (OSError, AmbiguousIdError) as exc:
        raise RunResumeUnavailableError(
            f"Branch {branch_id!r} has no available CLI snapshot and cannot be resumed"
        ) from exc

    # find_branch deliberately accepts prefixes for CLI convenience. The API
    # resolved an exact StateDB member, so silently accepting a different
    # snapshot whose id merely starts with this value would resume the wrong
    # conversation.
    if snapshot_path.name not in {branch_id, f"{branch_id}.json"}:
        raise RunResumeUnavailableError(
            f"Branch {branch_id!r} has no exact CLI snapshot and cannot be resumed"
        )

    def hydrate_exact_snapshot() -> None:
        from lionagi.session.branch import Branch

        serialized = json.loads(snapshot_path.read_text())
        branch = Branch.from_dict(serialized)
        if str(branch.id) != branch_id:
            raise ValueError("snapshot branch identity does not match")

    try:
        await asyncio.to_thread(hydrate_exact_snapshot)
    except Exception as exc:  # noqa: BLE001
        _log.warning("Refusing incompatible branch snapshot %s: %s", branch_id, exc)
        raise RunResumeUnavailableError(
            f"Branch {branch_id!r} has an invalid CLI snapshot and cannot be resumed"
        ) from exc


async def _require_resumable_snapshot(run_id: str, branch_id: str) -> bool:
    """The one prerequisite check for an agent-kind resume, shared by GET and POST.

    Returns whether the source run is still queued. A queued source has no
    snapshot to check yet — a queued resume writes its own worker config and
    the snapshot is verified once the source actually finishes, matching
    _resume_agent_run's own launch-time branching. Only when the source is
    already terminal does `li agent -r` need a snapshot to reopen right now,
    so only then is one required here.

    resume_availability (GET) and _resume_agent_run (POST) both call this
    instead of each doing their own version of it — GET previously only
    checked branch membership, so it could answer "resumable" for a run POST
    would then 409 on because the branch's CLI snapshot was never written or
    had since been pruned. Sharing the check means GET's answer and POST's
    outcome can only disagree when something about the run genuinely changed
    between the two calls.
    """
    from lionagi.state.db import SESSION_TERMINAL_STATUSES

    source_status = await _run_status(run_id)
    queued = source_status not in SESSION_TERMINAL_STATUSES
    if not queued:
        await _ensure_branch_snapshot_available(branch_id)
    return queued


def _build_resume_argv(
    executable_prefix: list[str],
    *,
    branch_id: str,
    instruction: str,
    model: str | None,
) -> list[str]:
    """Build the existing CLI resume command without permission-bypass flags."""
    argv = [*executable_prefix, "agent", "-r", branch_id]
    if model is not None:
        # With --prompt carrying the instruction, the sole positional is the
        # optional model override accepted by ``li agent``.
        argv.append(model)
    # argparse treats an option-looking next token as another flag instead of
    # the value to --prompt. Keep the ordinary command human-readable while
    # using its assignment form for a literal instruction that starts with '-'.
    if instruction.startswith("-"):
        argv.append(f"--prompt={instruction}")
    else:
        argv.extend(["--prompt", instruction])
    return argv


def _write_queued_resume_config(
    *,
    run_id: str,
    branch_id: str,
    instruction: str,
    model: str | None,
    executable_prefix: list[str],
) -> str:
    fd, path = tempfile.mkstemp(prefix="lionagi-resume-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(
                {
                    "run_id": run_id,
                    "branch_id": branch_id,
                    "instruction": instruction,
                    "model": model,
                    "executable_prefix": executable_prefix,
                },
                file,
                sort_keys=True,
                separators=(",", ":"),
            )
    except BaseException:
        os.unlink(path)
        raise
    return path


async def _resume_agent_run(
    run_id: str,
    *,
    instruction: str,
    branch_id: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Launch a follow-up turn on an existing run's durable branch."""
    _validate_resume_inputs(instruction, branch_id=branch_id, model=model)
    resolved_branch_id = await _resolve_branch(run_id, branch_id)

    executable_prefix, resolve_error = _subprocess.resolve_li_executable()
    if executable_prefix is None:
        if resolve_error:
            _log.error("Could not resolve the installed `li` executable: %s", resolve_error)
        raise _launches.LiExecutableUnavailableError(
            "The Studio daemon could not resolve the installed `li` executable; "
            "reinstall LionAGI with the Studio extra and restart Studio"
        )

    async with _resume_admission_lock:
        active = await _active_resume_for_branch(resolved_branch_id)
        if active is not None:
            metadata = active["node_metadata"]
            if (
                active.get("prompt") == instruction
                and metadata.get("model") == model
                and metadata.get("run_id") == run_id
            ):
                return {
                    "run_id": run_id,
                    "branch_id": resolved_branch_id,
                    "invocation_id": active["id"],
                }
            raise RunResumeInProgressError(
                f"Branch {resolved_branch_id!r} already has a resume in progress"
            )

        queued = await _require_resumable_snapshot(run_id, resolved_branch_id)
        tmp_path: str | None = None
        if queued:
            tmp_path = _write_queued_resume_config(
                run_id=run_id,
                branch_id=resolved_branch_id,
                instruction=instruction,
                model=model,
                executable_prefix=executable_prefix,
            )
            argv = [
                sys.executable,
                "-m",
                "lionagi.studio.services.run_resume_worker",
                "--config",
                tmp_path,
            ]
        else:
            argv = _build_resume_argv(
                executable_prefix,
                branch_id=resolved_branch_id,
                instruction=instruction,
                model=model,
            )
        try:
            invocation_id = await _launches.launch_detached_argv(
                argv,
                skill="resume:agent",
                plugin="studio_run_resume",
                prompt=instruction,
                tmp_path=tmp_path,
                action_kind="agent",
                node_metadata={
                    "run_id": run_id,
                    "branch_id": resolved_branch_id,
                    "resume": True,
                    "queued_for_terminal": queued,
                    "model": model,
                },
            )
        except BaseException:
            if tmp_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
            raise
    return {
        "run_id": run_id,
        "branch_id": resolved_branch_id,
        "invocation_id": invocation_id,
    }


async def _resolve_flow_checkpoint(target: str) -> tuple[Any, dict[str, Any]]:
    """Preflight a flow-kind resume to the checkpoint it would replay.

    Reuses the exact resolution `li o flow --resume` performs (run id, or
    any session/invocation/play id backed by one) so a Studio "no
    checkpoint" answer and a CLI "no checkpoint" answer can never disagree
    about the same target. Raises RunResumeCheckpointError, never launches.
    """
    from lionagi.cli._util import AmbiguousIdError
    from lionagi.cli.orchestrate._checkpoint import FlowResumeError, resolve_checkpoint_target

    try:
        run_dir, checkpoint = await resolve_checkpoint_target(target)
    except FlowResumeError as exc:
        message = str(exc)
        if "No checkpoint.json found" in message:
            reason = "no_checkpoint"
        elif "has no run_id on record" in message:
            reason = "no_run_id"
        elif "No backing session found" in message:
            reason = "no_backing_session"
        else:
            reason = "target_not_found"
        raise RunResumeCheckpointError(reason, message) from exc
    except AmbiguousIdError as exc:
        # A short id prefix matched more than one run directory. This is a
        # distinct resumability state from "no checkpoint" — the caller
        # named something real, it just isn't unique yet.
        raise RunResumeCheckpointError(
            "ambiguous_target", f"Target {target!r} is ambiguous: {exc}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        # The checkpoint file exists but couldn't be read/parsed (truncated
        # write, disk error, hand-edited JSON). Distinct from "no checkpoint"
        # so the caller can tell "nothing to resume" from "resume is blocked
        # on a corrupt persisted state".
        raise RunResumeCheckpointError(
            "invalid_checkpoint",
            f"Checkpoint for {target!r} could not be read: {exc}",
        ) from exc

    if not checkpoint.get("plan"):
        raise RunResumeCheckpointError(
            "empty_checkpoint",
            f"Checkpoint for {target!r} has an empty plan — nothing to resume.",
        )
    return run_dir, checkpoint


def _build_flow_resume_argv(
    executable_prefix: list[str],
    *,
    target: str,
    allow_degraded_context: bool,
    retry_failed: bool,
) -> list[str]:
    """Build the checkpoint-replay resume command.

    No instruction, no branch, no model: the checkpoint owns the plan.
    --allow-degraded-context and --retry-failed are appended only on explicit
    opt-in — neither is ever the default, since the whole purpose of each is
    to proceed past a refusal: one protects conversational context, the other
    protects against re-executing the side effects of a failed attempt.
    """
    argv = [*executable_prefix, "orchestrate", "flow", "--resume", target]
    if allow_degraded_context:
        argv.append("--allow-degraded-context")
    if retry_failed:
        argv.append("--retry-failed")
    return argv


async def _active_flow_resume(run_id: str) -> dict[str, Any] | None:
    """Same admission-guard shape as _active_resume_for_branch, keyed on the
    distinct resume:flow skill so agent and flow resumes for the same source
    never mask each other's in-progress state."""
    async with StateDB(readonly=read_only_open_supported()) as db:
        rows = await db.list_invocations(
            skill="resume:flow",
            status="running",
            limit=200,
            offset=0,
        )
    for row in rows:
        metadata = row.get("node_metadata")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = None
        if isinstance(metadata, dict) and metadata.get("run_id") == run_id:
            return {**row, "node_metadata": metadata}
    return None


async def _resume_flow_run(
    run_id: str,
    *,
    invocation_kind: str,
    allow_degraded_context: bool,
    retry_failed: bool,
) -> dict[str, Any]:
    """Launch a checkpointed flow/play/show-play resume.

    Unlike the agent path there is no branch to reopen: `li o flow --resume`
    replays the persisted plan from the checkpoint, so the only inputs are
    the target id and the explicit degraded-context opt-in.
    """
    # Determine resumability BEFORE offering/launching the action: a run
    # with no checkpoint is a distinct, explicit state, not a generic
    # detached-launch failure or an empty success payload. Checked before
    # the executable lookup so the persisted checkpoint fact — which the UI
    # can determine independent of the daemon's own launch environment —
    # always wins over a launch-environment error like a missing `li`.
    run_dir, _checkpoint = await _resolve_flow_checkpoint(run_id)

    executable_prefix, resolve_error = _subprocess.resolve_li_executable()
    if executable_prefix is None:
        if resolve_error:
            _log.error("Could not resolve the installed `li` executable: %s", resolve_error)
        raise _launches.LiExecutableUnavailableError(
            "The Studio daemon could not resolve the installed `li` executable; "
            "reinstall LionAGI with the Studio extra and restart Studio"
        )

    async with _resume_admission_lock:
        active = await _active_flow_resume(run_id)
        if active is not None:
            metadata = active["node_metadata"]
            if (
                metadata.get("allow_degraded_context") == allow_degraded_context
                and bool(metadata.get("retry_failed")) == retry_failed
            ):
                return {
                    "run_id": run_id,
                    "invocation_kind": invocation_kind,
                    "invocation_id": active["id"],
                    "checkpoint_run_id": run_dir.run_id,
                }
            raise RunResumeInProgressError(f"Run {run_id!r} already has a flow resume in progress")

        argv = _build_flow_resume_argv(
            executable_prefix,
            target=run_id,
            allow_degraded_context=allow_degraded_context,
            retry_failed=retry_failed,
        )
        invocation_id = await _launches.launch_detached_argv(
            argv,
            skill="resume:flow",
            plugin="studio_run_resume",
            prompt=None,
            tmp_path=None,
            action_kind="flow",
            node_metadata={
                "run_id": run_id,
                "invocation_kind": invocation_kind,
                "resume": True,
                "allow_degraded_context": allow_degraded_context,
                "retry_failed": retry_failed,
                "checkpoint_run_id": run_dir.run_id,
            },
        )
    return {
        "run_id": run_id,
        "invocation_kind": invocation_kind,
        "invocation_id": invocation_id,
        "checkpoint_run_id": run_dir.run_id,
    }


async def _dispatch_resume_by_kind(
    run_id: str,
    session: dict[str, Any],
    *,
    instruction: str | None,
    branch_id: str | None,
    model: str | None,
    allow_degraded_context: bool,
    retry_failed: bool,
) -> dict[str, Any]:
    """Route an already-fetched session row to its resume path by invocation_kind.

    Split out from resume_run so the NULL/unknown-kind refusal (the one
    behavior the DB's own CHECK constraint can never let a live row exercise,
    since it restricts the column to exactly the known vocabulary) is
    directly unit-testable against a synthetic session dict.
    """
    kind = session.get("invocation_kind")

    if kind == "agent":
        if instruction is None:
            raise ValueError("instruction is required to resume an agent run")
        return await _resume_agent_run(
            run_id, instruction=instruction, branch_id=branch_id, model=model
        )

    if kind in FLOW_RESUME_KINDS:
        if instruction is not None:
            raise ValueError(
                f"invocation_kind {kind!r} replays the persisted checkpoint plan; "
                "instruction is not accepted"
            )
        if branch_id is not None:
            raise ValueError(
                f"invocation_kind {kind!r} replays the persisted checkpoint plan; "
                "branch_id is not accepted"
            )
        if model is not None:
            raise ValueError(
                f"invocation_kind {kind!r} replays the persisted checkpoint plan; "
                "model is not accepted"
            )
        return await _resume_flow_run(
            run_id,
            invocation_kind=kind,
            allow_degraded_context=allow_degraded_context,
            retry_failed=retry_failed,
        )

    raise RunResumeUnsupportedKindError(
        f"Run {run_id!r} has invocation_kind {kind!r}, which does not support resume."
    )


async def resume_availability(run_id: str) -> dict[str, Any]:
    """Read-only resumability precheck the UI calls before rendering the resume action.

    Never launches anything and never touches the `li` executable/launch
    admission path. Reuses the exact branch/checkpoint resolution
    `resume_run` uses, so a "yes" here and the POST outcome can never
    disagree about the same run. A run with no checkpoint, an ambiguous
    target, or an unsupported invocation_kind are each a distinct,
    explicit ``resumable: False`` state with a machine-readable ``reason`` —
    never a generic failure and never something the UI could mistake for
    "still loading".
    """
    _svc_validate_identifier(run_id, "run_id")
    if state_db_known_absent():
        raise RunNotFoundError(f"Run {run_id!r} not found")

    async with StateDB(readonly=read_only_open_supported()) as db:
        session = await db.get_session(run_id)
    if session is None:
        raise RunNotFoundError(f"Run {run_id!r} not found")

    kind = session.get("invocation_kind")

    if kind == "agent":
        try:
            branch_id = await _resolve_branch(run_id, None)
        except RunBranchConflictError as exc:
            return {
                "run_id": run_id,
                "invocation_kind": kind,
                "resumable": False,
                "reason": "branch_conflict",
                "message": str(exc),
            }
        try:
            await _require_resumable_snapshot(run_id, branch_id)
        except RunResumeUnavailableError as exc:
            return {
                "run_id": run_id,
                "invocation_kind": kind,
                "resumable": False,
                "reason": "snapshot_unavailable",
                "message": str(exc),
            }
        return {
            "run_id": run_id,
            "invocation_kind": kind,
            "resumable": True,
            "branch_id": branch_id,
        }

    if kind in FLOW_RESUME_KINDS:
        try:
            run_dir, _checkpoint = await _resolve_flow_checkpoint(run_id)
        except RunResumeCheckpointError as exc:
            return {
                "run_id": run_id,
                "invocation_kind": kind,
                "resumable": False,
                "reason": exc.reason,
                "message": str(exc),
            }
        return {
            "run_id": run_id,
            "invocation_kind": kind,
            "resumable": True,
            "checkpoint_run_id": run_dir.run_id,
        }

    return {
        "run_id": run_id,
        "invocation_kind": kind,
        "resumable": False,
        "reason": "unsupported_kind",
        "message": f"Run {run_id!r} has invocation_kind {kind!r}, which does not support resume.",
    }


async def resume_run(
    run_id: str,
    *,
    instruction: str | None = None,
    branch_id: str | None = None,
    model: str | None = None,
    allow_degraded_context: bool = False,
    retry_failed: bool = False,
) -> dict[str, Any]:
    """Dispatch a resume request by the run's recorded invocation_kind.

    `agent` keeps the existing single-branch CLI resume path byte-for-byte.
    `play`/`flow`/`show-play` all replay a checkpointed flow — the checkpoint
    owns the plan, so instruction/branch/model are rejected. `fanout` has no
    checkpoint-replay mechanism at all and is refused the same way NULL and
    any other unsupported value are: before anything launches, since
    silently defaulting to one path would resume the wrong thing.
    """
    _svc_validate_identifier(run_id, "run_id")
    if state_db_known_absent():
        raise RunNotFoundError(f"Run {run_id!r} not found")

    async with StateDB(readonly=read_only_open_supported()) as db:
        session = await db.get_session(run_id)
    if session is None:
        raise RunNotFoundError(f"Run {run_id!r} not found")

    return await _dispatch_resume_by_kind(
        run_id,
        session,
        instruction=instruction,
        branch_id=branch_id,
        model=model,
        allow_degraded_context=allow_degraded_context,
        retry_failed=retry_failed,
    )


@studio_route(
    "/runs/{run_id}/resume",
    method="GET",
    area="runs",
    name="resume_availability",
)
async def resume_availability_route(run_id: str) -> dict[str, Any]:
    """Read-only precheck: can this run be resumed, and why/why not.

    The UI calls this before rendering the resume action so a run with no
    checkpoint reads as an explicit, explained state rather than a dead or
    guessed-at control.
    """
    try:
        return await resume_availability(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@studio_route(
    "/runs/{run_id}/resume",
    method="POST",
    area="runs",
    status_code=202,
    name="resume_run",
)
async def resume_run_route(run_id: str, body: RunResumeRequest) -> dict[str, Any]:
    """Resume any run that has an underlying branch or checkpoint, dispatched by invocation_kind."""
    try:
        return await resume_run(
            run_id,
            instruction=body.instruction,
            branch_id=body.branch_id,
            model=body.model,
            allow_degraded_context=body.allow_degraded_context,
            retry_failed=body.retry_failed,
        )
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RunResumeCheckpointError as exc:
        raise HTTPException(
            status_code=409, detail={"reason": exc.reason, "message": str(exc)}
        ) from exc
    except (
        RunBranchConflictError,
        RunResumeInProgressError,
        RunResumeUnsupportedKindError,
        RunResumeUnavailableError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (RunBranchMembershipError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except _launches.TooManyLaunchesError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except _launches.LiExecutableUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
