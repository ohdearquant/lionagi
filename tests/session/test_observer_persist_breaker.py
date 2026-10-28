# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Signal persistence while another connection holds the store's writer.

A real SQLite store and a second connection holding ``BEGIN IMMEDIATE``: the
first signal pays the busy wait, the ones after it (and the ones queued behind
it) are skipped for a window and counted, and the first signal after the window
tries once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

import lionagi.ln.concurrency as concurrency_mod
import lionagi.session.observer as observer_mod
from lionagi.session.observer import SessionObserver
from lionagi.session.signal import NodeStarted
from lionagi.state import engine as state_engine
from lionagi.state.db import StateDB

BUSY_MS = 200
SESSION_ID = "breaker-session"
SIGNALS = 10


@contextlib.asynccontextmanager
async def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(state_engine, "SQLITE_BUSY_TIMEOUT_MS", BUSY_MS)
    path = tmp_path / "state.db"
    db = StateDB(path)
    await db.open()
    blocker = sqlite3.connect(str(path), timeout=0, isolation_level=None)
    try:
        await db.create_progression("breaker-prog")
        await db.create_session(
            {
                "id": SESSION_ID,
                "created_at": 100.0,
                "progression_id": "breaker-prog",
                "name": "breaker",
                "status": "running",
                "invocation_kind": "agent",
            }
        )
        # Every write the store refused the writer lock: each one is a busy wait
        # that ran to its timeout, so their number is the number of busy waits paid.
        refused: list[str] = []
        real_insert = db.insert_session_signal

        async def _counting_insert(**kwargs):
            try:
                return await real_insert(**kwargs)
            except Exception as exc:
                if observer_mod._is_writer_lock_failure(exc):
                    refused.append(kwargs.get("op_id", ""))
                raise

        db.insert_session_signal = _counting_insert
        yield SimpleNamespace(db=db, blocker=blocker, refused=refused)
    finally:
        if blocker.in_transaction:
            blocker.rollback()
        blocker.close()
        await db.close()


@pytest.fixture
def clock(monkeypatch):
    """Replace the observer's clock; ``time.time`` stays real for signal timestamps."""
    fake = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(
        observer_mod,
        "time",
        SimpleNamespace(time=time.time, monotonic=lambda: fake.now),
        raising=False,
    )
    return fake


def _writer_open_failures(store) -> int:
    return len(store.refused)


def _observer_warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "lionagi.session.observer" and record.levelno == logging.WARNING
    ]


async def _signal_op_ids(db) -> list[str]:
    return [row["op_id"] for row in await db.get_session_signals_after(SESSION_ID, 0)]


async def test_a_held_writer_costs_one_busy_wait_and_every_skipped_signal_is_counted(
    tmp_path, monkeypatch, caplog
):
    async with _store(tmp_path, monkeypatch) as store:
        observer = SessionObserver()
        observer.bind_db_persistence(SESSION_ID, db=store.db)
        store.blocker.execute("BEGIN IMMEDIATE")
        with caplog.at_level(logging.WARNING):
            for i in range(SIGNALS):
                await observer.emit(NodeStarted(op_id=f"held-{i}", name="step"))
        store.blocker.rollback()
        persisted = await _signal_op_ids(store.db)

    assert _writer_open_failures(store) == 1
    assert persisted == []
    assert observer.persist_dropped == SIGNALS


async def test_signals_emitted_together_before_any_refusal_cost_one_busy_wait(
    tmp_path, monkeypatch, caplog
):
    async with _store(tmp_path, monkeypatch) as store:
        observer = SessionObserver()
        observer.bind_db_persistence(SESSION_ID, db=store.db)
        store.blocker.execute("BEGIN IMMEDIATE")
        with caplog.at_level(logging.WARNING):
            await asyncio.gather(
                *(
                    observer.emit(NodeStarted(op_id=f"burst-{i}", name="step"))
                    for i in range(SIGNALS)
                )
            )
        store.blocker.rollback()
        persisted = await _signal_op_ids(store.db)

    assert _writer_open_failures(store) == 1
    assert persisted == []
    assert observer.persist_dropped == SIGNALS


async def test_the_window_skips_even_a_free_writer_and_the_first_signal_after_it_lands(
    tmp_path, monkeypatch, caplog, clock
):
    async with _store(tmp_path, monkeypatch) as store:
        observer = SessionObserver()
        observer.bind_db_persistence(SESSION_ID, db=store.db)
        store.blocker.execute("BEGIN IMMEDIATE")
        with caplog.at_level(logging.WARNING):
            await observer.emit(NodeStarted(op_id="held", name="step"))
            await observer.emit(NodeStarted(op_id="inside-at-once", name="step"))
            assert _writer_open_failures(store) == 1

            store.blocker.rollback()
            clock.now += observer_mod._PERSIST_SKIP_WINDOW_SECONDS - 0.5
            await observer.emit(NodeStarted(op_id="inside-last", name="step"))
            clock.now += 0.5
            await observer.emit(NodeStarted(op_id="after", name="step"))
            await observer.emit(NodeStarted(op_id="after-again", name="step"))
        persisted = await _signal_op_ids(store.db)

    assert _writer_open_failures(store) == 1
    assert persisted == ["after", "after-again"]
    assert observer.persist_dropped == 3


async def test_a_writer_still_held_when_the_window_ends_reopens_it_after_one_try(
    tmp_path, monkeypatch, caplog, clock
):
    async with _store(tmp_path, monkeypatch) as store:
        observer = SessionObserver()
        observer.bind_db_persistence(SESSION_ID, db=store.db)
        store.blocker.execute("BEGIN IMMEDIATE")
        with caplog.at_level(logging.WARNING):
            await observer.emit(NodeStarted(op_id="held", name="step"))
            clock.now += 3600
            await observer.emit(NodeStarted(op_id="try", name="step"))
            await observer.emit(NodeStarted(op_id="reopened", name="step"))
            assert _writer_open_failures(store) == 2

            store.blocker.rollback()
            await observer.emit(NodeStarted(op_id="still-inside", name="step"))
        persisted = await _signal_op_ids(store.db)

    assert persisted == []
    assert observer.persist_dropped == 4
    # Reopening the window after the try is not a new warning.
    assert len(_observer_warnings(caplog)) == 1


async def test_concurrent_signals_at_the_end_of_a_window_share_one_try(
    tmp_path, monkeypatch, caplog, clock
):
    async with _store(tmp_path, monkeypatch) as store:
        observer = SessionObserver()
        observer.bind_db_persistence(SESSION_ID, db=store.db)
        store.blocker.execute("BEGIN IMMEDIATE")
        with caplog.at_level(logging.WARNING):
            await observer.emit(NodeStarted(op_id="held", name="step"))
            clock.now += 3600
            await asyncio.gather(
                *(
                    observer.emit(NodeStarted(op_id=f"together-{i}", name="step"))
                    for i in range(SIGNALS)
                )
            )
        store.blocker.rollback()

    assert _writer_open_failures(store) == 2
    assert observer.persist_dropped == 1 + SIGNALS


class _FailsOnce:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.attempts: list[str] = []

    async def insert_session_signal(self, **row):
        self.attempts.append(row["op_id"])
        if len(self.attempts) == 1:
            raise self.error


def _failure_during_lock_recovery() -> Exception:
    try:
        raise sqlite3.OperationalError("database is locked")
    except sqlite3.OperationalError:
        try:
            raise RuntimeError("write recovery failed")
        except RuntimeError as exc:
            return exc


async def test_a_failure_during_lock_recovery_does_not_open_a_skip_window(caplog):
    db = _FailsOnce(_failure_during_lock_recovery())
    observer = SessionObserver()
    observer.bind_db_persistence(SESSION_ID, db=db)

    with caplog.at_level(logging.WARNING):
        await observer.emit(NodeStarted(op_id="failed", name="step"))
        await observer.emit(NodeStarted(op_id="next", name="step"))

    assert db.attempts == ["failed", "next"]
    assert observer.persist_dropped == 1
    assert _observer_warnings(caplog) == []


@pytest.mark.parametrize(
    "error",
    [
        sqlite3.OperationalError("disk I/O error"),
        sqlite3.IntegrityError("UNIQUE constraint failed: session_signals.id"),
        RuntimeError("store handle is closed"),
    ],
    ids=["disk-io", "integrity", "runtime"],
)
async def test_a_failure_other_than_the_writer_lock_is_counted_and_opens_no_window(error, caplog):
    db = _FailsOnce(error)
    observer = SessionObserver()
    observer.bind_db_persistence(SESSION_ID, db=db)

    with caplog.at_level(logging.WARNING):
        await observer.emit(NodeStarted(op_id="fails", name="step"))
        await observer.emit(NodeStarted(op_id="next", name="step"))

    assert observer.persist_dropped == 1
    assert db.attempts == ["fails", "next"]
    assert _observer_warnings(caplog) == []


class _RefusesThenHolds:
    """Refuses the writer lock once, holds the next write until it is cancelled, then accepts."""

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self.holding = asyncio.Event()

    async def insert_session_signal(self, **row):
        self.attempts.append(row["op_id"])
        if len(self.attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        if len(self.attempts) == 2:
            self.holding.set()
            await asyncio.Event().wait()


async def test_a_try_cancelled_at_the_end_of_a_window_is_counted_and_the_next_signal_tries(
    clock,
):
    db = _RefusesThenHolds()
    observer = SessionObserver()
    observer.bind_db_persistence(SESSION_ID, db=db)
    await observer.emit(NodeStarted(op_id="refused", name="step"))
    clock.now += 3600

    trying = asyncio.create_task(observer.emit(NodeStarted(op_id="try", name="step")))
    await asyncio.wait_for(db.holding.wait(), 5)
    trying.cancel()
    with pytest.raises(asyncio.CancelledError):
        await trying
    assert observer.persist_dropped == 2

    await observer.emit(NodeStarted(op_id="next", name="step"))
    assert db.attempts == ["refused", "try", "next"]
    assert observer.persist_dropped == 2


class _CommitsThenHolds:
    """Takes the row, then holds the return until cancelled: a commit the caller never hears of."""

    def __init__(self) -> None:
        self.rows: list[str] = []
        self.committed = asyncio.Event()

    async def insert_session_signal(self, **row):
        self.rows.append(row["op_id"])
        self.committed.set()
        await asyncio.Event().wait()


async def test_a_write_cancelled_after_its_row_committed_is_still_counted():
    """The count is of signals the sink could not confirm it wrote, so it can exceed what the
    store is missing by the writes cancelled after they committed."""
    db = _CommitsThenHolds()
    observer = SessionObserver()
    observer.bind_db_persistence(SESSION_ID, db=db)
    writing = asyncio.create_task(observer.emit(NodeStarted(op_id="landed", name="step")))
    await asyncio.wait_for(db.committed.wait(), 5)
    writing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await writing

    assert db.rows == ["landed"]
    assert observer.persist_dropped == 1


class _HeldUntilReleased:
    def __init__(self) -> None:
        self.attempts: list[str] = []
        self.holding = asyncio.Event()
        self.release = asyncio.Event()

    async def insert_session_signal(self, **row):
        self.attempts.append(row["op_id"])
        self.holding.set()
        await self.release.wait()


class _AlwaysRefuses:
    def __init__(self) -> None:
        self.attempts: list[str] = []

    async def insert_session_signal(self, **row):
        self.attempts.append(row["op_id"])
        raise sqlite3.OperationalError("database is locked")


async def test_a_write_from_the_previous_binding_that_lands_late_leaves_the_new_binding_alone():
    old_db, new_db = _HeldUntilReleased(), _AlwaysRefuses()
    observer = SessionObserver()
    observer.bind_db_persistence("previous-session", db=old_db)
    in_flight = asyncio.create_task(observer.emit(NodeStarted(op_id="old", name="step")))
    try:
        await asyncio.wait_for(old_db.holding.wait(), 5)
        observer.bind_db_persistence(SESSION_ID, db=new_db)
        # Bounded: a binding that shared the previous one's lock would queue here
        # behind the write that is still held.
        await asyncio.wait_for(observer.emit(NodeStarted(op_id="new-refused", name="step")), 5)
    finally:
        old_db.release.set()
    await asyncio.wait_for(in_flight, 5)
    await observer.emit(NodeStarted(op_id="new-inside-window", name="step"))

    assert old_db.attempts == ["old"]
    assert new_db.attempts == ["new-refused"]
    assert observer.persist_dropped == 2


class _HeldThenFinishes:
    """Holds every write until released, then lands it or fails it."""

    def __init__(self, fails: bool) -> None:
        self.fails = fails
        self.attempts: list[str] = []
        self.holding = asyncio.Event()
        self.release = asyncio.Event()

    async def insert_session_signal(self, **row):
        self.attempts.append(row["op_id"])
        self.holding.set()
        await self.release.wait()
        if self.fails:
            raise RuntimeError("store handle is closed")


@pytest.mark.parametrize("fails", [True, False], ids=["fail-after-unbind", "land-after-unbind"])
async def test_writes_in_flight_at_unbind_are_in_the_count_read_then(fails):
    """Teardown reads the count right after unbind and then closes the store, so a write still
    writing and one queued behind it count as unconfirmed: whatever they do afterwards, the
    count read at unbind stays an upper bound."""
    db = _HeldThenFinishes(fails)
    observer = SessionObserver()
    observer.bind_db_persistence(SESSION_ID, db=db)
    writing = asyncio.create_task(observer.emit(NodeStarted(op_id="writing", name="step")))
    await asyncio.wait_for(db.holding.wait(), 5)
    queued = asyncio.create_task(observer.emit(NodeStarted(op_id="queued", name="step")))
    for _ in range(5):
        await asyncio.sleep(0)  # the queued emit reaches the binding's lock
    observer.unbind_db_persistence()
    at_unbind = observer.persist_dropped
    db.release.set()
    await asyncio.wait_for(asyncio.gather(writing, queued), 5)

    assert at_unbind == 2
    assert db.attempts == ["writing", "queued"]
    assert observer.persist_dropped == (2 if fails else 0)


async def test_a_dispatched_write_is_counted_before_unbind(monkeypatch):
    db = _HeldThenFinishes(fails=True)
    observer = SessionObserver()
    observer.bind_db_persistence(SESSION_ID, db=db)
    dispatched = asyncio.Event()
    resume = asyncio.Event()

    async def _wait_before_handlers(*handlers):
        dispatched.set()
        await resume.wait()
        return await asyncio.gather(*handlers)

    monkeypatch.setattr(concurrency_mod, "gather", _wait_before_handlers)
    emitting = asyncio.create_task(observer.emit(NodeStarted(op_id="queued", name="step")))
    await asyncio.wait_for(dispatched.wait(), 5)
    observer.unbind_db_persistence()
    at_unbind = observer.persist_dropped

    resume.set()
    await asyncio.wait_for(db.holding.wait(), 5)
    db.release.set()
    await asyncio.wait_for(emitting, 5)

    assert at_unbind == 1
    assert db.attempts == ["queued"]
    assert observer.persist_dropped == 1


class _Unrepresentable:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def __str__(self) -> str:
        raise self.error

    def __repr__(self) -> str:
        raise self.error


class _Accepts:
    def __init__(self) -> None:
        self.attempts: list[str] = []

    async def insert_session_signal(self, **row):
        self.attempts.append(row["op_id"])


@pytest.mark.parametrize(
    "error",
    [RuntimeError("repr rejected"), sqlite3.OperationalError("database is locked")],
    ids=["runtime-error", "lock-shaped-error"],
)
async def test_a_signal_whose_payload_cannot_be_built_is_counted_and_the_emit_goes_on(error):
    """The store was never asked, so even an error that reads like a refused writer lock
    opens no skip window and the next signal is written."""
    db = _Accepts()
    observer = SessionObserver()
    observer.bind_db_persistence(SESSION_ID, db=db)

    await observer.emit(NodeStarted(op_id="unbuildable", name="step", data=_Unrepresentable(error)))
    await observer.emit(NodeStarted(op_id="next", name="step"))

    assert observer.persist_dropped == 1
    assert db.attempts == ["next"]


class _OpIdRejected(NodeStarted):
    def __getattribute__(self, name):
        if name == "op_id":
            raise RuntimeError("op_id rejected")
        return super().__getattribute__(name)


class _OpIdLockShaped(NodeStarted):
    def __getattribute__(self, name):
        if name == "op_id":
            raise sqlite3.OperationalError("database is locked")
        return super().__getattribute__(name)


@pytest.mark.parametrize(
    "signal_type", [_OpIdRejected, _OpIdLockShaped], ids=["op-id", "lock-shaped-op-id"]
)
async def test_a_signal_whose_fields_cannot_be_read_is_counted_and_the_emit_goes_on(signal_type):
    """The store was never asked, so even a field error that reads like a refused writer lock
    opens no skip window and the next signal is written."""
    db = _Accepts()
    observer = SessionObserver()
    observer.bind_db_persistence(SESSION_ID, db=db)

    # The flow reads only the signal's id; op_id is first read by persistence.
    await observer.emit(signal_type(name="step"))
    await observer.emit(NodeStarted(op_id="next", name="step"))

    assert observer.persist_dropped == 1
    assert db.attempts == ["next"]


class _UnbindsWhenRepresented:
    """A payload whose repr unbinds the sink and reads the count, as teardown does."""

    def __init__(self, observer: SessionObserver) -> None:
        self.observer = observer
        self.read_at_unbind: list[int] = []

    def __str__(self) -> str:
        raise RuntimeError("str rejected")

    def __repr__(self) -> str:
        if not self.read_at_unbind:
            self.observer.unbind_db_persistence()
            self.read_at_unbind.append(self.observer.persist_dropped)
        return "<unbinds when represented>"


@pytest.mark.parametrize("fails", [True, False], ids=["fail-after-unbind", "land-after-unbind"])
async def test_a_signal_whose_payload_reaches_unbind_is_in_the_count_read_then(fails):
    """Building the payload runs the signal's own code before the write starts; a count read
    from there already includes the signal."""
    db = _HeldThenFinishes(fails)
    db.release.set()
    observer = SessionObserver()
    observer.bind_db_persistence(SESSION_ID, db=db)
    data = _UnbindsWhenRepresented(observer)

    await observer.emit(NodeStarted(op_id="reentrant", name="step", data=data))

    assert data.read_at_unbind == [1]
    assert db.attempts == ["reentrant"]
    assert observer.persist_dropped == (1 if fails else 0)


class _LockRefusalUnbindsWhenRepresented(sqlite3.OperationalError):
    """A refused writer lock whose repr unbinds the sink and reads the count, as teardown does."""

    sqlite_errorcode = 5  # SQLITE_BUSY
    observer: SessionObserver
    read_at_unbind: list[int]

    def __repr__(self) -> str:
        if not self.read_at_unbind:
            self.observer.unbind_db_persistence()
            self.read_at_unbind.append(self.observer.persist_dropped)
        return "OperationalError('database is locked')"


async def test_a_refused_write_whose_error_reaches_unbind_is_counted_once(caplog):
    """Logging the refusal runs the error's own repr; a count read from there holds the
    signal once."""
    observer = SessionObserver()
    error = _LockRefusalUnbindsWhenRepresented("database is locked")
    error.observer = observer
    error.read_at_unbind = []
    db = _FailsOnce(error)
    observer.bind_db_persistence(SESSION_ID, db=db)

    with caplog.at_level(logging.WARNING):
        await observer.emit(NodeStarted(op_id="refused", name="step"))

    assert error.read_at_unbind == [1]
    assert db.attempts == ["refused"]
    assert observer.persist_dropped == 1


class OperationalError(Exception):
    """Named as SQLite's error is, so the classifier reads its text, which cannot be read."""

    def __str__(self) -> str:
        raise RuntimeError("str rejected")


class _RaisingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        raise RuntimeError("log handler failed")


def _delivered(observer: SessionObserver) -> list[str]:
    seen: list[str] = []
    observer.observe(NodeStarted, handler=lambda event, _ctx: seen.append(event.op_id))
    return seen


async def test_an_error_that_cannot_be_classified_is_counted_and_the_emit_goes_on():
    db = _FailsOnce(OperationalError())
    observer = SessionObserver()
    seen = _delivered(observer)
    observer.bind_db_persistence(SESSION_ID, db=db)

    await observer.emit(NodeStarted(op_id="unclassified", name="step"))
    await observer.emit(NodeStarted(op_id="next", name="step"))

    assert seen == ["unclassified", "next"]
    assert db.attempts == ["unclassified", "next"]
    assert observer.persist_dropped == 1


async def test_a_log_handler_that_raises_loses_the_warning_only():
    """The window opens before the warning is logged, so a failing handler cannot keep it shut."""
    db = _FailsOnce(sqlite3.OperationalError("database is locked"))
    observer = SessionObserver()
    seen = _delivered(observer)
    observer.bind_db_persistence(SESSION_ID, db=db)
    logger = logging.getLogger(observer_mod.__name__)
    handler = _RaisingHandler(logging.WARNING)
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        await observer.emit(NodeStarted(op_id="refused", name="step"))
        await observer.emit(NodeStarted(op_id="skipped", name="step"))
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)

    assert seen == ["refused", "skipped"]
    assert db.attempts == ["refused"]
    assert observer.persist_dropped == 2


async def test_a_sink_with_no_store_writes_and_counts_nothing(monkeypatch):
    import lionagi.state.db as state_db

    opened: list[bool] = []
    monkeypatch.setattr(state_db, "state_db_known_absent", lambda: True)
    monkeypatch.setattr(state_db, "StateDB", lambda *a, **k: opened.append(True))
    observer = SessionObserver()
    observer.bind_db_persistence(SESSION_ID)

    await observer.emit(NodeStarted(op_id="nowhere", name="step"))
    observer.unbind_db_persistence()

    assert opened == []
    assert observer.persist_dropped == 0


async def test_one_warning_when_the_window_opens_and_one_teardown_line_with_the_count(
    tmp_path, monkeypatch, caplog
):
    async with _store(tmp_path, monkeypatch) as store:
        observer = SessionObserver()
        observer.bind_db_persistence(SESSION_ID, db=store.db)
        store.blocker.execute("BEGIN IMMEDIATE")
        with caplog.at_level(logging.WARNING):
            for i in range(SIGNALS):
                await observer.emit(NodeStarted(op_id=f"held-{i}", name="step"))
            opening = _observer_warnings(caplog)
            observer.unbind_db_persistence()
            observer.unbind_db_persistence()
        store.blocker.rollback()

    assert len(opening) == 1, opening
    assert SESSION_ID in opening[0]
    assert "writer lock" in opening[0]
    teardown = _observer_warnings(caplog)[1:]
    assert len(teardown) == 1, teardown
    assert f"could not confirm it wrote {SIGNALS} signal(s)" in teardown[0]
    assert SESSION_ID in teardown[0]
    assert observer.persist_dropped == SIGNALS


async def test_a_clean_binding_reports_zero_and_logs_no_teardown_line(
    tmp_path, monkeypatch, caplog
):
    async with _store(tmp_path, monkeypatch) as store:
        observer = SessionObserver()
        observer.bind_db_persistence(SESSION_ID, db=store.db)
        with caplog.at_level(logging.WARNING):
            await observer.emit(NodeStarted(op_id="clean", name="step"))
            observer.unbind_db_persistence()
        persisted = await _signal_op_ids(store.db)

    assert persisted == ["clean"]
    assert observer.persist_dropped == 0
    assert _observer_warnings(caplog) == []


async def test_a_second_binding_replaces_the_first_instead_of_writing_twice():
    first, second = _Accepts(), _Accepts()
    observer = SessionObserver()
    observer.bind_db_persistence("first-session", db=first)
    observer.bind_db_persistence(SESSION_ID, db=second)

    await observer.emit(NodeStarted(op_id="once", name="step"))

    assert first.attempts == []
    assert second.attempts == ["once"]


async def test_the_first_skip_window_of_a_binding_calls_back_once_on_the_event_loop(
    tmp_path, monkeypatch, clock
):
    calls: list[int] = []
    async with _store(tmp_path, monkeypatch) as store:
        observer = SessionObserver()
        observer.bind_db_persistence(
            SESSION_ID, db=store.db, on_window_opened=lambda: calls.append(threading.get_ident())
        )
        store.blocker.execute("BEGIN IMMEDIATE")
        await observer.emit(NodeStarted(op_id="held", name="step"))
        await observer.emit(NodeStarted(op_id="inside", name="step"))
        assert calls == [threading.get_ident()]

        store.blocker.rollback()
        clock.now += 3600
        await observer.emit(NodeStarted(op_id="lands", name="step"))  # the window closes
        store.blocker.execute("BEGIN IMMEDIATE")
        await observer.emit(NodeStarted(op_id="held-again", name="step"))  # and opens again
        store.blocker.rollback()
        persisted = await _signal_op_ids(store.db)

    assert persisted == ["lands"]
    assert _writer_open_failures(store) == 2
    assert calls == [threading.get_ident()]


async def test_a_new_binding_calls_back_for_its_own_first_window(tmp_path, monkeypatch):
    first: list[str] = []
    second: list[str] = []
    async with _store(tmp_path, monkeypatch) as store:
        observer = SessionObserver()
        observer.bind_db_persistence(
            SESSION_ID, db=store.db, on_window_opened=lambda: first.append("opened")
        )
        store.blocker.execute("BEGIN IMMEDIATE")
        await observer.emit(NodeStarted(op_id="held", name="step"))
        observer.bind_db_persistence(
            SESSION_ID, db=store.db, on_window_opened=lambda: second.append("opened")
        )
        await observer.emit(NodeStarted(op_id="held-by-the-new-binding", name="step"))
        store.blocker.rollback()

    assert first == ["opened"]
    assert second == ["opened"]


async def test_no_callback_without_a_window(tmp_path, monkeypatch):
    calls: list[str] = []
    async with _store(tmp_path, monkeypatch) as store:
        observer = SessionObserver()
        observer.bind_db_persistence(
            SESSION_ID, db=store.db, on_window_opened=lambda: calls.append("opened")
        )
        await observer.emit(NodeStarted(op_id="clean", name="step"))
        persisted = await _signal_op_ids(store.db)

    failing = _FailsOnce(sqlite3.OperationalError("disk I/O error"))
    other = SessionObserver()
    other.bind_db_persistence(
        SESSION_ID, db=failing, on_window_opened=lambda: calls.append("opened")
    )
    await other.emit(NodeStarted(op_id="fails", name="step"))  # a failure that is not the lock

    assert persisted == ["clean"]
    assert other.persist_dropped == 1
    assert calls == []


async def test_a_failing_window_callback_changes_nothing_in_persistence(
    tmp_path, monkeypatch, caplog, clock
):
    def broken() -> None:
        raise RuntimeError("callback broke")

    async with _store(tmp_path, monkeypatch) as store:
        observer = SessionObserver()
        observer.bind_db_persistence(SESSION_ID, db=store.db, on_window_opened=broken)
        store.blocker.execute("BEGIN IMMEDIATE")
        with caplog.at_level(logging.WARNING):
            for i in range(SIGNALS):
                await observer.emit(NodeStarted(op_id=f"held-{i}", name="step"))
            store.blocker.rollback()
            clock.now += 3600
            await observer.emit(NodeStarted(op_id="after", name="step"))
        persisted = await _signal_op_ids(store.db)

    assert persisted == ["after"]
    assert observer.persist_dropped == SIGNALS
    assert _writer_open_failures(store) == 1
    warnings = _observer_warnings(caplog)
    assert len(warnings) == 1 and "writer lock" in warnings[0], warnings
