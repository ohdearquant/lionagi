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

from lionagi.state.db import DEFAULT_DB_PATH, StateDB

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
) -> int:
    """Execute *sql_prefix* + ' IN (?,?,...)' for *ids* in chunks of _CHUNK.

    *sql_prefix* must end just before the IN clause. Returns total rowcount.
    """
    total = 0
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i : i + _CHUNK]
        ph = ", ".join("?" * len(chunk))
        result = await conn.execute(
            *_q(f"{sql_prefix} IN ({ph})", (*extra_params, *chunk))  # noqa: S608
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


async def checkpoint_state_db(
    mode: str = "TRUNCATE",
    *,
    actor: str = "studio_db_maintenance",
) -> dict[str, int | None]:
    """Run ``PRAGMA wal_checkpoint(<mode>)`` and write an audit event.

    Returns the PRAGMA result dict: busy, log_pages, checkpointed.
    """
    if not DEFAULT_DB_PATH.exists():
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
    if not DEFAULT_DB_PATH.exists():
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

    if not DEFAULT_DB_PATH.exists():
        return {"sessions_pruned": 0, "runs_pruned": 0, "dispatch_purged": 0}

    cutoff = time.time() - keep_days * 86400.0
    sess_ph = ", ".join("?" * len(_TERMINAL_SESSION_STATUSES))
    run_ph = ", ".join("?" * len(_TERMINAL_RUN_STATUSES))

    sessions_pruned = 0
    runs_pruned = 0

    async with StateDB() as db:
        async with db.transaction() as conn:
            # ── find session IDs to prune ─────────────────────────────────
            sql = f"SELECT id FROM sessions WHERE status IN ({sess_ph}) AND started_at <= ?"  # noqa: S608
            rows = (await conn.execute(*_q(sql, (*_TERMINAL_SESSION_STATUSES, cutoff)))).fetchall()
            session_ids = [r[0] for r in rows]

            if session_ids:
                session_ids = sorted(set(session_ids))

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

                # Nullify soft FKs (no CASCADE) before deleting sessions.
                await _exec_chunked(
                    conn, "UPDATE artifacts SET session_id = NULL WHERE session_id", session_ids
                )
                await _exec_chunked(
                    conn, "UPDATE plays SET session_id = NULL WHERE session_id", session_ids
                )
                await _exec_chunked(
                    conn,
                    "UPDATE team_messages SET session_id = NULL WHERE session_id",
                    session_ids,
                )
                # dispatch_outbox.session_id is a plain FK (no CASCADE) — nullify
                # before the parent DELETE or the prune aborts on the FK constraint.
                await _exec_chunked(
                    conn,
                    "UPDATE dispatch_outbox SET session_id = NULL WHERE session_id",
                    session_ids,
                )
                await _exec_chunked(
                    conn,
                    "DELETE FROM status_transitions WHERE entity_type = 'session' AND entity_id",
                    session_ids,
                )
                # branches cascade automatically via FK ON DELETE CASCADE
                sessions_pruned = await _exec_chunked(
                    conn, "DELETE FROM sessions WHERE id", session_ids
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
    if not DEFAULT_DB_PATH.exists():
        return {"status": "skipped"}

    async with StateDB() as db:
        await db.vacuum()
        await db.insert_admin_event(action="vacuum", details={}, actor=actor)

    _log.info("VACUUM complete")
    return {"status": "ok"}
