# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""DB maintenance helpers for Studio.

Three capabilities:
- ``checkpoint_state_db()``: runs ``PRAGMA wal_checkpoint(TRUNCATE)`` and
  records an ``admin_events`` row so ``/api/stats`` can surface the timestamp.
- ``get_last_checkpoint_at()``: fetches the most recent checkpoint event.
- ``get_db_size_alert()``: compares current DB size to the configured threshold.
- ``prune_old_data()``: transactionally removes terminal sessions/runs older
  than ``keep_days``, respecting FK constraints (nullifies soft FKs before
  deleting parents), and writes an ``admin_events`` audit row.
"""

from __future__ import annotations

import logging
import time

from lionagi.state.db import DEFAULT_DB_PATH, StateDB

_log = logging.getLogger(__name__)

# Statuses that are safe to prune (process is definitively done).
_TERMINAL_SESSION_STATUSES = ("completed", "failed", "timed_out", "aborted", "cancelled")
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
        cur = await db.db.execute(f"PRAGMA wal_checkpoint({mode})")  # noqa: S608
        row = await cur.fetchone()
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
    actor: str = "studio_db_maintenance",
) -> dict[str, int]:
    """Remove terminal sessions/runs older than ``keep_days`` in one transaction.

    FK safety:
    - ``branches`` → CASCADE on sessions (auto-deleted)
    - ``artifacts``, ``plays``, ``team_messages`` → soft FK to sessions
      (no CASCADE); session_id is nullified before the DELETE so FK
      constraints don't fire.
    - ``status_transitions`` has no FK to sessions; rows are deleted for
      hygiene.
    - ``schedule_runs.chain_parent_id`` self-references schedule_runs;
      children are nullified before the parent delete.
    """
    from lionagi.studio.config import PRUNE_KEEP_DAYS

    if keep_days is None:
        keep_days = PRUNE_KEEP_DAYS

    if not DEFAULT_DB_PATH.exists():
        return {"sessions_pruned": 0, "runs_pruned": 0}

    cutoff = time.time() - keep_days * 86400.0
    sess_ph = ", ".join("?" * len(_TERMINAL_SESSION_STATUSES))
    run_ph = ", ".join("?" * len(_TERMINAL_RUN_STATUSES))

    sessions_pruned = 0
    runs_pruned = 0

    async with StateDB() as db:
        # ── find session IDs to prune ─────────────────────────────────────
        cur = await db.db.execute(
            f"SELECT id FROM sessions WHERE status IN ({sess_ph}) AND started_at <= ?",  # noqa: S608
            (*_TERMINAL_SESSION_STATUSES, cutoff),
        )
        rows = await cur.fetchall()
        session_ids = [r[0] for r in rows]

        if session_ids:
            id_ph = ", ".join("?" * len(session_ids))

            # ── Capture child ids BEFORE deleting anything ────────────────
            # progressions referenced by the pruned sessions
            cur = await db.db.execute(
                f"SELECT progression_id FROM sessions WHERE id IN ({id_ph}) AND progression_id IS NOT NULL",  # noqa: S608
                session_ids,
            )
            session_prog_ids = [r[0] for r in await cur.fetchall()]

            # progressions referenced by the branches that will cascade-delete
            cur = await db.db.execute(
                f"SELECT progression_id FROM branches WHERE session_id IN ({id_ph}) AND progression_id IS NOT NULL",  # noqa: S608
                session_ids,
            )
            branch_prog_ids = [r[0] for r in await cur.fetchall()]

            candidate_prog_ids = list({*session_prog_ids, *branch_prog_ids})

            # messages referenced by those candidate progressions' collection arrays
            candidate_msg_ids: list[str] = []
            if candidate_prog_ids:
                prog_ph = ", ".join("?" * len(candidate_prog_ids))
                cur = await db.db.execute(
                    f"SELECT value FROM progressions, json_each(progressions.collection)"  # noqa: S608
                    f" WHERE progressions.id IN ({prog_ph}) AND value IS NOT NULL",
                    candidate_prog_ids,
                )
                candidate_msg_ids = [r[0] for r in await cur.fetchall()]

            # Nullify soft FKs (no CASCADE) before deleting sessions.
            await db.db.execute(
                f"UPDATE artifacts SET session_id = NULL WHERE session_id IN ({id_ph})",  # noqa: S608
                session_ids,
            )
            await db.db.execute(
                f"UPDATE plays SET session_id = NULL WHERE session_id IN ({id_ph})",  # noqa: S608
                session_ids,
            )
            await db.db.execute(
                f"UPDATE team_messages SET session_id = NULL WHERE session_id IN ({id_ph})",  # noqa: S608
                session_ids,
            )
            # Delete audit trail for these sessions (no FK; good hygiene).
            await db.db.execute(
                f"DELETE FROM status_transitions WHERE entity_type = 'session' AND entity_id IN ({id_ph})",  # noqa: S608
                session_ids,
            )
            # Delete sessions; branches cascade automatically via FK ON DELETE CASCADE.
            cur = await db.db.execute(
                f"DELETE FROM sessions WHERE id IN ({id_ph})",  # noqa: S608
                session_ids,
            )
            sessions_pruned = cur.rowcount

            # ── Targeted orphan cleanup (scoped to pruned lineage only) ───
            # Only delete progressions/messages that were part of the pruned
            # sessions' lineage and are now unreferenced.  Never touch rows
            # outside that lineage — this prevents a newborn-orphan race where
            # _persist.py commits a progression before the session row exists.
            if candidate_prog_ids:
                prog_ph = ", ".join("?" * len(candidate_prog_ids))
                # Delete progressions in the candidate set that are no longer
                # referenced by any surviving session or branch.  The NOT IN
                # subquery is scoped to the candidate set, so newborn progressions
                # (not in candidate_prog_ids) are never touched.
                await db.db.execute(
                    f"DELETE FROM progressions WHERE id IN ({prog_ph})"  # noqa: S608
                    " AND id NOT IN ("
                    "  SELECT progression_id FROM sessions WHERE progression_id IS NOT NULL"
                    "  UNION"
                    "  SELECT progression_id FROM branches WHERE progression_id IS NOT NULL"
                    ")",
                    candidate_prog_ids,
                )

            if candidate_msg_ids:
                msg_ph = ", ".join("?" * len(candidate_msg_ids))
                # Delete messages in the candidate set that no longer appear in
                # any progression's collection array.  Scoped to candidate_msg_ids
                # so newborn messages (not yet referenced by their progression) are
                # never touched.
                await db.db.execute(
                    f"DELETE FROM messages WHERE id IN ({msg_ph})"  # noqa: S608
                    " AND id NOT IN ("
                    "  SELECT value FROM progressions, json_each(progressions.collection)"
                    "  WHERE value IS NOT NULL"
                    ")",
                    candidate_msg_ids,
                )

        # ── prune old terminal schedule_runs ─────────────────────────────
        # Nullify chain_parent_id for child runs whose parent will be deleted.
        await db.db.execute(
            f"UPDATE schedule_runs SET chain_parent_id = NULL WHERE chain_parent_id IN "  # noqa: S608
            f"(SELECT id FROM schedule_runs WHERE status IN ({run_ph}) AND fired_at <= ?)",
            (*_TERMINAL_RUN_STATUSES, cutoff),
        )
        cur = await db.db.execute(
            f"DELETE FROM schedule_runs WHERE status IN ({run_ph}) AND fired_at <= ?",  # noqa: S608
            (*_TERMINAL_RUN_STATUSES, cutoff),
        )
        runs_pruned = cur.rowcount

        # insert_admin_event commits the entire transaction above.
        await db.insert_admin_event(
            action="prune",
            details={
                "keep_days": keep_days,
                "cutoff": cutoff,
                "sessions_pruned": sessions_pruned,
                "runs_pruned": runs_pruned,
            },
            actor=actor,
        )

    _log.info(
        "Prune old data (keep_days=%d, cutoff=%.0f): sessions=%d runs=%d",
        keep_days,
        cutoff,
        sessions_pruned,
        runs_pruned,
    )
    return {"sessions_pruned": sessions_pruned, "runs_pruned": runs_pruned}
