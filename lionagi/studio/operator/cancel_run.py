# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Studio Operator lifecycle service/adapter: ``cancel_run``.

Cancels one Studio run (a `sessions` row) by reference -- a run id, an id
prefix, a name substring, or ``"current"`` -- gated on the same durable
human allow/deny proposal flow ``launch_playbook`` uses
(`application_mcp.py::launch_playbook`). Real in-process cancellation only:
the state-changing act reuses the exact primitives ``li kill`` uses
(``lionagi.cli._util.fetch_unique_row`` for id/prefix resolution,
``lionagi.cli.kill._kill_one`` for identity-checked termination), so a
cancellation through the Operator is indistinguishable from one run through
``li kill`` directly -- no subprocess is spawned. See
docs/internals/studio.md ("Turn identity and the propose/poll/execute
pattern") for why ``resume_run`` is a separate operation rather than an
un-gate of a cancelled run's status.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .redact import public_project
from .store import OperatorStore

CANCEL_RUN_COMMAND_TYPE = "cancel"

CANCEL_RUN_DESCRIPTION = (
    "Cancel a running run by id, session id, name, or 'current'. This sends a "
    "termination signal to the run's process through the exact in-process "
    "path `li kill` uses -- no shell subprocess is spawned. "
    "Cancellation goes through a human approval flow; it is never automatic. "
    "A denied proposal leaves the run untouched. "
    "Safe to call on an already-terminated run: it reports the existing "
    "terminal status rather than sending a duplicate signal, and does not "
    "create a proposal for a run there is nothing left to do to. "
    "Does not support mass cancellation -- one run at a time. "
    "Accepts a run UUID, an 8+ hex id prefix, a name substring (minimum 3 "
    "characters), or 'current' for the run open when this instruction was "
    "sent. Ambiguous references return candidates rather than guessing. "
    "Use resume_run to continue a run's conversation with a new instruction "
    "instead -- that launches a new invocation rather than reopening this "
    "run's status."
)


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CancelRunInput(_StrictInput):
    run: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=500)


class AmbiguousRunReferenceError(ValueError):
    """A run reference matched more than one session; never guess."""

    def __init__(self, candidates: list[str]) -> None:
        self.candidates = list(candidates)
        super().__init__(f"ambiguous run reference -- matches {len(candidates)} sessions")


class MissingOwnerContextError(ValueError):
    """The calling turn has no durable project mapping to authorize against.

    Raised before any row is resolved, signalled, or reported on -- a turn
    with no owner/project context must never fall back to matching every
    project's runs. Mirrors ``run_progress.py``'s own copy of this error --
    kept separate rather than a shared import for the same reason
    ``_allowed_project`` below is its own copy.
    """

    code = "missing_owner_context"


def _identity() -> tuple[OperatorStore, str, str]:
    import os

    db_path = os.environ.get("LIONAGI_OPERATOR_DB_PATH")
    conversation_id = os.environ.get("LIONAGI_OPERATOR_CONVERSATION_ID")
    request_id = os.environ.get("LIONAGI_OPERATOR_REQUEST_ID")
    if not db_path or not conversation_id or not request_id:
        raise RuntimeError("Studio application bridge is missing its durable turn identity")
    return OperatorStore(db_path), conversation_id, request_id


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# A full canonical UUID (36 characters, 8-4-4-4-12 hex). Mirrors
# ``run_progress.py``'s own copy, kept separate for the same reason
# ``_allowed_project`` is: this module's identity/store handling stays
# self-contained.
_EXACT_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


async def _current_run_id(store: OperatorStore, request_id: str) -> str | None:
    """The run the human was looking at when this instruction was sent.

    Deliberately reads the turn's OWN frozen context rather than a
    later-reported live view -- acting on a view reported after the
    instruction was sent could target a run the human was never looking at
    when they said "stop it". See docs/internals/studio.md ("Resolving a
    run reference") for why this reads the "s" key specifically.
    """
    turn = await store.get_turn(request_id)
    context = turn.get("context")
    if not isinstance(context, dict):
        return None
    selection = context.get("selection")
    if not isinstance(selection, dict):
        return None
    value = selection.get("s")
    return value if isinstance(value, str) and value else None


async def _allowed_project(store: OperatorStore, request_id: str) -> str:
    """The project this Operator turn is scoped to.

    Raises :class:`MissingOwnerContextError` rather than returning a
    sentinel when the turn's own context names no project -- a turn with no
    owner mapping must never be treated as authorized for every project's
    runs. A separate small copy of ``run_progress.py::_allowed_project``, so
    this module's identity/store handling stays self-contained.
    """
    turn = await store.get_turn(request_id)
    context = turn.get("context")
    project = context.get("project") if isinstance(context, dict) else None
    if not isinstance(project, str) or not project:
        raise MissingOwnerContextError(
            "operator turn has no project context -- refusing to resolve or "
            "cancel a run by prefix or name. Pass the run's full 36-character "
            "id instead, or cancel the run the human has open ('current')."
        )
    return project


def _owns(row_project: Any, project: str) -> bool:
    """Whether a session's ``project`` column is visible to a turn scoped to
    ``project``. ``project`` is always a real, non-empty value -- callers
    obtain it from ``_allowed_project``, which raises rather than returning
    a sentinel for "no owner", so there is no value here that means "every
    project"."""
    return row_project == project


async def _owned_rows(db: Any, ids: list[str], project: str) -> list[dict[str, Any]]:
    """Re-fetch *ids* and keep only the rows ``project`` may see, preserving
    the input order. Used to strip foreign-project rows out of an
    ``AmbiguousIdError``'s candidate list before it ever reaches the human --
    a foreign row must not even be visible as an ambiguity candidate."""
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = await db.fetch_all(
        f"SELECT * FROM sessions WHERE id IN ({placeholders})",  # noqa: S608
        tuple(ids),
    )
    by_id = {row["id"]: db._row_to_dict(row) for row in rows}
    return [by_id[i] for i in ids if i in by_id and _owns(by_id[i].get("project"), project)]


async def _resolve_run(db: Any, ref: str, *, project: str) -> dict[str, Any] | None:
    """Resolve *ref* to exactly one `sessions` row (a Studio "run").

    Mirrors `lionagi.cli._util.fetch_unique_row`'s exact-id-then-prefix
    discipline, narrowed to the `sessions` table. A prefix matching more
    than one session raises `AmbiguousRunReferenceError` rather than picking
    one -- never guess which process to signal. Every arm is scoped to
    ``project`` (the calling turn's own; see ``_allowed_project``): a
    foreign exact/prefix match is reported exactly like a nonexistent one
    (``None``), never as e.g. "already terminal", which would itself
    confirm the id exists. See docs/internals/studio.md ("Resolving a run
    reference").
    """
    from lionagi.cli._util import AmbiguousIdError, fetch_unique_row

    ref = ref.strip()
    if not ref:
        return None

    try:
        row = await fetch_unique_row(db, "sessions", ref)
    except AmbiguousIdError as exc:
        owned = await _owned_rows(db, exc.candidates, project)
        if not owned:
            return None
        if len(owned) == 1:
            return owned[0]
        raise AmbiguousRunReferenceError([r["id"] for r in owned]) from exc
    if row is not None:
        row_dict = db._row_to_dict(row)
        if not _owns(row_dict.get("project"), project):
            return None
        return row_dict

    if len(ref) < 3:
        return None

    pattern = f"%{_escape_like(ref)}%"
    rows = await db.fetch_all(
        "SELECT * FROM sessions WHERE (name LIKE ? ESCAPE '\\' OR playbook_name LIKE ? ESCAPE '\\') "
        "AND project = ? ORDER BY started_at DESC LIMIT 11",
        (pattern, pattern, project),
    )
    if not rows:
        return None
    if len(rows) > 1:
        raise AmbiguousRunReferenceError([db._row_to_dict(r)["id"] for r in rows])
    return db._row_to_dict(rows[0])


async def _resolve_reference(store: OperatorStore, request_id: str, ref: str) -> dict[str, Any]:
    """Resolve *ref* to a tool-facing outcome. Never guesses, never raises
    for an ordinary resolution failure -- ambiguous and not-found are both
    first-class reported outcomes."""
    from lionagi.state.db import StateDB

    if ref == "current":
        run_id = await _current_run_id(store, request_id)
        if run_id is None:
            return {"found": False}
        ref = run_id

    ref = ref.strip()
    try:
        project = await _allowed_project(store, request_id)
    except MissingOwnerContextError:
        if _EXACT_UUID_RE.fullmatch(ref) is None:
            raise
        # A turn with an owner but no declared project may still resolve one
        # run by its full id: an exact 36-character UUID identifies at most
        # one row and cannot enumerate anything, the same position
        # ``run_detail`` already takes for a bare id. Prefix and
        # name-substring resolution stay behind the fence, and a turn that
        # *does* declare a project keeps full ownership scoping on every arm.
        async with StateDB() as db:
            row = await db.fetch_one("SELECT * FROM sessions WHERE id = ?", (ref,))
            if row is None:
                return {"found": False}
            return {"found": True, "ambiguous": False, "run": db._row_to_dict(row)}

    async with StateDB() as db:
        try:
            row = await _resolve_run(db, ref, project=project)
        except AmbiguousRunReferenceError as exc:
            candidates = exc.candidates[:10]
            return {
                "found": True,
                "ambiguous": True,
                "candidates": candidates,
                "truncated": len(exc.candidates) > 10,
            }
        if row is None:
            return {"found": False}
        return {"found": True, "ambiguous": False, "run": row}


def _redacted_cancel_result(proposal: dict[str, Any], run_id: str) -> dict[str, Any]:
    if proposal["status"] != "succeeded":
        reason = "denied" if proposal["status"] == "cancelled" else proposal["status"]
        return {
            "cancelled": False,
            "reason": reason,
            "run_untouched": True,
            "id": run_id,
        }
    raw = proposal.get("result")
    result = raw if isinstance(raw, dict) else {}
    # A "succeeded" proposal only means the executor ran without raising --
    # it says nothing about whether the run was actually cancelled.
    # `execute_cancel_command` names its outcome in `status`; "terminal" is
    # the only value that means the database row now holds "cancelled". Every
    # other outcome (not_found, already_terminal, identity_mismatch) is the
    # process staying exactly as it was, so `cancelled` must say so.
    status = result.get("status", "unknown")
    cancelled = status == "terminal"
    return {
        "cancelled": cancelled,
        "status": status,
        "id": run_id,
        "signal": result.get("signal"),
        "run_untouched": not cancelled,
    }


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes}m"
    if minutes:
        return f"{minutes}m{secs}s"
    return f"{secs}s"


def _cancel_summary(row: dict[str, Any]) -> str:
    """A human deciding whether to approve a destructive cancel needs more
    than a bare id -- name the project, the run's own name, its status, and
    how long it has been running."""
    run_id = row["id"]
    parts = [f"Cancel run {run_id[:12]}"]
    project_label = public_project(row.get("project"))
    if project_label:
        parts.append(f"project {project_label}")
    name = row.get("name")
    if name:
        parts.append(f"'{name}'")
    status = row.get("status")
    if status:
        parts.append(f"status {status}")
    started_at = row.get("started_at")
    if isinstance(started_at, (int, float)):
        parts.append(f"running {_format_elapsed(time.time() - started_at)}")
    return " -- ".join(parts)


async def cancel_run(arguments: dict[str, Any]) -> dict[str, Any]:
    """MCP tool handler: resolve -> durable proposal -> poll -> result.

    Mirrors `application_mcp.py::launch_playbook`'s shape exactly: this
    function only creates the proposal and waits for it to leave "pending".
    The actual cancellation happens in `execute_cancel_command`, invoked by
    the coordinator once a human allows the proposal (see module docstring).
    """
    args = CancelRunInput.model_validate(arguments)
    store, conversation_id, request_id = _identity()

    resolution = await _resolve_reference(store, request_id, args.run)
    if not resolution["found"]:
        return {"cancelled": False, "reason": "not_found", "run_untouched": True}
    if resolution["ambiguous"]:
        return {
            "cancelled": False,
            "reason": "ambiguous_reference",
            "run_untouched": True,
            "candidates": resolution["candidates"],
            "truncated": resolution["truncated"],
        }

    row = resolution["run"]
    run_id = row["id"]

    if row.get("status") != "running":
        # Nothing to propose: there is no action left to gate behind a human
        # decision, so no proposal is created and no mutation callback runs.
        # This call performed no state change -- the run was already
        # terminal before it was ever asked to cancel anything -- so
        # `cancelled` must be false, not a claim that this call caused it.
        return {
            "cancelled": False,
            "status": "already_terminal",
            "id": run_id,
            "run_untouched": True,
        }

    # Carried through to `execute_cancel_command` so ownership is checked
    # again immediately before the process is signalled, not only once at
    # resolution time -- the human's approval window is a gap a run's
    # project could change across.
    command = {"session_id": run_id, "reason": args.reason, "project": row.get("project")}
    stable = store.canonical_hash(
        {
            "requestId": request_id,
            "tool": "cancel_run",
            "command": command,
        }
    )
    summary = _cancel_summary(row)
    if args.reason:
        summary += f" -- {args.reason}"
    proposal = await store.create_proposal(
        conversation_id,
        request_id,
        command_type=CANCEL_RUN_COMMAND_TYPE,
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
            return _redacted_cancel_result(proposal, run_id)
        await asyncio.sleep(0.1)


async def execute_cancel_command(command: dict[str, Any]) -> dict[str, Any]:
    """The real state-changing act -- the adapter's other half.

    Wire this into `OperatorCoordinator`'s ``command_executor`` for
    ``command_type == "cancel"``. Re-resolves the session by exact id and
    re-checks ownership at execution time rather than trusting the
    resolution `cancel_run` captured. `_persist_cancel` is a no-op once the
    row is no longer 'running', so a race here degrades to
    ``already_terminal`` -- never a double cancel, never a wrong-run cancel.
    Reuses `lionagi.cli.kill`'s deepest-first child traversal
    (``_list_running_children``/``_kill_one``) so a still-running owning
    invocation is reaped exactly the way ``li kill --recursive`` does,
    rather than orphaned when the parent session goes terminal.
    """
    from lionagi.cli.kill import _kill_one, _list_running_children
    from lionagi.state.db import StateDB

    run_id = command.get("session_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("cancel command is missing session_id")
    reason = command.get("reason") or ""
    project = command.get("project")
    user_reason = f"Operator: {reason}" if reason else "Operator"

    async with StateDB() as db:
        row = await db.fetch_one("SELECT * FROM sessions WHERE id = ?", (run_id,))
        if row is None:
            return {"status": "not_found", "id": run_id}
        row_dict = db._row_to_dict(row)
        # The command carries the project the resolved row held at proposal
        # time. Two arms, both failing toward not_found rather than
        # disclosure: a command with a real project must match the row's
        # exactly, and a command with NO project (built from the exact-id
        # fence arm for a row that had none -- Operator-launched runs have
        # no project today) matches only a row that STILL has none. A row
        # that gained an owner during the human's approval window fails
        # closed, and a project-less command can never touch an owned row.
        row_project = row_dict.get("project")
        if isinstance(project, str) and project:
            if row_project != project:
                return {"status": "not_found", "id": run_id}
        elif isinstance(row_project, str) and row_project:
            return {"status": "not_found", "id": run_id}
        if row_dict.get("status") != "running":
            return {"status": "already_terminal", "id": run_id}

        # Deepest first: a running child is signalled before its parent, so
        # it is never left orphaned by the parent going terminal ahead of it.
        # Best-effort per child -- an identity-mismatched or already-gone
        # child does not block the parent's own cancellation below.
        children = await _list_running_children(db, "session", run_id)
        for _table, child_type, child_row in children:
            await _kill_one(db, child_type, child_row["id"], child_row, user_reason=user_reason)

        outcome = await _kill_one(
            db,
            "session",
            run_id,
            row_dict,
            user_reason=user_reason,
        )
        if outcome["signal"] == "identity_mismatch":
            # _kill_one() returns without calling _persist_cancel for an
            # identity mismatch -- nothing was written. Report that directly
            # rather than re-reading a row that is guaranteed unchanged.
            return {"status": "identity_mismatch", "id": run_id, "signal": outcome["signal"]}

        # `_persist_cancel` guards its write on the row still being 'running'
        # at persist time (lionagi/cli/kill.py) -- a race that lets the run
        # finish naturally during the human approval window makes that write
        # a no-op rather than an overwrite. Re-read the row instead of
        # inferring success from the OS-signal outcome, so this adapter never
        # claims a cancellation the database does not actually hold.
        after = await db.fetch_one("SELECT status FROM sessions WHERE id = ?", (run_id,))
        after_status = db._row_to_dict(after).get("status") if after is not None else None

    status = "terminal" if after_status == "cancelled" else "already_terminal"
    return {"status": status, "id": run_id, "signal": outcome["signal"]}
