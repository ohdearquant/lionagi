# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Studio Operator lifecycle service/adapter: ``rename_session``.

Gives one Studio run (a `sessions` row) a human name through the Operator,
gated on the same durable human allow/deny proposal flow `cancel_run` and
`resume_run` use. Deliberately distinct from renaming the Operator's own
*conversation* (`store.py::update_conversation`, a direct human UI/REST
action) -- see docs/internals/studio.md ("Turn identity and the
propose/poll/execute pattern"). Reuses `run_progress.py::resolve_run` for
reference resolution rather than a third private copy of that logic.
"""

from __future__ import annotations

import asyncio
import time
import unicodedata
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .redact import public_project
from .run_progress import resolve_run
from .store import OperatorStore

# Cc/Cf: C0/C1 controls (incl. NUL, DEL) and Unicode formatting chars (incl.
# bidi overrides) -- not printable characters a human ever intends to type
# into a name. Zl/Zp: line/paragraph separators -- structurally the same
# problem as an embedded newline. Deliberately category-based rather than an
# enumerated blocklist so it does not need updating as new control code
# points are assigned. Zs (ordinary space) is NOT included -- an internal
# space is a normal, expected part of a name.
_REJECTED_NAME_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})

RENAME_SESSION_COMMAND_TYPE = "rename_session"

RENAME_SESSION_DESCRIPTION = (
    "Give one Studio run a human name. This renames the run's own record "
    "only -- it is not the Operator conversation's own name, which has a "
    "separate rename path outside this tool. Goes through a human approval "
    "flow; it is never automatic, and a denied proposal leaves the run's "
    "name untouched. Accepts a run UUID, an 8+ hex id prefix, a name "
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
        for char in value:
            category = unicodedata.category(char)
            if category in _REJECTED_NAME_CATEGORIES:
                raise ValueError(
                    f"name must not contain control or line/paragraph-separator "
                    f"characters (found {char!r}, category {category})"
                )
        return value


def _identity() -> tuple[OperatorStore, str, str]:
    import os

    db_path = os.environ.get("LIONAGI_OPERATOR_DB_PATH")
    conversation_id = os.environ.get("LIONAGI_OPERATOR_CONVERSATION_ID")
    request_id = os.environ.get("LIONAGI_OPERATOR_REQUEST_ID")
    if not db_path or not conversation_id or not request_id:
        raise RuntimeError("Studio application bridge is missing its durable turn identity")
    return OperatorStore(db_path), conversation_id, request_id


def _rename_summary(row: dict[str, Any], new_name: str) -> str:
    """A human deciding whether to approve a rename needs more than a bare
    id -- the project, the run's current name (if any), and the proposed one."""
    run_id = row["id"]
    parts = [f"Rename run {run_id[:12]}"]
    project_label = public_project(row.get("project"))
    if project_label:
        parts.append(f"project {project_label}")
    old_name = row.get("name")
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

    resolution = await resolve_run(args.run)
    if not resolution["found"]:
        return {"renamed": False, "reason": "not_found"}
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

    row_project = row_dict.get("project")
    if not isinstance(row_project, str) or not row_project:
        # Only reachable through resolve_run()'s exact-id arm: a fenced
        # resolution always yields a row matching the turn's own non-empty
        # project. The execute-time ownership guard refuses a row with no
        # project, so creating a proposal for one would burn a human
        # approval on an act that cannot execute -- report it the way the
        # executor would.
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
    while True:
        proposal = await store.get_proposal(proposal["id"])
        status = proposal["status"]
        if status == "pending" and proposal["expiresAt"] <= time.time():
            proposal = await store.expire_proposal(proposal["id"])
            status = proposal["status"]
        if status in {"succeeded", "failed", "cancelled", "expired", "conflict"}:
            return _redacted_rename_result(proposal, run_id)
        await asyncio.sleep(0.1)


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
        # A command built by rename_session() above always carries the
        # resolved row's own (non-empty) project -- rename_session() refuses
        # to propose for a row without one. A missing or empty project
        # here is itself an ownership failure, not a value meaning
        # "unscoped, allow any row" -- it fails exactly like a project
        # mismatch and exactly like a nonexistent id, the same reasoning
        # `execute_cancel_command` documents.
        if not isinstance(project, str) or not project or row_dict.get("project") != project:
            return {"status": "not_found", "id": run_id}
        await db.update_session(run_id, name=name)

    return {"status": "renamed", "id": run_id, "name": name}
