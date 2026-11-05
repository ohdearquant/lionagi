# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Lookup helpers shared by the read-only lifecycle commands.

Status vocabularies, exit codes and entity resolution used by `li agent status`,
`li play status`, `li o ctl status` and the commands that wait on or act on a run.
This module imports no command module.
"""

from __future__ import annotations

import re
from typing import Any

from ._util import (
    _CANDIDATES_SHOWN,
    AmbiguousIdError,
    _like_prefix_pattern,
    fetch_unique_row,
    resolve_entity,
)

# ── Status vocabularies (mirror the CHECK constraints / VALID_* sets in state/db.py) ──
# "cancelled" lands in FAILURE by elimination (neither success nor still-running).

_SESSION_SUCCESS = frozenset({"completed"})
# "completed_empty" (ran clean but produced no commits/artifacts — the
# completion-trust gate) is classified as failure here, not success.
_SESSION_FAILURE = frozenset({"completed_empty", "failed", "timed_out", "aborted", "cancelled"})
_PLAY_SUCCESS = frozenset({"merged"})
_PLAY_FAILURE = frozenset({"gate_failed", "escalated", "blocked", "aborted_after_finish"})

EXIT_RUNNING = 3
EXIT_UNKNOWN = 2

# StateDB.open() always runs a schema-apply step under a write transaction,
# even for pure reads — this bounds a status read so it fails fast, not hangs.
_DB_BUSY_TIMEOUT_S = 10.0


# ── Entity resolution ───────────────────────────────────────────────────────


async def fetch_by_id(db: Any, table: str, id_or_short: str) -> dict[str, Any] | None:
    """Exact-id fetch, then unique-prefix fetch, scoped to one table (see
    _util.resolve_entity for the multi-table sweep).

    Raises `AmbiguousIdError` when a prefix matches more than one row; the
    status entry points turn that into EXIT_UNKNOWN, never a status readout
    for an arbitrarily chosen run.
    """
    row = await fetch_unique_row(db, table, id_or_short)
    return db._row_to_dict(row) if row is not None else None


async def _latest_session(
    db: Any, *, invocation_kinds: tuple[str, ...], project: str | None
) -> dict[str, Any] | None:
    """Newest session (by updated_at) among *invocation_kinds*, optionally project-scoped."""
    placeholders = ", ".join("?" for _ in invocation_kinds)
    sql = f"SELECT * FROM sessions WHERE invocation_kind IN ({placeholders})"  # noqa: S608
    params: list[Any] = list(invocation_kinds)
    if project:
        sql += " AND project = ?"
        params.append(project)
    sql += " ORDER BY updated_at DESC LIMIT 1"
    row = await db.fetch_one(sql, params)
    return db._row_to_dict(row) if row is not None else None


async def _resolve_session_by_branch_id(db: Any, entity_id: str) -> dict[str, Any] | None:
    """Fallback: resolve *entity_id* as a branch_id to its owning session.

    See docs/internals/cli.md for the branches/sessions schema note.
    """
    branch = await fetch_by_id(db, "branches", entity_id)
    if branch is None:
        return None
    return await db.get_session(branch["session_id"])


# A run id is `YYYYMMDDTHHMMSS-hex6` (`_runs._new_run_id`). Every character before the `T`
# is a digit, and digits are valid hex, so a prefix that stops short of the `T` can also begin
# an entity id, or simply be a number or a date. A prefix that reaches it cannot begin a UUID.
RUN_ID_THROUGH_T = re.compile(r"\d{8}T")


async def _resolve_session_by_run_id(
    db: Any, entity_id: str, *, prefix_through_t: bool = False
) -> dict[str, Any] | None:
    """Fallback: resolve *entity_id* as a CLI run id to its most-recent session.

    Run ids are not primary keys `_util.resolve_entity` searches — they live
    in a separate id space (one directory per run under `LIONAGI_HOME/runs/`,
    allocated in `cli/_runs.py`) and are only mirrored onto sessions through
    the nullable `sessions.run_id` column. A run id is the handle an operator
    actually holds for an agent leg, so this fallback exists for the same
    reason `_resolve_session_by_branch_id` does: the generic resolver has
    nothing to look up.

    Exact id wins outright, same as every other resolver here. `run_id` carries
    no uniqueness constraint (`get_sessions_for_run` already documents that
    one run can persist more than one session), so the most recently updated
    one is returned rather than assuming a 1:1 mapping. A prefix that fits
    more than one *distinct* run id raises `AmbiguousIdError`, for the same
    reason a colliding table prefix does: there is no correct row to prefer.

    With *prefix_through_t*, a prefix resolves only once it reaches the `T` after
    the date (ADR-0108, Amendment 2); the commands that act on a run ask for it.
    """
    entity_id = entity_id.strip()
    if not entity_id:
        return None

    exact = await db.get_sessions_for_run(entity_id)
    if exact:
        return max(exact, key=lambda s: s.get("updated_at") or 0)

    if prefix_through_t and not RUN_ID_THROUGH_T.match(entity_id):
        return None
    run_ids = await run_id_candidates(db, entity_id)
    if not run_ids:
        return None
    if len(run_ids) > 1:
        raise AmbiguousIdError(entity_id, "run", run_ids)

    sessions = await db.get_sessions_for_run(run_ids[0])
    return max(sessions, key=lambda s: s.get("updated_at") or 0) if sessions else None


async def run_id_candidates(db: Any, entity_id: str) -> list[str]:
    """Distinct run ids *entity_id* matches as a prefix, capped for display.

    Separate from the resolver above so a caller can ask whether a string also
    lands in the run-id space without resolving it to a session and acting on
    the result.
    """
    rows = await db.fetch_all(
        "SELECT DISTINCT run_id FROM sessions WHERE run_id LIKE ? ESCAPE '\\' "
        "AND substr(run_id, 1, ?) = ? ORDER BY run_id LIMIT ?",
        (
            _like_prefix_pattern(entity_id),
            len(entity_id),
            entity_id,
            _CANDIDATES_SHOWN + 1,
        ),
    )
    return [r["run_id"] for r in rows]


async def resolve_agent_target(
    db: Any, entity_id: str | None, project: str | None
) -> tuple[str, dict[str, Any]] | None:
    """`li agent status` resolution: session (any kind), invocation, or a
    branch_id, by id; default-latest is scoped to agent-kind sessions.

    Sessions and invocations are searched together: a prefix that fits one of
    each is ambiguous, and reporting the session because it is looked at first
    is search order deciding a question it cannot answer.
    """
    if entity_id:
        hit = await resolve_entity(db, entity_id, tables=("sessions", "invocations"))
        if hit is not None:
            _table, entity_type, row = hit
            return entity_type, row
        row = await _resolve_session_by_branch_id(db, entity_id)
        if row is not None:
            return "session", row
        return None
    row = await _latest_session(db, invocation_kinds=("agent",), project=project)
    return ("session", row) if row is not None else None


async def resolve_play_target(
    db: Any, entity_id: str | None, project: str | None
) -> tuple[str, dict[str, Any]] | None:
    """`li play status` resolution: session, invocation, or a show sub-play
    row, by id; default-latest is scoped to play/flow-kind sessions.

    The three kinds are searched together, for the same reason `li agent
    status` searches its two together.
    """
    if entity_id:
        hit = await resolve_entity(db, entity_id, tables=("sessions", "invocations", "plays"))
        if hit is not None:
            _table, entity_type, row = hit
            return entity_type, row
        return None
    row = await _latest_session(db, invocation_kinds=("play", "flow"), project=project)
    return ("session", row) if row is not None else None


async def resolve_any_target(
    db: Any, entity_id: str, *, run_prefix_through_t: bool = False
) -> tuple[str, dict[str, Any]] | None:
    """`li o ctl status <id>` resolution: no kind scoping, id required (no
    latest). Falls back to branch_id, then run_id, after sessions/invocations/plays.

    The kinds are searched together rather than one after another. Trying each
    in turn and keeping the first hit resolves a prefix that fits a session and
    an invocation to whichever is looked at first, and the commands built on
    this resolver act: `li o ctl pause` would queue a control for a flow the
    caller never identified. The branch and run_id fallbacks stay last and
    apply only when no entity matched at all — run ids are not part of the
    generic resolver's search space (see `_resolve_session_by_run_id`).

    Searching the run-id space last orders it, which is not the same as keeping
    it apart: a string that identifies both an entity and a run is ambiguous
    however late the second space is consulted, so that case is refused rather
    than resolved, after an entity hit and after a branch hit alike.

    *run_prefix_through_t* is passed by the commands that act on the answer:
    their run-id prefixes must reach the `T` after the date (ADR-0108,
    Amendment 2), so a number, a date or a label never acts on a run.
    """
    hit = await resolve_entity(db, entity_id, tables=("sessions", "invocations", "plays"))
    if hit is not None:
        _table, entity_type, row = hit
        # Keeping run ids out of the generic search space orders the two spaces,
        # it does not separate them: a run id opens with a date, whose digits are
        # all valid hex, so a short prefix can fit a run id and a UUID at once.
        # Taking the entity because it is searched first would let search order
        # decide a question it cannot answer, and the commands built on this
        # resolver act on the answer. Only a real double match refuses. A full id
        # cannot reach here: a run id is shorter than a UUID and carries a `T`,
        # which no UUID contains.
        also_runs = await run_id_candidates(db, entity_id)
        if also_runs:
            raise AmbiguousIdError(
                entity_id,
                None,
                [f"{entity_type} {row.get('id')}", *(f"run {r}" for r in also_runs)],
            )
        return entity_type, row
    row = await _resolve_session_by_branch_id(db, entity_id)
    if row is not None:
        also_runs = await run_id_candidates(db, entity_id)
        if also_runs:
            raise AmbiguousIdError(
                entity_id,
                None,
                [f"branch of session {row.get('id')}", *(f"run {r}" for r in also_runs)],
            )
        return "session", row
    row = await _resolve_session_by_run_id(db, entity_id, prefix_through_t=run_prefix_through_t)
    if row is not None:
        return "session", row
    return None


async def resolve_primary_session(
    db: Any, entity_type: str, row: dict[str, Any]
) -> dict[str, Any] | None:
    """Best-effort backing session for model/provider/phase/progress enrichment."""
    if entity_type == "session":
        return row
    if entity_type == "play":
        sid = row.get("session_id")
        return await db.get_session(sid) if sid else None
    if entity_type == "invocation":
        sessions = await db.list_sessions_for_invocation(row["id"])
        if not sessions:
            return None
        return max(sessions, key=lambda s: s.get("updated_at") or 0)
    return None
