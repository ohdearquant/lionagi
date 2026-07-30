# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""DB maintenance helpers — checkpoint, prune, vacuum, size alert for Studio."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from lionagi._errors import LionError
from lionagi.state.db import StateDB, state_db_known_absent

_log = logging.getLogger(__name__)

_CHUNK = 500  # max placeholders per IN-list statement


def _q(sql: str, params: Sequence[Any]) -> tuple[Any, dict[str, Any]]:
    """Translate qmark SQL + positional params to a bound ``text()`` + named dict."""
    s, p = StateDB._to_named(sql, tuple(params))
    return text(s), p


async def _exec_chunked(
    conn: AsyncConnection,
    sql_prefix: str,
    ids: Sequence[str],
    extra_params: Sequence[Any] = (),
    suffix: str = "",
    suffix_params: Sequence[Any] = (),
) -> int:
    """Execute *sql_prefix* + ' IN (?,?,...)' + *suffix* for *ids* in chunks.

    *sql_prefix* must end just before the IN clause. *suffix* is appended after
    it, for a condition that has to be part of the statement itself rather than
    checked beforehand. Returns total rowcount.
    """
    total = 0
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i : i + _CHUNK]
        ph = ", ".join("?" * len(chunk))
        result = await conn.execute(
            *_q(f"{sql_prefix} IN ({ph}){suffix}", (*extra_params, *chunk, *suffix_params))  # noqa: S608
        )
        total += result.rowcount
    return total


async def _fetch_chunked(
    conn: AsyncConnection,
    sql_prefix: str,
    ids: Sequence[str],
    extra_params: Sequence[Any] = (),
) -> list[Any]:
    """SELECT *sql_prefix* + ' IN (?,?,...)' for *ids* in chunks; returns flat list."""
    results: list[Any] = []
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i : i + _CHUNK]
        ph = ", ".join("?" * len(chunk))
        result = await conn.execute(
            *_q(f"{sql_prefix} IN ({ph})", (*extra_params, *chunk))  # noqa: S608
        )
        results.extend(result.fetchall())
    return results


class PruneRaceError(LionError):
    """A session stopped being terminal partway through pruning its history.

    The prune holds every candidate row locked for the length of its
    transaction, so this cannot happen through the lock; it is raised as a
    post-condition, and raising it abandons the transaction rather than
    committing a session that kept its row and lost its associations.
    """


# Statuses that are safe to prune (process is definitively done).
_TERMINAL_SESSION_STATUSES = (
    "completed",
    "completed_empty",
    "failed",
    "timed_out",
    "aborted",
    "cancelled",
)
_TERMINAL_RUN_STATUSES = ("completed", "failed", "skipped", "cancelled")


def _session_retention_predicate(cutoff: float) -> tuple[str, tuple[Any, ...]]:
    """What makes a session prunable, as a SQL fragment and its parameters.

    Two conditions, both required: the session is in a terminal status, and it
    has had no activity since *cutoff*. Returned as a fragment rather than a
    whole statement because the prune asks the same question in two different
    shapes -- once to select candidates, and once with an id restriction after
    those rows are locked.

    It comes from one place because the second read has to test exactly what the
    first one did. Either condition can stop holding in between: a resume
    returns a session to running, and any write moves ``updated_at`` forward. The
    recheck exists to narrow the candidate set, so a second spelling that drifted
    even slightly could widen it instead, and the row it wrongly admitted would
    be one the selection had already decided to spare.
    """
    placeholders = ", ".join("?" * len(_TERMINAL_SESSION_STATUSES))
    return (
        f"status IN ({placeholders}) AND updated_at <= ?",
        (*_TERMINAL_SESSION_STATUSES, cutoff),
    )


async def checkpoint_state_db(
    mode: str = "TRUNCATE",
    *,
    actor: str = "studio_db_maintenance",
) -> dict[str, int | None]:
    """Run ``PRAGMA wal_checkpoint(<mode>)`` and write an audit event.

    Returns the PRAGMA result dict: busy, log_pages, checkpointed.
    """
    if state_db_known_absent():
        return {"mode": mode, "busy": None, "log_pages": None, "checkpointed": None}

    async with StateDB() as db:
        row = await db.checkpoint(mode)
        details: dict[str, int | None] = {
            "mode": mode,
            "busy": int(row[0]) if row else None,
            "log_pages": int(row[1]) if row else None,
            "checkpointed": int(row[2]) if row else None,
        }
        await db.insert_admin_event(action="checkpoint", details=details, actor=actor)

    _log.info("WAL checkpoint (%s): %s", mode, details)
    return details


async def get_last_checkpoint_at() -> float | None:
    """Return the ``created_at`` timestamp of the most recent checkpoint event."""
    if state_db_known_absent():
        return None
    try:
        async with StateDB() as db:
            events = await db.list_admin_events(action="checkpoint", limit=1)
        if events:
            return events[0].get("created_at")
    except Exception:
        _log.exception("get_last_checkpoint_at error")
    return None


def get_db_size_alert(size_bytes: int) -> tuple[bool, int]:
    """Return ``(size_alert, threshold_bytes)`` given the current DB size."""
    from lionagi.studio.config import DB_SIZE_ALERT_BYTES

    threshold = DB_SIZE_ALERT_BYTES
    return size_bytes >= threshold, threshold


async def prune_old_data(
    *,
    keep_days: int | None = None,
    dispatch_success_keep_days: int | None = None,
    dispatch_dead_letter_keep_days: int | None = None,
    actor: str = "studio_db_maintenance",
) -> dict[str, int]:
    """Remove terminal sessions/runs/dispatches older than their keep windows, in one transaction.

    FK safety: soft-FK children (artifacts/plays/team_messages/dispatch_outbox) are
    nullified before DELETE since they lack CASCADE.
    """
    from lionagi.studio.config import (
        DISPATCH_RETENTION_DEAD_LETTER_DAYS,
        DISPATCH_RETENTION_SUCCESS_DAYS,
        PRUNE_KEEP_DAYS,
    )

    if keep_days is None:
        keep_days = PRUNE_KEEP_DAYS
    if dispatch_success_keep_days is None:
        dispatch_success_keep_days = DISPATCH_RETENTION_SUCCESS_DAYS
    if dispatch_dead_letter_keep_days is None:
        dispatch_dead_letter_keep_days = DISPATCH_RETENTION_DEAD_LETTER_DAYS

    if state_db_known_absent():
        return {"sessions_pruned": 0, "runs_pruned": 0, "dispatch_purged": 0}

    cutoff = time.time() - keep_days * 86400.0
    sess_ph = ", ".join("?" * len(_TERMINAL_SESSION_STATUSES))
    run_ph = ", ".join("?" * len(_TERMINAL_RUN_STATUSES))

    sessions_pruned = 0
    runs_pruned = 0

    async with StateDB() as db:
        async with db.transaction() as conn:
            # ── find session IDs to prune ─────────────────────────────────
            # First of the predicate's two reads: candidate selection, over every
            # session rather than a known set of ids.
            retention_sql, retention_params = _session_retention_predicate(cutoff)
            sql = f"SELECT id FROM sessions WHERE {retention_sql}"  # noqa: S608
            rows = (await conn.execute(*_q(sql, retention_params))).fetchall()
            session_ids = [r[0] for r in rows]

            if session_ids:
                session_ids = sorted(set(session_ids))

                # Lock every candidate row for the rest of the transaction
                # before its status is read. A write to a row is what takes the
                # lock on both backends: postgresql locks the rows themselves,
                # sqlite escalates the whole transaction to a write, which is
                # the same guarantee at a coarser grain. The value is left
                # exactly as it was — this statement exists for the lock.
                #
                # Reading the status without holding the rows would only move
                # the window rather than close it. A session can leave a
                # terminal status at any time (resuming a branch returns it to
                # running), so a resume that commits after an unlocked read is
                # still free to land between two of the destructive statements
                # below, which is how a session ends up keeping its row and
                # losing the associations already cleared for it.
                await _exec_chunked(
                    conn,
                    "UPDATE sessions SET updated_at = updated_at WHERE id",
                    session_ids,
                )

                # Second of the predicate's two reads: the recheck under lock,
                # narrowed to the candidate ids. Now that the rows are held, both
                # conditions are re-read so a session that came back to life or
                # received recent activity before the lock drops out.
                rows = await _fetch_chunked(
                    conn,
                    f"SELECT id FROM sessions WHERE {retention_sql} AND id",  # noqa: S608
                    session_ids,
                    retention_params,
                )
                session_ids = sorted({r[0] for r in rows})

            if session_ids:
                # Capture child ids BEFORE deleting anything.
                rows = await _fetch_chunked(
                    conn,
                    "SELECT progression_id FROM sessions WHERE id",
                    session_ids,
                )
                session_prog_ids = [r[0] for r in rows if r[0] is not None]

                rows = await _fetch_chunked(
                    conn,
                    "SELECT progression_id FROM branches WHERE session_id",
                    session_ids,
                )
                branch_prog_ids = [r[0] for r in rows if r[0] is not None]

                candidate_prog_ids = sorted({*session_prog_ids, *branch_prog_ids})

                coll_msg_ids: list[str] = []
                if candidate_prog_ids:
                    rows = await _fetch_chunked(
                        conn,
                        "SELECT value FROM progressions, json_each(progressions.collection)"
                        " WHERE value IS NOT NULL AND progressions.id",
                        candidate_prog_ids,
                    )
                    coll_msg_ids = [r[0] for r in rows]

                # schema.sql: sessions.first_msg_id / last_msg_id REFERENCES messages(id)
                rows = await _fetch_chunked(
                    conn,
                    "SELECT first_msg_id FROM sessions WHERE first_msg_id IS NOT NULL AND id",
                    session_ids,
                )
                session_first_ids = [r[0] for r in rows]
                rows = await _fetch_chunked(
                    conn,
                    "SELECT last_msg_id FROM sessions WHERE last_msg_id IS NOT NULL AND id",
                    session_ids,
                )
                session_last_ids = [r[0] for r in rows]

                # schema.sql: branches.system_msg_id REFERENCES messages(id)
                rows = await _fetch_chunked(
                    conn,
                    "SELECT system_msg_id FROM branches WHERE system_msg_id IS NOT NULL AND session_id",
                    session_ids,
                )
                branch_sys_ids = [r[0] for r in rows]

                candidate_msg_ids = sorted(
                    {*coll_msg_ids, *session_first_ids, *session_last_ids, *branch_sys_ids}
                )

                # Every destructive statement carries the terminal condition
                # itself. Checking it once above and then running a sequence of
                # writes would protect only whichever statement happens to be
                # last: a session that reopens partway through would keep its
                # row and lose the history and associations already removed,
                # which is a worse outcome than the one being prevented.
                still_terminal = (
                    f" AND session_id IN (SELECT id FROM sessions WHERE status IN ({sess_ph}))"  # noqa: S608
                )

                # Nullify soft FKs (no CASCADE) before deleting sessions.
                for table in ("artifacts", "plays", "team_messages", "dispatch_outbox"):
                    # dispatch_outbox.session_id is a plain FK (no CASCADE) —
                    # nullify before the parent DELETE or the prune aborts on
                    # the FK constraint.
                    await _exec_chunked(
                        conn,
                        f"UPDATE {table} SET session_id = NULL WHERE session_id",  # noqa: S608
                        session_ids,
                        suffix=still_terminal,
                        suffix_params=_TERMINAL_SESSION_STATUSES,
                    )
                await _exec_chunked(
                    conn,
                    "DELETE FROM status_transitions WHERE entity_type = 'session' AND entity_id",
                    session_ids,
                    suffix=(
                        f" AND entity_id IN (SELECT id FROM sessions WHERE status IN ({sess_ph}))"  # noqa: S608
                    ),
                    suffix_params=_TERMINAL_SESSION_STATUSES,
                )
                # branches cascade automatically via FK ON DELETE CASCADE
                # The status predicate rides the delete itself, not only the
                # read above it: on a backend where concurrent transactions can
                # commit between the two, the statement that removes the row is
                # the only place the condition is guaranteed to still hold.
                sessions_pruned = await _exec_chunked(
                    conn,
                    f"DELETE FROM sessions WHERE status IN ({sess_ph}) AND id",  # noqa: S608
                    session_ids,
                    _TERMINAL_SESSION_STATUSES,
                )

                # The delete is where a session that reopened mid-sequence
                # would show up: its row survives while the statements above
                # have already cleared its history and associations. The lock
                # taken before the re-read is what prevents that, and this is
                # the check that it held. Raising abandons the transaction,
                # so the pass either applies whole or leaves the row exactly as
                # it was; the next pass drops the resumed session at selection.
                survivors = await _fetch_chunked(
                    conn, "SELECT id FROM sessions WHERE id", session_ids
                )
                if survivors:
                    raise PruneRaceError(
                        "session(s) "
                        + ", ".join(sorted(str(r[0]) for r in survivors))
                        + " stopped being terminal while their history was being removed; "
                        "nothing was pruned"
                    )

                # Targeted orphan cleanup scoped to pruned lineage only — avoids a
                # newborn-orphan race where _persist.py commits a progression before
                # the session row exists.
                if candidate_prog_ids:
                    for i in range(0, len(candidate_prog_ids), _CHUNK):
                        chunk = candidate_prog_ids[i : i + _CHUNK]
                        ph = ", ".join("?" * len(chunk))
                        sql = (
                            f"DELETE FROM progressions WHERE id IN ({ph})"  # noqa: S608
                            " AND id NOT IN ("
                            "  SELECT progression_id FROM sessions WHERE progression_id IS NOT NULL"
                            "  UNION"
                            "  SELECT progression_id FROM branches WHERE progression_id IS NOT NULL"
                            ")"
                        )
                        await conn.execute(*_q(sql, chunk))

                if candidate_msg_ids:
                    for i in range(0, len(candidate_msg_ids), _CHUNK):
                        chunk = candidate_msg_ids[i : i + _CHUNK]
                        ph = ", ".join("?" * len(chunk))
                        sql = (
                            f"DELETE FROM messages WHERE id IN ({ph})"  # noqa: S608
                            " AND id NOT IN ("
                            "  SELECT value FROM progressions, json_each(progressions.collection)"
                            "  WHERE value IS NOT NULL"
                            "  UNION"
                            "  SELECT first_msg_id FROM sessions WHERE first_msg_id IS NOT NULL"
                            "  UNION"
                            "  SELECT last_msg_id FROM sessions WHERE last_msg_id IS NOT NULL"
                            "  UNION"
                            "  SELECT system_msg_id FROM branches WHERE system_msg_id IS NOT NULL"
                            ")"
                        )
                        await conn.execute(*_q(sql, chunk))

            # Nullify chain_parent_id for child runs whose parent will be deleted.
            upd_sql = (
                "UPDATE schedule_runs SET chain_parent_id = NULL WHERE chain_parent_id IN "  # noqa: S608
                f"(SELECT id FROM schedule_runs WHERE status IN ({run_ph}) AND fired_at <= ?)"
            )
            await conn.execute(*_q(upd_sql, (*_TERMINAL_RUN_STATUSES, cutoff)))
            # Same plain-FK hazard as dispatch_outbox.session_id above.
            disp_upd_sql = (
                "UPDATE dispatch_outbox SET schedule_run_id = NULL WHERE schedule_run_id IN "  # noqa: S608
                f"(SELECT id FROM schedule_runs WHERE status IN ({run_ph}) AND fired_at <= ?)"
            )
            await conn.execute(*_q(disp_upd_sql, (*_TERMINAL_RUN_STATUSES, cutoff)))
            del_sql = f"DELETE FROM schedule_runs WHERE status IN ({run_ph}) AND fired_at <= ?"  # noqa: S608
            runs_pruned = (
                await conn.execute(*_q(del_sql, (*_TERMINAL_RUN_STATUSES, cutoff)))
            ).rowcount

            # dispatch_outbox retention (ADR-0059 delta 3): two separate windows for
            # success vs dead-lettered; pending/delivering are never in either list.
            dispatch_success_cutoff = time.time() - dispatch_success_keep_days * 86400.0
            dispatch_dead_letter_cutoff = time.time() - dispatch_dead_letter_keep_days * 86400.0
            success_purged = (
                await conn.execute(
                    *_q(
                        "DELETE FROM dispatch_outbox WHERE status IN ('delivered', 'acked')"
                        " AND updated_at <= ?",
                        (dispatch_success_cutoff,),
                    )
                )
            ).rowcount
            dead_letter_purged = (
                await conn.execute(
                    *_q(
                        "DELETE FROM dispatch_outbox WHERE status IN ('dead_letter', 'expired')"
                        " AND updated_at <= ?",
                        (dispatch_dead_letter_cutoff,),
                    )
                )
            ).rowcount
            dispatch_purged = success_purged + dead_letter_purged

        # Runs after the prune transaction commits — insert_admin_event opens its own
        # write transaction; nesting would self-deadlock on the sqlite write lock.
        await db.insert_admin_event(
            action="prune",
            details={
                "keep_days": keep_days,
                "cutoff": cutoff,
                "sessions_pruned": sessions_pruned,
                "runs_pruned": runs_pruned,
                "dispatch_success_keep_days": dispatch_success_keep_days,
                "dispatch_dead_letter_keep_days": dispatch_dead_letter_keep_days,
                "dispatch_purged": dispatch_purged,
            },
            actor=actor,
        )

    _log.info(
        "Prune old data (keep_days=%d, cutoff=%.0f): sessions=%d runs=%d dispatch=%d",
        keep_days,
        cutoff,
        sessions_pruned,
        runs_pruned,
        dispatch_purged,
    )
    return {
        "sessions_pruned": sessions_pruned,
        "runs_pruned": runs_pruned,
        "dispatch_purged": dispatch_purged,
    }


async def vacuum_state_db(
    *,
    actor: str = "studio_db_maintenance",
) -> dict[str, str]:
    """Run ``VACUUM`` (exclusive lock) and write an audit event; call after ``prune_old_data()``."""
    if state_db_known_absent():
        return {"status": "skipped"}

    async with StateDB() as db:
        await db.vacuum()
        await db.insert_admin_event(action="vacuum", details={}, actor=actor)

    _log.info("VACUUM complete")
    return {"status": "ok"}
