# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""DB maintenance helpers — checkpoint, prune, vacuum, size alert for Studio."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from lionagi._errors import LionError
from lionagi.state.db import StateDB, state_db_file, state_db_known_absent
from lionagi.studio.services.retention_archive import archive_chunk_id, write_archive_chunk

_log = logging.getLogger(__name__)

_SQL_IN_CHUNK = 500  # placeholder bound for a single IN-list statement; not a transaction boundary


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
    for i in range(0, len(ids), _SQL_IN_CHUNK):
        chunk = ids[i : i + _SQL_IN_CHUNK]
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
    *,
    fetch_mappings: bool = False,
) -> list[Any]:
    """SELECT *sql_prefix* + ' IN (?,?,...)' for *ids* in chunks; returns flat list.

    With *fetch_mappings*, rows come back as plain ``dict`` (via
    ``.mappings()``) instead of tuples — used when the caller needs full,
    column-named rows (e.g. to archive them) rather than a single column.
    """
    results: list[Any] = []
    for i in range(0, len(ids), _SQL_IN_CHUNK):
        chunk = ids[i : i + _SQL_IN_CHUNK]
        ph = ", ".join("?" * len(chunk))
        result = await conn.execute(
            *_q(f"{sql_prefix} IN ({ph})", (*extra_params, *chunk))  # noqa: S608
        )
        if fetch_mappings:
            results.extend(dict(r) for r in result.mappings().all())
        else:
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


def _run_retention_predicate(cutoff: float) -> tuple[str, tuple[Any, ...]]:
    """What makes a schedule_run prunable, mirroring the session predicate shape."""
    placeholders = ", ".join("?" * len(_TERMINAL_RUN_STATUSES))
    return (
        f"status IN ({placeholders}) AND fired_at <= ?",
        (*_TERMINAL_RUN_STATUSES, cutoff),
    )


_DISPATCH_SUCCESS_STATUSES = ("delivered", "acked")
_DISPATCH_DEAD_LETTER_STATUSES = ("dead_letter", "expired")


def _dispatch_retention_predicate(
    success_cutoff: float, dead_letter_cutoff: float
) -> tuple[str, tuple[Any, ...]]:
    """What makes a dispatch_outbox row prunable: two disjoint status/window classes.

    pending/delivering never qualify because neither status appears in either
    branch of this OR.
    """
    success_ph = ", ".join("?" * len(_DISPATCH_SUCCESS_STATUSES))
    dl_ph = ", ".join("?" * len(_DISPATCH_DEAD_LETTER_STATUSES))
    return (
        f"((status IN ({success_ph}) AND updated_at <= ?)"
        f" OR (status IN ({dl_ph}) AND updated_at <= ?))",
        (
            *_DISPATCH_SUCCESS_STATUSES,
            success_cutoff,
            *_DISPATCH_DEAD_LETTER_STATUSES,
            dead_letter_cutoff,
        ),
    )


def _session_retention_predicate(cutoff: float) -> tuple[str, tuple[Any, ...]]:
    """What makes a session prunable (terminal status AND no activity since
    *cutoff*), as a reusable SQL fragment + params -- the prune selects
    candidates with it, then rechecks under lock with an id restriction
    using the exact same fragment, so a drifted second spelling can't widen
    the set past what selection already decided to spare."""
    placeholders = ", ".join("?" * len(_TERMINAL_SESSION_STATUSES))
    return (
        f"status IN ({placeholders}) AND updated_at <= ?",
        (*_TERMINAL_SESSION_STATUSES, cutoff),
    )


def _wal_bytes_now() -> int | None:
    """Size of the store's WAL sidecar at this moment, or None if unmeasurable.

    ``None`` means the question does not apply or could not be answered: a
    server-backed or in-memory store has no sidecar, and a file we are not
    allowed to stat gives no size. A missing sidecar beside a real store file
    is 0 rather than None, because that is a genuine answer: SQLite removes
    the WAL on a clean close and on a successful TRUNCATE checkpoint, and both
    mean there is nothing there to drain.
    """
    path = state_db_file()
    if path is None:
        return None
    try:
        return path.with_name(path.name + "-wal").stat().st_size
    except FileNotFoundError:
        return 0
    except OSError:
        return None


async def checkpoint_state_db(
    mode: str = "TRUNCATE",
    *,
    actor: str = "studio_db_maintenance",
) -> dict[str, Any]:
    """Run ``PRAGMA wal_checkpoint(<mode>)`` and write an audit event.
    Returns the PRAGMA result (busy, log_pages, checkpointed) plus
    ``wal_bytes_before`` (read before the connection opens) and
    ``elapsed_ms`` (covers opening the connection too). For TRUNCATE, all
    three PRAGMA counters read zero on success regardless of how much was
    drained -- that is the success signature, not evidence of nothing to do.
    Written only after the checkpoint returns, so a hung checkpoint leaves no
    row at all rather than a slow one. See studio.md."""
    if state_db_known_absent():
        return {
            "mode": mode,
            "busy": None,
            "log_pages": None,
            "checkpointed": None,
            "wal_bytes_before": None,
            "elapsed_ms": None,
        }

    wal_before = _wal_bytes_now()
    started = time.perf_counter()
    async with StateDB() as db:
        row = await db.checkpoint(mode)
        details: dict[str, Any] = {
            "mode": mode,
            "busy": int(row[0]) if row else None,
            "log_pages": int(row[1]) if row else None,
            "checkpointed": int(row[2]) if row else None,
            "wal_bytes_before": wal_before,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
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


async def get_last_prune_at() -> float | None:
    """Return the ``created_at`` of the most recent prune event, or None if a
    prune has never been recorded.

    Unlike ``get_last_checkpoint_at`` this does not swallow read failures. Its
    caller uses the answer to decide when the next automatic prune is due, and
    a failed read reported as None is indistinguishable from "never pruned",
    which would anchor the schedule to the failure instead of retrying.
    """
    if state_db_known_absent():
        return None
    async with StateDB() as db:
        events = await db.list_admin_events(action="prune", limit=1)
    return events[0].get("created_at") if events else None


def get_db_size_alert(size_bytes: int) -> tuple[bool, int]:
    """Return ``(size_alert, threshold_bytes)`` given the current DB size."""
    from lionagi.studio.config import DB_SIZE_ALERT_BYTES

    threshold = DB_SIZE_ALERT_BYTES
    return size_bytes >= threshold, threshold


async def _candidate_chunks(
    db: StateDB,
    *,
    table: str,
    where_sql: str,
    params: Sequence[Any],
    size: int,
) -> AsyncIterator[list[str]]:
    """Yield ids eligible under *where_sql* in bounded, ascending-id chunks.

    The scan reads one chunk at a time instead of every eligible id at once,
    so a pass holds at most *size* ids however far the aged backlog has grown.
    Each chunk is read in its own short transaction, which keeps the selection
    off the write lock the chunk deletes then take.

    The cursor advances by id rather than by "what is still eligible", because
    a chunk can legitimately delete nothing: `_prune_session_chunk` re-checks
    each row under the write lock and skips one that stopped being terminal.
    Re-asking the same question would hand that row back forever.
    """
    after = ""
    while True:
        sql = f"SELECT id FROM {table} WHERE ({where_sql}) AND id > ? ORDER BY id LIMIT ?"  # noqa: S608
        async with db.transaction() as conn:
            rows = (await conn.execute(*_q(sql, (*params, after, size)))).fetchall()
        ids = sorted({r[0] for r in rows})
        if not ids:
            return
        yield ids
        after = ids[-1]


async def _after_prune_chunk_committed(*, chunk_index: int, chunk_ids: list[str]) -> None:
    """No-op extension point run after each prune chunk's transaction commits.

    Exists so tests can simulate a crash between chunks (the write lock has
    already been released and the chunk's deletes are already durable at
    this point) without special-casing the production code path.
    """
    return None


async def _prune_session_chunk(
    conn: AsyncConnection,
    session_ids: list[str],
    *,
    sess_ph: str,
    retention_sql: str,
    retention_params: tuple[Any, ...],
    archive_destination: Path | None,
    archive_id: str,
) -> int:
    """Archive-then-delete one chunk of candidate session ids in the
    caller's transaction, running the same lock/recheck/soft-FK-nullify/
    delete/orphan-cleanup sequence ``prune_old_data`` always had, scoped to
    this chunk. When *archive_destination* is set, doomed rows are durably
    archived before any DELETE runs; a failed archive write raises and the
    caller's transaction rolls back, so this chunk's rows are refused
    rather than lost. Returns sessions actually deleted (0 if the chunk
    raced empty on recheck)."""
    if not session_ids:
        return 0

    session_ids = sorted(set(session_ids))

    # Lock every candidate row for the rest of the transaction before its
    # status is read — see the module-level design note on this race; the
    # same hazard applies per chunk as it did to the whole set in the
    # pre-chunking version of this function.
    await _exec_chunked(
        conn,
        "UPDATE sessions SET updated_at = updated_at WHERE id",
        session_ids,
    )

    # Recheck under lock, narrowed to this chunk's ids.
    rows = await _fetch_chunked(
        conn,
        f"SELECT id FROM sessions WHERE {retention_sql} AND id",  # noqa: S608
        session_ids,
        retention_params,
    )
    session_ids = sorted({r[0] for r in rows})
    if not session_ids:
        return 0

    # Capture child ids BEFORE archiving or deleting anything.
    rows = await _fetch_chunked(conn, "SELECT progression_id FROM sessions WHERE id", session_ids)
    session_prog_ids = [r[0] for r in rows if r[0] is not None]

    rows = await _fetch_chunked(
        conn, "SELECT progression_id FROM branches WHERE session_id", session_ids
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

    if archive_destination is not None:
        # Archive-before-delete: durably write this chunk's doomed rows first.
        # A failed write raises ArchiveWriteError, the caller's `async with
        # db.transaction()` rolls back, and NOTHING in this chunk is deleted
        # -- refusal without deletion on archive failure.
        session_rows = await _fetch_chunked(
            conn, "SELECT * FROM sessions WHERE id", session_ids, fetch_mappings=True
        )
        branch_rows = await _fetch_chunked(
            conn,
            "SELECT * FROM branches WHERE session_id",
            session_ids,
            fetch_mappings=True,
        )
        progression_rows = (
            await _fetch_chunked(
                conn,
                "SELECT * FROM progressions WHERE id",
                candidate_prog_ids,
                fetch_mappings=True,
            )
            if candidate_prog_ids
            else []
        )
        message_rows = (
            await _fetch_chunked(
                conn,
                "SELECT * FROM messages WHERE id",
                candidate_msg_ids,
                fetch_mappings=True,
            )
            if candidate_msg_ids
            else []
        )
        # Preimages of soft-FK rows this chunk is about to NULLIFY (not
        # delete) below -- captured before the nullify so a restore can
        # recover the original session linkage instead of leaving these
        # rows permanently orphaned.
        preimage_rows: dict[str, list[dict[str, Any]]] = {}
        for table in ("artifacts", "plays", "team_messages", "dispatch_outbox"):
            preimage_rows[table] = await _fetch_chunked(
                conn,
                f"SELECT * FROM {table} WHERE session_id",  # noqa: S608
                session_ids,
                fetch_mappings=True,
            )

        write_archive_chunk(
            archive_destination,
            archive_id,
            {
                "sessions": session_rows,
                "branches": branch_rows,
                "progressions": progression_rows,
                "messages": message_rows,
            },
            preimages=preimage_rows,
        )

    # Every destructive statement carries the terminal condition itself —
    # see the module-level design note; the same reasoning applies per chunk.
    still_terminal = f" AND session_id IN (SELECT id FROM sessions WHERE status IN ({sess_ph}))"  # noqa: S608

    for table in ("artifacts", "plays", "team_messages", "dispatch_outbox"):
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
    sessions_pruned = await _exec_chunked(
        conn,
        f"DELETE FROM sessions WHERE status IN ({sess_ph}) AND id",  # noqa: S608
        session_ids,
        _TERMINAL_SESSION_STATUSES,
    )

    survivors = await _fetch_chunked(conn, "SELECT id FROM sessions WHERE id", session_ids)
    if survivors:
        raise PruneRaceError(
            "session(s) "
            + ", ".join(sorted(str(r[0]) for r in survivors))
            + " stopped being terminal while their history was being removed; "
            "nothing was pruned"
        )

    # Targeted orphan cleanup scoped to this chunk's lineage only.
    if candidate_prog_ids:
        for i in range(0, len(candidate_prog_ids), _SQL_IN_CHUNK):
            chunk = candidate_prog_ids[i : i + _SQL_IN_CHUNK]
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
        for i in range(0, len(candidate_msg_ids), _SQL_IN_CHUNK):
            chunk = candidate_msg_ids[i : i + _SQL_IN_CHUNK]
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

    return sessions_pruned


async def _prune_run_chunk(
    conn: AsyncConnection,
    run_ids: list[str],
    *,
    run_ph: str,
    retention_sql: str,
    retention_params: tuple[Any, ...],
    archive_destination: Path | None,
    archive_id: str,
) -> int:
    """Archive-then-delete one chunk of candidate schedule_run ids in the caller's transaction.

    Same shape as :func:`_prune_session_chunk`: lock, recheck under lock,
    archive the rechecked rows (if configured), nullify children that
    reference this chunk's ids, then delete only the rechecked ids. A failed
    archive write raises and the caller's transaction rolls back, so nothing
    in this chunk is deleted.
    """
    if not run_ids:
        return 0

    run_ids = sorted(set(run_ids))

    await _exec_chunked(conn, "UPDATE schedule_runs SET fired_at = fired_at WHERE id", run_ids)

    rows = await _fetch_chunked(
        conn,
        f"SELECT id FROM schedule_runs WHERE {retention_sql} AND id",  # noqa: S608
        run_ids,
        retention_params,
    )
    run_ids = sorted({r[0] for r in rows})
    if not run_ids:
        return 0

    if archive_destination is not None:
        run_rows = await _fetch_chunked(
            conn, "SELECT * FROM schedule_runs WHERE id", run_ids, fetch_mappings=True
        )
        # Preimages of soft-FK rows this chunk is about to NULLIFY below.
        chain_child_rows = await _fetch_chunked(
            conn, "SELECT * FROM schedule_runs WHERE chain_parent_id", run_ids, fetch_mappings=True
        )
        dispatch_child_rows = await _fetch_chunked(
            conn,
            "SELECT * FROM dispatch_outbox WHERE schedule_run_id",
            run_ids,
            fetch_mappings=True,
        )
        write_archive_chunk(
            archive_destination,
            archive_id,
            {"schedule_runs": run_rows},
            preimages={
                "schedule_runs": chain_child_rows,
                "dispatch_outbox": dispatch_child_rows,
            },
        )

    await _exec_chunked(
        conn, "UPDATE schedule_runs SET chain_parent_id = NULL WHERE chain_parent_id", run_ids
    )
    await _exec_chunked(
        conn, "UPDATE dispatch_outbox SET schedule_run_id = NULL WHERE schedule_run_id", run_ids
    )

    return await _exec_chunked(
        conn,
        f"DELETE FROM schedule_runs WHERE status IN ({run_ph}) AND id",  # noqa: S608
        run_ids,
        _TERMINAL_RUN_STATUSES,
    )


async def _prune_dispatch_chunk(
    conn: AsyncConnection,
    dispatch_ids: list[str],
    *,
    retention_sql: str,
    retention_params: tuple[Any, ...],
    archive_destination: Path | None,
    archive_id: str,
) -> int:
    """Archive-then-delete one chunk of candidate dispatch_outbox ids in the caller's transaction.

    Same archive-before-delete contract as the session and run chunk
    helpers. pending/delivering rows never enter *dispatch_ids* because they
    never satisfy *retention_sql*.
    """
    if not dispatch_ids:
        return 0

    dispatch_ids = sorted(set(dispatch_ids))

    await _exec_chunked(
        conn, "UPDATE dispatch_outbox SET updated_at = updated_at WHERE id", dispatch_ids
    )

    rows = await _fetch_chunked(
        conn,
        f"SELECT id FROM dispatch_outbox WHERE {retention_sql} AND id",  # noqa: S608
        dispatch_ids,
        retention_params,
    )
    dispatch_ids = sorted({r[0] for r in rows})
    if not dispatch_ids:
        return 0

    if archive_destination is not None:
        dispatch_rows = await _fetch_chunked(
            conn, "SELECT * FROM dispatch_outbox WHERE id", dispatch_ids, fetch_mappings=True
        )
        write_archive_chunk(archive_destination, archive_id, {"dispatch_outbox": dispatch_rows})

    return await _exec_chunked(
        conn,
        f"DELETE FROM dispatch_outbox WHERE {retention_sql} AND id",  # noqa: S608
        dispatch_ids,
        retention_params,
    )


async def prune_old_data(
    *,
    keep_days: int | None = None,
    dispatch_success_keep_days: int | None = None,
    dispatch_dead_letter_keep_days: int | None = None,
    actor: str = "studio_db_maintenance",
) -> dict[str, int]:
    """Archive-then-prune terminal sessions/runs/dispatches older than their
    keep windows. All three root kinds are pruned in chunks of at most
    ``PRUNE_CHUNK_ROWS`` ids, each archived (if ``PRUNE_ARCHIVE_DIR`` is set)
    and deleted in its own short transaction, so an interrupted run keeps
    every chunk that already committed; a failed archive write aborts the
    remainder of the pass. Soft-FK children are nullified before DELETE
    since they lack CASCADE. See studio.md.

    Candidate ids are read one chunk at a time as well, so the pass holds
    ``PRUNE_CHUNK_ROWS`` ids at a time rather than every id an aged backlog
    made eligible. Total work still scales with that backlog; what is bounded
    is the memory a pass occupies and the length of any one lock it takes."""
    from lionagi.studio.config import (
        DISPATCH_RETENTION_DEAD_LETTER_DAYS,
        DISPATCH_RETENTION_SUCCESS_DAYS,
        PRUNE_ARCHIVE_DIR,
        PRUNE_CHUNK_ROWS,
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
    retention_sql, retention_params = _session_retention_predicate(cutoff)

    sessions_pruned = 0
    runs_pruned = 0
    session_archive_ids: list[str] = []
    run_archive_ids: list[str] = []
    dispatch_archive_ids: list[str] = []

    async with StateDB() as db:
        # ── archive-then-delete in bounded, independently committed chunks ─
        # Candidate ids arrive one chunk at a time; see _candidate_chunks.
        chunk_index = -1
        async for chunk_ids in _candidate_chunks(
            db,
            table="sessions",
            where_sql=retention_sql,
            params=retention_params,
            size=PRUNE_CHUNK_ROWS,
        ):
            chunk_index += 1
            archive_id = archive_chunk_id(cutoff=cutoff, chunk_index=chunk_index)
            async with db.transaction() as conn:
                chunk_pruned = await _prune_session_chunk(
                    conn,
                    chunk_ids,
                    sess_ph=sess_ph,
                    retention_sql=retention_sql,
                    retention_params=retention_params,
                    archive_destination=PRUNE_ARCHIVE_DIR,
                    archive_id=archive_id,
                )
            # Outside the transaction: this chunk has already committed and its
            # write lock has already been released. A crash here (or in the
            # next chunk's setup) keeps every chunk committed so far -- the
            # hook exists so tests can simulate exactly that interruption
            # point without it rolling back the chunk that just landed.
            sessions_pruned += chunk_pruned
            # An archive is only ever written when the chunk actually deleted
            # something (see _prune_session_chunk: an empty post-recheck
            # chunk returns before write_archive_chunk runs), so chunk_pruned
            # tracks archive existence exactly.
            if PRUNE_ARCHIVE_DIR is not None and chunk_pruned:
                session_archive_ids.append(archive_id)
            await _after_prune_chunk_committed(chunk_index=chunk_index, chunk_ids=chunk_ids)

        # ── schedule_run retention: archive-then-delete in bounded, ────────
        # independently committed chunks (ADR-R3 shape, applied to runs).
        run_retention_sql, run_retention_params = _run_retention_predicate(cutoff)
        chunk_index = -1
        async for chunk_ids in _candidate_chunks(
            db,
            table="schedule_runs",
            where_sql=run_retention_sql,
            params=run_retention_params,
            size=PRUNE_CHUNK_ROWS,
        ):
            chunk_index += 1
            archive_id = archive_chunk_id(cutoff=cutoff, chunk_index=chunk_index, kind="run")
            async with db.transaction() as conn:
                chunk_pruned = await _prune_run_chunk(
                    conn,
                    chunk_ids,
                    run_ph=run_ph,
                    retention_sql=run_retention_sql,
                    retention_params=run_retention_params,
                    archive_destination=PRUNE_ARCHIVE_DIR,
                    archive_id=archive_id,
                )
            # Same lock-release semantics as the session chunk loop above:
            # this chunk has already committed by the time we get here.
            runs_pruned += chunk_pruned
            if PRUNE_ARCHIVE_DIR is not None and chunk_pruned:
                run_archive_ids.append(archive_id)
            await _after_prune_chunk_committed(chunk_index=chunk_index, chunk_ids=chunk_ids)

        # ── dispatch_outbox retention (ADR-0059 delta 3): two separate ─────
        # windows for success vs dead-lettered, same chunked archive-then-
        # delete shape; pending/delivering are never in either window.
        dispatch_success_cutoff = time.time() - dispatch_success_keep_days * 86400.0
        dispatch_dead_letter_cutoff = time.time() - dispatch_dead_letter_keep_days * 86400.0
        dispatch_retention_sql, dispatch_retention_params = _dispatch_retention_predicate(
            dispatch_success_cutoff, dispatch_dead_letter_cutoff
        )
        dispatch_purged = 0
        chunk_index = -1
        async for chunk_ids in _candidate_chunks(
            db,
            table="dispatch_outbox",
            where_sql=dispatch_retention_sql,
            params=dispatch_retention_params,
            size=PRUNE_CHUNK_ROWS,
        ):
            chunk_index += 1
            archive_id = archive_chunk_id(
                cutoff=dispatch_success_cutoff, chunk_index=chunk_index, kind="dispatch"
            )
            async with db.transaction() as conn:
                chunk_purged = await _prune_dispatch_chunk(
                    conn,
                    chunk_ids,
                    retention_sql=dispatch_retention_sql,
                    retention_params=dispatch_retention_params,
                    archive_destination=PRUNE_ARCHIVE_DIR,
                    archive_id=archive_id,
                )
            dispatch_purged += chunk_purged
            if PRUNE_ARCHIVE_DIR is not None and chunk_purged:
                dispatch_archive_ids.append(archive_id)
            await _after_prune_chunk_committed(chunk_index=chunk_index, chunk_ids=chunk_ids)

        # Runs after the prune transactions commit — insert_admin_event opens its own
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
                "archived": PRUNE_ARCHIVE_DIR is not None,
                "archive_ids": {
                    "sessions": session_archive_ids,
                    "runs": run_archive_ids,
                    "dispatch": dispatch_archive_ids,
                },
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
