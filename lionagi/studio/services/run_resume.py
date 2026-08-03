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
    """Another queued or executing resume already owns this branch."""


class RunResumeRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=MAX_SPEC_PROMPT_CHARS)
    branch_id: str | None = None
    model: str | None = None


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


async def resume_run(
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

        source_status = await _run_status(run_id)
        from lionagi.state.db import SESSION_TERMINAL_STATUSES

        queued = source_status not in SESSION_TERMINAL_STATUSES
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
            await _ensure_branch_snapshot_available(resolved_branch_id)
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


@studio_route(
    "/runs/{run_id}/resume",
    method="POST",
    area="runs",
    status_code=202,
    name="resume_run",
)
async def resume_run_route(run_id: str, body: RunResumeRequest) -> dict[str, Any]:
    """Resume any run that has an underlying branch, including terminal runs."""
    try:
        return await resume_run(
            run_id,
            instruction=body.instruction,
            branch_id=body.branch_id,
            model=body.model,
        )
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        RunBranchConflictError,
        RunResumeInProgressError,
        RunResumeUnavailableError,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (RunBranchMembershipError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except _launches.TooManyLaunchesError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except _launches.LiExecutableUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
