# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Teardown helper for PostgreSQL tests that delete an imported session.

``StateDB.delete_imported_session`` takes its table locks ``NOWAIT`` on PostgreSQL. That is
deliberate: a delete that meets a conflicting lock fails at once with SQLSTATE 55P03 and its
callers retry. A test teardown is such a caller. Anything that holds a conflicting lock for an
instant is enough to refuse the delete: an autovacuum or autoanalyze worker (its SHARE UPDATE
EXCLUSIVE lock conflicts with the EXCLUSIVE lock the delete asks for) or a backend that is still
finishing a rollback. ``delete_imported_session_with_retry`` waits such a holder out for a
bounded time.

A lock that is still held when that time is spent is not hidden. The helper raises
``AssertionError`` naming the session and listing the backends that hold locks on the tables, so
a real leak shows up as a failure instead of being retried forever.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from sqlalchemy.exc import DBAPIError

if TYPE_CHECKING:
    from lionagi.state.db import StateDB

_LOCK_NOT_AVAILABLE = "55P03"
_BUDGET_SECONDS = 5.0
_INITIAL_DELAY = 0.05
_MAX_DELAY = 0.5
_QUERY_CHARS = 200

_HOLDERS_SQL = """
SELECT l.pid AS pid,
       a.backend_type AS backend_type,
       a.state AS state,
       l.mode AS mode,
       l.granted AS granted,
       c.relname AS relation,
       a.query AS query
FROM pg_locks AS l
JOIN pg_class AS c ON c.oid = l.relation
LEFT JOIN pg_stat_activity AS a ON a.pid = l.pid
WHERE l.locktype = 'relation'
  AND c.relname IN ('branches', 'progressions', 'sessions')
  AND l.pid <> pg_backend_pid()
ORDER BY l.pid, c.relname, l.mode
"""


async def delete_imported_session_with_retry(
    db: StateDB, session_id: str, *, require_source_kind: str
) -> bool:
    """Delete an imported session for a test teardown, waiting out a momentary lock holder.

    Calls ``db.delete_imported_session(session_id, require_source_kind=...)`` and returns its
    result. The delete takes ``NOWAIT`` table locks on PostgreSQL by design, so a backend that
    holds a conflicting lock for an instant (autovacuum, a rollback still finishing) makes it
    fail with SQLSTATE 55P03. That failure alone is retried, after a delay that starts at
    0.05 s, doubles and is capped at 0.5 s, until about 5 s have passed. Every other
    exception propagates unchanged the first time it occurs.

    Raises:
        AssertionError: the lock was still refused when the time ran out. The message names
            the session and lists the other backends holding locks on ``branches``,
            ``progressions`` or ``sessions`` (pid, backend type, state, lock mode, whether it
            was granted, relation and the start of the running query), so a real leak is
            reported rather than hidden. If that listing cannot be read, the message says so.
    """
    deadline = time.monotonic() + _BUDGET_SECONDS
    delay = _INITIAL_DELAY
    while True:
        try:
            return await db.delete_imported_session(
                session_id, require_source_kind=require_source_kind
            )
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != _LOCK_NOT_AVAILABLE:
                raise
            refused = exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 2, _MAX_DELAY)
    raise AssertionError(await _exhausted_message(db, session_id, refused)) from refused


async def _exhausted_message(db: StateDB, session_id: str, refused: DBAPIError) -> str:
    """Describe a delete whose lock was still refused when the time ran out."""
    head = (
        f"could not delete imported session {session_id}: its table locks were still refused "
        f"after {_BUDGET_SECONDS:g}s ({refused.orig})."
    )
    try:
        rows = await db.fetch_all(_HOLDERS_SQL)
    except Exception as exc:
        return f"{head} The lock holders could not be listed ({type(exc).__name__}: {exc})."
    if not rows:
        return (
            f"{head} No other backend held a lock on branches, progressions or sessions "
            "when they were listed."
        )
    holders = "\n".join(
        f"  pid={row['pid']} backend_type={row['backend_type']} state={row['state']} "
        f"mode={row['mode']} granted={row['granted']} relation={row['relation']} "
        f"query={str(row['query'] or '')[:_QUERY_CHARS]!r}"
        for row in rows
    )
    return f"{head} Backends holding locks on branches, progressions or sessions:\n{holders}"
