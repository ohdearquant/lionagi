# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Retrying a contended imported-session delete in a PostgreSQL test teardown.

``StateDB.delete_imported_session`` does not wait for locks on PostgreSQL, so a backend that
holds a conflicting lock for an instant fails it quickly with SQLSTATE 55P03. The helper under
test waits such a holder out for a bounded time and, if the lock is still held after that,
fails loudly with the holders listed.

The tests that need no server drive the helper with a stub store and a fake clock. The
others use the ``pg_url`` fixture and are skipped where PostgreSQL is not reachable.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from lionagi.state.db import StateDB
from tests.state import _pg_cleanup
from tests.state._pg_cleanup import delete_imported_session_with_retry

_WAIT = 10.0
_BUDGET_MARGIN = 3.0
_KIND = "imported_fs"


def _uid() -> str:
    return str(uuid.uuid4())


class _DriverError(Exception):
    """Stands in for the driver exception that SQLAlchemy keeps as ``DBAPIError.orig``."""

    def __init__(self, message: str, sqlstate: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


def _lock_refused() -> DBAPIError:
    return DBAPIError(
        "LOCK TABLE branches, progressions, sessions IN EXCLUSIVE MODE NOWAIT",
        None,
        _DriverError('could not obtain lock on relation "progressions"', "55P03"),
    )


def _holder_row(**overrides) -> dict:
    row = {
        "pid": 4242,
        "backend_type": "client backend",
        "state": "idle in transaction",
        "mode": "RowExclusiveLock",
        "granted": True,
        "relation": "progressions",
        "query": "LOCK TABLE progressions IN ROW EXCLUSIVE MODE",
    }
    row.update(overrides)
    return row


class _StubDB:
    """Replays outcomes for ``delete_imported_session``; the last outcome repeats."""

    def __init__(self, outcomes, *, holders=None, holders_error=None) -> None:
        self.outcomes = list(outcomes)
        self.holders = [] if holders is None else holders
        self.holders_error = holders_error
        self.calls: list[tuple[str, str]] = []
        self.holder_queries: list[tuple[str, object]] = []

    async def delete_imported_session(self, session_id, *, require_source_kind):
        self.calls.append((session_id, require_source_kind))
        outcome = self.outcomes[min(len(self.calls), len(self.outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def fetch_all(self, sql, params=None):
        self.holder_queries.append((sql, params))
        if self.holders_error is not None:
            raise self.holders_error
        return self.holders


class _Clock:
    """A clock that only moves when the helper sleeps, so retry timing is exact and instant."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


@pytest.fixture
def clock(monkeypatch):
    """Replace ``time`` and ``asyncio`` inside the helper module only."""
    fake = _Clock()
    monkeypatch.setattr(_pg_cleanup, "time", SimpleNamespace(monotonic=fake.monotonic))
    monkeypatch.setattr(_pg_cleanup, "asyncio", SimpleNamespace(sleep=fake.sleep))
    return fake


# --------------------------------------------------------------------------------------
# No server needed: the helper against a stub store.
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lock_refusals_are_retried_until_the_delete_goes_through(clock):
    refused = _lock_refused()
    db = _StubDB([refused, refused, True])

    result = await delete_imported_session_with_retry(db, "sid-1", require_source_kind=_KIND)

    assert result is True
    assert db.calls == [("sid-1", _KIND)] * 3
    assert clock.sleeps == pytest.approx([0.05, 0.1])
    assert db.holder_queries == []


@pytest.mark.asyncio
async def test_retry_delay_grows_and_is_capped_within_the_budget(clock):
    db = _StubDB([_lock_refused()])

    with pytest.raises(AssertionError):
        await delete_imported_session_with_retry(db, "sid-1", require_source_kind=_KIND)

    assert clock.sleeps[:5] == pytest.approx([0.05, 0.1, 0.2, 0.4, 0.5])
    assert all(0 < delay <= 0.5 + 1e-9 for delay in clock.sleeps)
    assert sum(clock.sleeps) == pytest.approx(5.0)
    # One attempt before each wait and one at the end of the budget.
    assert len(db.calls) == len(clock.sleeps) + 1
    assert len(db.holder_queries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        DBAPIError("DELETE FROM sessions", None, _DriverError("deadlock detected", "40P01")),
        DBAPIError("DELETE FROM sessions", None, ValueError("no sqlstate on this error")),
        RuntimeError("unrelated failure"),
    ],
    ids=["other-sqlstate", "no-sqlstate", "not-a-driver-error"],
)
async def test_other_failures_propagate_on_the_first_call(clock, error):
    db = _StubDB([error])

    with pytest.raises(type(error)) as caught:
        await delete_imported_session_with_retry(db, "sid-1", require_source_kind=_KIND)

    assert caught.value is error
    assert len(db.calls) == 1
    assert clock.sleeps == []
    assert db.holder_queries == []


@pytest.mark.asyncio
async def test_a_false_result_is_returned_without_a_retry(clock):
    db = _StubDB([False])

    result = await delete_imported_session_with_retry(db, "sid-1", require_source_kind=_KIND)

    assert result is False
    assert len(db.calls) == 1
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_exhausted_budget_names_the_session_and_every_lock_holder(clock):
    refused = _lock_refused()
    other = _holder_row(
        pid=777,
        backend_type="autovacuum worker",
        state=None,
        mode="ShareUpdateExclusiveLock",
        relation="sessions",
        query="autovacuum: ANALYZE public.sessions",
    )
    db = _StubDB([refused], holders=[_holder_row(), other])

    with pytest.raises(AssertionError) as caught:
        await delete_imported_session_with_retry(db, "sid-1", require_source_kind=_KIND)

    message = str(caught.value)
    assert "sid-1" in message
    for expected in (
        "pid=4242",
        "backend_type=client backend",
        "state=idle in transaction",
        "mode=RowExclusiveLock",
        "granted=True",
        "relation=progressions",
        "LOCK TABLE progressions IN ROW EXCLUSIVE MODE",
        "pid=777",
        "backend_type=autovacuum worker",
        "mode=ShareUpdateExclusiveLock",
        "relation=sessions",
        "autovacuum: ANALYZE public.sessions",
    ):
        assert expected in message
    # The refusal that ended the retries stays visible, and stays the cause.
    assert "could not obtain lock on relation" in message
    assert caught.value.__cause__ is refused


@pytest.mark.asyncio
async def test_holder_query_text_is_cut_to_200_characters(clock):
    long_query = "x" * 200 + "TAIL"
    db = _StubDB([_lock_refused()], holders=[_holder_row(query=long_query)])

    with pytest.raises(AssertionError) as caught:
        await delete_imported_session_with_retry(db, "sid-1", require_source_kind=_KIND)

    assert "x" * 200 in str(caught.value)
    assert "TAIL" not in str(caught.value)


@pytest.mark.asyncio
async def test_holder_listing_reads_every_relation_lock_in_the_database_but_its_own(clock):
    db = _StubDB([_lock_refused()])

    with pytest.raises(AssertionError):
        await delete_imported_session_with_retry(db, "sid-1", require_source_kind=_KIND)

    (sql, params) = db.holder_queries[0]
    assert len(db.holder_queries) == 1
    for expected in (
        "pg_locks",
        "pg_stat_activity",
        "pg_backend_pid()",
        "current_database()",
        "::regclass",
        "LIMIT :limit",
    ):
        assert expected in sql
    # The refused lock can be on any table the delete touches, so no table name filters it.
    for table in ("branches", "progressions", "sessions"):
        assert f"'{table}'" not in sql
    assert params == {"limit": 51}


@pytest.mark.asyncio
async def test_holder_listing_past_the_cap_shows_fifty_and_says_it_was_cut(clock):
    holders = [_holder_row(pid=1000 + n) for n in range(51)]
    db = _StubDB([_lock_refused()], holders=holders)

    with pytest.raises(AssertionError) as caught:
        await delete_imported_session_with_retry(db, "sid-1", require_source_kind=_KIND)

    message = str(caught.value)
    assert "pid=1049 " in message
    assert "pid=1050 " not in message
    assert "first 50 shown, more exist" in message


@pytest.mark.asyncio
async def test_holder_listing_at_the_cap_is_not_marked_cut(clock):
    holders = [_holder_row(pid=1000 + n) for n in range(50)]
    db = _StubDB([_lock_refused()], holders=holders)

    with pytest.raises(AssertionError) as caught:
        await delete_imported_session_with_retry(db, "sid-1", require_source_kind=_KIND)

    message = str(caught.value)
    assert "pid=1049 " in message
    assert "more exist" not in message


@pytest.mark.asyncio
async def test_exhausted_budget_without_a_holder_says_none_was_found(clock):
    db = _StubDB([_lock_refused()], holders=[])

    with pytest.raises(AssertionError) as caught:
        await delete_imported_session_with_retry(db, "sid-1", require_source_kind=_KIND)

    message = str(caught.value)
    assert "sid-1" in message
    assert "no other backend" in message.lower()


@pytest.mark.asyncio
async def test_failing_holder_query_is_reported_next_to_the_original_failure(clock):
    refused = _lock_refused()
    db = _StubDB(
        [refused], holders_error=RuntimeError("permission denied for view pg_stat_activity")
    )

    with pytest.raises(AssertionError) as caught:
        await delete_imported_session_with_retry(db, "sid-1", require_source_kind=_KIND)

    message = str(caught.value)
    assert "sid-1" in message
    assert "could not be listed" in message
    assert "permission denied for view pg_stat_activity" in message
    assert "could not obtain lock on relation" in message
    assert caught.value.__cause__ is refused


# --------------------------------------------------------------------------------------
# Against PostgreSQL: a second connection plays the backend that holds a table lock.
# --------------------------------------------------------------------------------------


@asynccontextmanager
async def _imported_session(pg_url):
    """One imported session with fresh ids, removed afterwards through normal state calls."""
    session_id, progression_id = _uid(), _uid()
    async with StateDB(url=pg_url) as db, StateDB(url=pg_url) as holder:
        assert db.dialect == holder.dialect == "postgresql"
        try:
            await db.create_progression(progression_id)
            await db.create_session(
                {
                    "id": session_id,
                    "progression_id": progression_id,
                    "source_kind": _KIND,
                    "status": "completed",
                }
            )
            yield db, holder, session_id
        finally:
            try:
                if await db.get_session(session_id) is not None:
                    assert await delete_imported_session_with_retry(
                        db, session_id, require_source_kind=_KIND
                    )
            finally:
                await db.execute("DELETE FROM progressions WHERE id = :id", {"id": progression_id})


@asynccontextmanager
async def _lock_holder(holder: StateDB, table: str = "progressions"):
    """Hold ``LOCK TABLE <table> IN ROW EXCLUSIVE MODE`` in an open transaction.

    Yields the holder's backend pid and an event that ends the transaction. The lock is
    always released on exit.
    """
    locked = asyncio.get_running_loop().create_future()
    release = asyncio.Event()

    async def hold() -> None:
        try:
            async with holder.transaction() as conn:
                await conn.execute(text(f"LOCK TABLE {table} IN ROW EXCLUSIVE MODE"))
                pid = int((await conn.exec_driver_sql("SELECT pg_backend_pid()")).scalar_one())
                locked.set_result(pid)
                await release.wait()
        except BaseException as exc:
            if not locked.done():
                locked.set_exception(exc)
            raise

    task = asyncio.create_task(hold())
    try:
        yield await asyncio.wait_for(locked, _WAIT), release
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_postgres_momentary_lock_holder_is_waited_out(pg_url):
    async with _imported_session(pg_url) as (db, holder, session_id):
        async with _lock_holder(holder) as (_pid, release):
            asyncio.get_running_loop().call_later(0.3, release.set)
            started = time.monotonic()
            deleted = await delete_imported_session_with_retry(
                db, session_id, require_source_kind=_KIND
            )
            elapsed = time.monotonic() - started
        assert deleted is True
        # The first attempts were refused, so the helper really did wait for the release.
        assert elapsed >= 0.2
        assert await db.get_session(session_id) is None


@pytest.mark.asyncio
async def test_postgres_lock_that_is_never_released_fails_naming_the_holder(pg_url, monkeypatch):
    budget = 1.0
    monkeypatch.setattr(_pg_cleanup, "_BUDGET_SECONDS", budget)
    async with _imported_session(pg_url) as (db, holder, session_id):
        async with _lock_holder(holder) as (holder_pid, _release):
            started = time.monotonic()
            with pytest.raises(AssertionError) as caught:
                await delete_imported_session_with_retry(db, session_id, require_source_kind=_KIND)
            elapsed = time.monotonic() - started
        message = str(caught.value)
        assert session_id in message
        assert f"pid={holder_pid}" in message
        assert "progressions" in message
        assert "RowExclusiveLock" in message
        assert budget <= elapsed < budget + _BUDGET_MARGIN
        assert await db.get_session(session_id) is not None


@pytest.mark.asyncio
async def test_postgres_holder_listing_names_a_lock_outside_the_three_locked_tables(pg_url):
    # Past its LOCK TABLE the delete reads and deletes rows in other tables under a 250 ms
    # lock_timeout, so the lock that refused it can be on one of those. The listing names it.
    async with _imported_session(pg_url) as (db, holder, session_id):
        async with _lock_holder(holder, table="messages") as (holder_pid, _release):
            message = await _pg_cleanup._exhausted_message(db, session_id, _lock_refused())
        assert f"pid={holder_pid} " in message
        assert "relation=messages " in message
        assert "RowExclusiveLock" in message


@pytest.mark.asyncio
async def test_postgres_other_failure_propagates_on_the_first_call(pg_url, monkeypatch):
    async with _imported_session(pg_url) as (db, _holder, session_id):
        calls: list[str] = []
        failure = DBAPIError(
            "DELETE FROM sessions", None, _DriverError("deadlock detected", "40P01")
        )

        async def fail(sid, *, require_source_kind):
            calls.append(sid)
            raise failure

        with monkeypatch.context() as patch:
            patch.setattr(db, "delete_imported_session", fail)
            with pytest.raises(DBAPIError) as caught:
                await delete_imported_session_with_retry(db, session_id, require_source_kind=_KIND)
        assert caught.value is failure
        assert calls == [session_id]
        assert await db.get_session(session_id) is not None


@pytest.mark.asyncio
async def test_postgres_refused_delete_returns_false_without_a_retry(pg_url, monkeypatch):
    async with _imported_session(pg_url) as (db, _holder, session_id):
        calls: list[str] = []
        real = db.delete_imported_session

        async def counting(sid, *, require_source_kind):
            calls.append(require_source_kind)
            return await real(sid, require_source_kind=require_source_kind)

        monkeypatch.setattr(db, "delete_imported_session", counting)
        # The row's source_kind differs from the required one, so the delete is refused.
        refused = await delete_imported_session_with_retry(
            db, session_id, require_source_kind="imported_codex"
        )
        assert refused is False
        assert calls == ["imported_codex"]
        assert await db.get_session(session_id) is not None
