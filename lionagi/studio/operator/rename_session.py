# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Studio Operator lifecycle service/adapter, exposed to the Operator as
the ``rename_run`` tool.

The module and its internals keep the ``rename_session`` name (the durable
``command_type`` must stay stable across pending proposals), but the
tool's catalog name says *run* like every sibling (``cancel_run``,
``resume_run``): a run and its session are the same ``sessions`` row, and
a name that suggests otherwise invites the caller to refuse the rename
while hunting for a "session id" that is the id it already has.

Writes one Studio run's explicit display label through the dedicated setter,
preserving its system name and activity timestamps. The Operator command is
gated on the same durable human allow/deny proposal flow `cancel_run` and
`resume_run` use. Deliberately distinct from renaming the Operator's own
*conversation* (`store.py::update_conversation`, presentation metadata) -- see docs/internals/studio.md ("Turn identity and the
propose/poll/execute pattern"). Reuses `run_progress.py::resolve_run` for
reference resolution rather than a third private copy of that logic.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lionagi.ln.concurrency import sleep
from lionagi.state.session_naming import resolve_display_name, validate_name_characters

from . import _common
from .redact import public_project
from .run_progress import MissingOwnerContextError, resolve_run
from .store import OperatorStore

RENAME_SESSION_COMMAND_TYPE = "rename_session"

RENAME_SESSION_DESCRIPTION = (
    "Give one Studio run a human name. A run and its session are the same "
    "record here, so this sets the run's own label -- it is not the Operator "
    "conversation's own name, which has a "
    "separate rename path outside this tool. Goes through a human approval "
    "flow; it is never automatic, and a denied proposal leaves the run's "
    "name untouched. Accepts a run UUID, an id prefix, a label or name "
    "substring (minimum 3 characters), or 'current' for the run open when "
    "this instruction was sent. Ambiguous references return candidates "
    "rather than guessing."
)


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RenameSessionInput(_StrictInput):
    run: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=160)

    @field_validator("name")
    @classmethod
    def _reject_control_characters(cls, value: str) -> str:
        return validate_name_characters(value)


def _identity() -> tuple[OperatorStore, str, str]:
    return _common._identity(OperatorStore)


def _rename_summary(row: dict[str, Any], new_name: str) -> str:
    """A human deciding whether to approve a rename needs more than a bare
    id -- the project, the run's current name (if any), and the proposed one."""
    run_id = row["id"]
    parts = [f"Rename run {run_id[:12]}"]
    project_label = public_project(row.get("project"))
    if project_label:
        parts.append(f"project {project_label}")
    old_name = resolve_display_name(row)
    if old_name:
        parts.append(f"from '{old_name}'")
    parts.append(f"to '{new_name}'")
    return " -- ".join(parts)


def _redacted_rename_result(proposal: dict[str, Any], run_id: str) -> dict[str, Any]:
    if proposal["status"] != "succeeded":
        reason = "denied" if proposal["status"] == "cancelled" else proposal["status"]
        return {"renamed": False, "reason": reason, "id": run_id}
    raw = proposal.get("result")
    result = raw if isinstance(raw, dict) else {}
    # A "succeeded" proposal only means the executor ran without raising --
    # it says nothing about whether the row was actually renamed. Only
    # `status == "renamed"` means the database write happened; every other
    # outcome (not_found) leaves the run exactly as it was.
    status = result.get("status", "unknown")
    return {
        "renamed": status == "renamed",
        "status": status,
        "id": run_id,
        "name": result.get("name"),
    }


async def rename_session(arguments: dict[str, Any]) -> dict[str, Any]:
    """MCP tool handler: resolve -> durable proposal -> poll -> result.

    Mirrors `cancel_run.py::cancel_run` / `resume_run.py::resume_run`'s
    shape exactly: this function only creates the proposal and waits for it
    to leave "pending". The actual rename happens in
    `execute_rename_session_command`, invoked by the coordinator once a
    human allows the proposal.
    """
    args = RenameSessionInput.model_validate(arguments)
    store, conversation_id, request_id = _identity()

    # A turn that declares no project cannot prove it owns the run it names,
    # and a rename mutates that run: report the refusal as a reason code
    # rather than resolving. Mirrors ``run_control``'s refusal shape.
    try:
        resolution = await resolve_run(args.run, allow_unscoped_exact_id=False)
    except MissingOwnerContextError:
        return {"renamed": False, "reason": "missing_owner_context"}
    if not resolution["found"]:
        return {
            "renamed": False,
            "reason": "not_found",
            "detail": resolution.get("reason"),
        }
    if resolution.get("ambiguous"):
        return {
            "renamed": False,
            "reason": "ambiguous_reference",
            "candidates": resolution["candidates"],
            "truncated": resolution.get("truncated", False),
        }

    run_id = resolution["session_id"]
    from lionagi.state.db import StateDB

    async with StateDB() as db:
        row = await db.fetch_one("SELECT * FROM sessions WHERE id = ?", (run_id,))
        row_dict = db._row_to_dict(row) if row is not None else None
    if row_dict is None:
        # resolve_run() already scoped the match to this turn's project, so
        # this row disappearing between resolution and this re-read is a
        # genuine race (the run was deleted), not an ownership question.
        return {"renamed": False, "reason": "not_found"}

    # Carried through to `execute_rename_session_command` so ownership is
    # checked again immediately before the row is written, not only once at
    # resolution time -- the human's approval window is a gap a run's
    # project could change across, the same reasoning `cancel_run` documents.
    command = {"session_id": run_id, "name": args.name, "project": row_dict.get("project")}
    stable = store.canonical_hash(
        {"requestId": request_id, "tool": "rename_session", "command": command}
    )
    proposal = await store.create_proposal(
        conversation_id,
        request_id,
        command_type=RENAME_SESSION_COMMAND_TYPE,
        command=command,
        risk="mutate",
        summary=_rename_summary(row_dict, args.name),
        idempotency_key=f"operator-app:{stable}",
    )
    proposal = await _common.wait_for_proposal(
        store,
        proposal,
        clock=lambda: time.time(),
        sleep=lambda interval: sleep(interval),
    )
    return _redacted_rename_result(proposal, run_id)


async def execute_rename_session_command(command: dict[str, Any]) -> dict[str, Any]:
    """The real state-changing act -- the adapter's other half.

    Wire this into `OperatorCoordinator`'s ``command_executor`` for
    ``command_type == "rename_session"`` (see `coordinator.py`'s
    ``_execute_application_command``). Re-resolves the session by exact id
    and re-checks ownership at execute time rather than trusting the
    resolution captured in the command -- mirrors
    `cancel_run.py::execute_cancel_command`'s same guard for the same
    reason: the human's approval window is a gap the run's project could
    change across.
    """
    from lionagi.state.db import StateDB

    run_id = command.get("session_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("rename command is missing session_id")
    name = command.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("rename command is missing name")
    project = command.get("project")

    async with StateDB() as db:
        row = await db.fetch_one("SELECT * FROM sessions WHERE id = ?", (run_id,))
        if row is None:
            return {"status": "not_found", "id": run_id}
        row_dict = db._row_to_dict(row)
        # The command carries the project the resolved row held at proposal
        # time. Two arms, both failing toward not_found: a command with a
        # real project must match the row's exactly, and a command with NO
        # project (built from the exact-id fence arm for a row that had
        # none -- Operator-launched runs have no project today) matches
        # only a row that STILL has none. Same reasoning
        # `execute_cancel_command` documents.
        row_project = row_dict.get("project")
        if isinstance(project, str) and project:
            if row_project != project:
                return {"status": "not_found", "id": run_id}
        elif isinstance(row_project, str) and row_project:
            return {"status": "not_found", "id": run_id}
        if not await db.set_session_user_label(run_id, name):
            return {"status": "not_found", "id": run_id}

    return {"status": "renamed", "id": run_id, "name": name}
