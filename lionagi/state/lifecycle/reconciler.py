# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Bounded, at-least-once delivery of persisted terminal transitions.

Capacity is shared per store and consumer in one process for synchronous
deliveries and asynchronous tasks. Timeout or caller cancellation requests
task cancellation, but a task still running retains its slot. If its loop
closes before it finishes, the event stays unacknowledged. An awaitable completed
by another thread after its owning loop closed may be observed and released on
a later pass; otherwise its slot is held until the process exits. An
unacknowledged event in a surviving durable store can be replayed after process
restart.
Tasks do not track work delegated beyond their own lifetime to executors,
threads, processes, or independent operations. Separate processes may also
deliver the same event. Consumers should deduplicate effects by event ID.

For a completed late success against a private in-memory store, a stopped but
restartable original loop retains the completed reservation for acknowledgement
on its next pass. If that loop is permanently closed, the finished reservation
is released without acknowledgement, and the fact remains owed. No other loop
is used to drive the original private-memory connection.

Each observed saturated pass advances its warning episode's version, so a stale
retired pass cannot erase an episode subsequently observed as active. A
reactivation with no subsequent saturated observation is not distinguishable
here as a separate in-process warning episode.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import inspect
import json
import logging
import math
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal
from urllib.parse import parse_qsl, unquote, urlsplit

from sqlalchemy import text
from sqlalchemy.dialects.sqlite.aiosqlite import SQLiteDialect_aiosqlite
from sqlalchemy.engine import make_url

from .callbacks import HANDLER_BUDGET_SECONDS
from .deliveries import (
    ConsumerNotRegisteredError,
    ConsumerRetiredError,
    ConsumerSchemaError,
    ack_delivery,
    eligibility_counts,
    reconcile_unacknowledged,
    register_consumer,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 4
BACKOFF_CAPACITY = 512
BACKOFF_MAX_SECONDS = 3600.0
SATURATION_WARNING_INTERVAL_SECONDS = 600.0


class DeliveryOutcome(Enum):
    SUCCESS = "success"
    NOT_APPLICABLE = "not_applicable"
    NO_SESSION = "no_session"
    AMBIGUOUS_PROJECT = "ambiguous_project"
    FAILURE = "failure"


_ACKNOWLEDGE_OUTCOMES = frozenset(
    {
        DeliveryOutcome.SUCCESS,
        DeliveryOutcome.NOT_APPLICABLE,
        DeliveryOutcome.NO_SESSION,
        DeliveryOutcome.AMBIGUOUS_PROJECT,
    }
)


async def _consumer_is_active(db: Any, consumer: str) -> bool:
    """Linearize an attempt against a concurrent one-statement retirement."""
    async with db._read() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT 1 FROM terminal_consumers WHERE name = :consumer AND retired_at IS NULL"
                ),
                {"consumer": consumer},
            )
        ).first()
    return row is not None


@dataclass(frozen=True)
class PreparedDelivery:
    """A single operation returning an explicit delivery outcome."""

    call: Callable[[], DeliveryOutcome | Awaitable[DeliveryOutcome]]


PrepareDelivery = Callable[[dict[str, Any], Any], Awaitable[PreparedDelivery | DeliveryOutcome]]


@dataclass
class _Backoff:
    attempts: int
    retry_at: float


@dataclass(eq=False)
class _Worker:
    """One process-wide delivery reservation."""

    key: tuple[str, str, str]
    db: Any
    loop: asyncio.AbstractEventLoop
    future: concurrent.futures.Future[Any]
    limit: int
    engine: Any
    keeper: sqlite3.Connection | None = None
    detached: bool = False
    finished: bool = False
    late_started: bool = False
    late_claimed: bool = False
    deferred: bool = False
    async_loop: asyncio.AbstractEventLoop | None = None
    async_task: asyncio.Future[Any] | None = None
    abandoned: bool = False
    late_settled: tuple[bool, Any] | None = None
    late_task: asyncio.Future[Any] | None = None
    started_at: float = 0.0
    deadline: float = 0.0
    timed_out: bool = False
    clock: Callable[[], float] = time.monotonic


@dataclass
class _SaturationWarning:
    clock: Callable[[], float]
    last_at: float
    threshold: int
    version: int = 0


_workers_lock = threading.Lock()
_workers: dict[tuple[str, str, str], _Worker] = {}
_saturation_warnings: dict[tuple[str, str], _SaturationWarning] = {}


def _store_identity(db: Any) -> str:
    """Recognize SQLite path aliases without merging separate memory databases."""
    if db.dialect == "sqlite":
        # Ask the same dialect that opens the DB: driver options such as
        # ``?timeout=5`` are not part of the filename, and URI filenames
        # require percent decoding and SQLite's memory-mode interpretation.
        (filename,), options = SQLiteDialect_aiosqlite().create_connect_args(make_url(db.url))
        if options.get("uri") and filename.startswith("file:"):
            uri = urlsplit(filename)
            params = dict(parse_qsl(uri.query, keep_blank_values=True))
            location = unquote(uri.path)
            if not location or location == ":memory:" or params.get("mode") == "memory":
                if location and params.get("cache") == "shared":
                    return f"sqlite-memory:shared:{location}"
                return f"sqlite-memory:private:{id(db)}:{id(db._engine)}"
            # SQLite only accepts an empty URI authority or localhost.
            path = location if uri.netloc in ("", "localhost") else filename
        else:
            path = filename
            if path in ("", ":memory:"):
                return f"sqlite-memory:private:{id(db)}:{id(db._engine)}"
        try:
            stat = os.stat(path)
        except OSError:
            return f"sqlite-path:{os.path.realpath(path)}"
        return f"sqlite-inode:{stat.st_dev}:{stat.st_ino}"
    return str(db.url)


def _reserve_worker(
    store: str,
    db: Any,
    consumer: str,
    transition_id: str,
    limit: int,
    *,
    started_at: float | None = None,
    timeout_seconds: float = HANDLER_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> _Worker | None:
    key = (store, consumer, transition_id)
    if started_at is None:
        started_at = clock()
    with _workers_lock:
        active = [worker for k, worker in _workers.items() if k[:2] == key[:2]]
        capacity = min([limit, *(worker.limit for worker in active)])
        if key in _workers or len(active) >= capacity:
            return None
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        # A loop closing can cancel its asyncio wrapper, but must never cancel
        # the underlying record for a thread that is still executing.
        future.set_running_or_notify_cancel()
        keeper = None
        if store.startswith("sqlite-memory:shared:"):
            # Keep the exact shared-memory generation alive independently of
            # StateDB handles and their event loops until this reservation ends.
            (filename,), options = SQLiteDialect_aiosqlite().create_connect_args(make_url(db.url))
            keeper = sqlite3.connect(
                filename, uri=bool(options.get("uri")), check_same_thread=False
            )
            keeper.execute("PRAGMA foreign_keys = ON")
        worker = _Worker(key, db, asyncio.get_running_loop(), future, limit, db._engine, keeper)
        worker.started_at = started_at
        worker.deadline = worker.started_at + timeout_seconds
        worker.clock = clock
        _workers[key] = worker
        return worker


def _release_worker(worker: _Worker) -> None:
    keeper = None
    with _workers_lock:
        if _workers.get(worker.key) is worker:
            del _workers[worker.key]
            keeper, worker.keeper = worker.keeper, None
            scope = worker.key[:2]
            warning = _saturation_warnings.get(scope)
            if warning is not None:
                remaining = sum(key[:2] == scope for key in _workers)
                if remaining < warning.threshold:
                    del _saturation_warnings[scope]
    if keeper is not None:
        keeper.close()


def _release_closed_loop_workers(store: str, consumer: str) -> None:
    """Observe finished tasks without disturbing their stopped or closed loops."""
    with _workers_lock:
        completed = []
        completed_late = []
        for key, worker in _workers.items():
            if key[:2] != (store, consumer):
                continue
            task, loop = worker.async_task, worker.async_loop
            if loop is not None:
                closed = loop.is_closed()
                stopped_foreign = (
                    store.startswith(("sqlite-inode:", "sqlite-path:"))
                    and loop is not worker.loop
                    and not loop.is_running()
                )
                if task is not None and task.done() and (closed or stopped_foreign):
                    completed.append((worker, task))
                elif closed:
                    worker.abandoned = True
            late_task = worker.late_task
            if worker.detached and late_task is None and worker.future.done():
                try:
                    candidate = worker.future.result()
                except BaseException:
                    candidate = None
                if isinstance(candidate, asyncio.Future):
                    late_task = candidate
                    worker.late_task = candidate
            if worker.detached and late_task is not None:
                late_loop = late_task.get_loop()
                if (
                    store.startswith(("sqlite-inode:", "sqlite-path:"))
                    and late_loop is not worker.loop
                    and not late_loop.is_running()
                    and late_task.done()
                ):
                    completed_late.append((worker, late_task))
        closed = [
            worker
            for key, worker in _workers.items()
            if key[:2] == (store, consumer)
            and key[0].startswith("sqlite-memory:private:")
            and worker.deferred
            and worker.loop.is_closed()
        ]
    for worker, task in completed:
        _async_delivery_done(worker, task)
    for worker, task in completed_late:
        _late_delivery_task_done(worker, task)
    for worker in closed:
        _release_worker(worker)


class _MemoryStoreChangedError(RuntimeError):
    """A private in-memory store was closed or replaced before acknowledgement."""


class _LoopClosedBeforeCompletionError(RuntimeError):
    """An unfinished task cannot be observed on its permanently closed loop."""


def _abandon_worker(worker: _Worker) -> None:
    """Keep unobservable unfinished work reserved for this process's lifetime."""
    with _workers_lock:
        if _workers.get(worker.key) is not worker or worker.late_claimed:
            return
        worker.abandoned = True
    logger.warning("Terminal delivery loop closed before completion for %s", worker.key[2])


def _keeper_ack(worker: _Worker) -> None:
    """Acknowledge on the original shared-memory connection without schema initialization."""
    assert worker.keeper is not None
    conn = worker.keeper
    required = {
        "schema_meta": {"key", "value"},
        "status_transitions": {"id", "delivery_eligible", "created_at"},
        "terminal_consumers": {"name", "registered_at", "retired_at"},
        "terminal_deliveries": {"transition_id", "consumer", "acked_at"},
    }
    # Shared-cache SQLITE_LOCKED does not honor busy_timeout. Retry briefly,
    # then keep the completed worker deferred for a subsequent pass.
    for attempt in range(20):
        try:
            conn.execute("BEGIN IMMEDIATE")
            columns = {
                table: {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
                for table in required
            }
            version = None
            if required["schema_meta"] <= columns["schema_meta"]:
                row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
                version = row[0] if row else None
            if any(not cols <= columns[table] for table, cols in required.items()):
                raise ConsumerSchemaError(
                    f"terminal consumer schema unavailable at schema version {version!s}"
                )
            marker_row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = ?",
                ("migration.terminal_delivery_eligibility",),
            ).fetchone()
            try:
                marker = json.loads(marker_row[0]) if marker_row else None
                initial, classified_at = marker["initial_null_count"], marker["classified_at"]
                if isinstance(initial, bool) or not isinstance(initial, int) or initial < 0:
                    raise ValueError("invalid initial NULL count")
                if isinstance(classified_at, bool) or not isinstance(classified_at, (int, float)):
                    raise ValueError("invalid classification time")
            except (TypeError, ValueError, KeyError) as exc:
                raise ConsumerSchemaError(
                    f"terminal consumer classification unavailable at schema version {version!s}"
                ) from exc
            if (
                conn.execute(
                    "SELECT 1 FROM terminal_consumers WHERE name = ?", (worker.key[1],)
                ).fetchone()
                is None
            ):
                raise ConsumerNotRegisteredError(f"consumer {worker.key[1]!r} is not registered")
            conn.execute(
                "INSERT INTO terminal_deliveries (transition_id, consumer, acked_at) "
                "VALUES (?, ?, ?) ON CONFLICT (transition_id, consumer) DO NOTHING",
                (worker.key[2], worker.key[1], time.time()),
            )
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            conn.rollback()
            code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
            retryable = code in (sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY) or (
                not code and ("locked" in str(exc).lower() or "busy" in str(exc).lower())
            )
            if not retryable or attempt == 19:
                raise
            time.sleep(0.01)
        except BaseException:
            conn.rollback()
            raise


def _finish_late_result(worker: _Worker, result: Any) -> None:
    """Record an already-completed detached delivery without reopening its store schema."""
    try:
        if result not in _ACKNOWLEDGE_OUTCOMES:
            logger.warning("Late terminal delivery failed for %s", worker.key[2])
            _release_worker(worker)
            return
        if worker.key[0].startswith("sqlite-memory:shared:"):
            # The keeper pins the generation and writes directly, avoiding
            # StateDB.open() and its schema initialization/locking.
            try:
                _keeper_ack(worker)
            except Exception:
                logger.exception(
                    "Late shared-memory acknowledgement deferred for %s", worker.key[2]
                )
                with _workers_lock:
                    worker.deferred = True
                return
        elif worker.key[0].startswith("sqlite-memory:"):
            if worker.db._engine is not worker.engine or worker.db._engine is None:
                logger.warning("Private memory store closed before late ACK for %s", worker.key[2])
                _release_worker(worker)
                return

            async def acknowledge_private() -> None:
                async with worker.db._holding_open_lock():
                    if worker.db._engine is not worker.engine:
                        raise _MemoryStoreChangedError
                    await ack_delivery(worker.db, worker.key[2], worker.key[1])

            if worker.loop.is_closed():
                # A closed loop cannot drain this private-memory ACK. Leave
                # the fact owed without driving the connection on another loop.
                _release_worker(worker)
                return
            if not worker.loop.is_running():
                # Its private-memory connection belongs to that loop. A later
                # pass on the same loop may drain this completed delivery.
                with _workers_lock:
                    worker.deferred = True
                return

            coroutine = acknowledge_private()
            try:
                ack = asyncio.run_coroutine_threadsafe(coroutine, worker.loop)
            except RuntimeError:
                coroutine.close()
                with _workers_lock:
                    worker.deferred = True
                return
            try:
                ack.result(timeout=HANDLER_BUDGET_SECONDS)
            except _MemoryStoreChangedError:
                _release_worker(worker)
                return
            except Exception:
                # Keep completed work for retry if the same private store lives.
                ack.cancel()
                with _workers_lock:
                    worker.deferred = True
                return
        else:
            from lionagi.state.db import StateDB, _install_begin_immediate
            from lionagi.state.engine import make_engine

            async def acknowledge() -> None:
                expected = worker.key[0]
                if worker.db.dialect == "sqlite" and _store_identity(worker.db) != expected:
                    raise RuntimeError("SQLite store identity changed before late acknowledgement")
                db = StateDB(url=worker.db.url)

                def check_opened(dbapi_conn) -> None:
                    cursor = dbapi_conn.cursor()
                    try:
                        cursor.execute("PRAGMA database_list")
                        opened = next((r[2] for r in cursor.fetchall() if r[1] == "main"), "")
                        stat = os.stat(opened)
                        if f"sqlite-inode:{stat.st_dev}:{stat.st_ino}" != expected:
                            raise RuntimeError(
                                "SQLite store identity changed during late acknowledgement"
                            )
                    finally:
                        cursor.close()

                # Deliberately do not call StateDB.open(): it applies the schema.
                db._engine = make_engine(
                    db.url, before_settings=check_opened if db.dialect == "sqlite" else None
                )
                if db.dialect == "sqlite":
                    _install_begin_immediate(db._engine.sync_engine)
                try:
                    await ack_delivery(db, worker.key[2], worker.key[1])
                finally:
                    await db.close()

            asyncio.run(acknowledge())
    except Exception:
        logger.exception("Late terminal delivery or acknowledgement failed for %s", worker.key[2])
    _release_worker(worker)


async def _settle_late_awaitable(worker: _Worker, awaitable: Awaitable[Any]) -> Any:
    """Request cancellation on budget expiry, but wait for the actual operation."""
    try:
        with _workers_lock:
            if worker.late_claimed or _workers.get(worker.key) is not worker:
                return DeliveryOutcome.FAILURE
            task = asyncio.ensure_future(awaitable)
            worker.late_task = task
        done, _ = await asyncio.wait({task}, timeout=HANDLER_BUDGET_SECONDS)
        if not done:
            task.cancel()
        try:
            result = await task
        except asyncio.CancelledError:
            result = DeliveryOutcome.FAILURE
    except BaseException as exc:
        with _workers_lock:
            worker.late_settled = (False, exc)
        raise
    with _workers_lock:
        worker.late_settled = (True, result)
    return result


def _close_late_coroutine(coroutine: Any) -> None:
    """Best-effort cleanup must not decide whether unfinished work is settled."""
    if inspect.iscoroutine(coroutine):
        try:
            coroutine.close()
        except BaseException:
            logger.warning(
                "Unable to close an unfinished terminal delivery coroutine", exc_info=True
            )


def _claim_late_completion(worker: _Worker) -> bool:
    """Only one observer may record or release a detached delivery."""
    with _workers_lock:
        if _workers.get(worker.key) is not worker or worker.late_claimed:
            return False
        worker.late_claimed = True
        return True


def _finish_late(worker: _Worker) -> None:
    """Complete a detached synchronous factory, including a returned awaitable."""
    try:
        with _workers_lock:
            if worker.late_claimed or _workers.get(worker.key) is not worker:
                return
        result = worker.future.result()
        if isinstance(result, asyncio.Future) and result.done():
            result = result.result()
        elif inspect.isawaitable(result):
            # Loop-bound Futures must be consumed on their own loop. A coroutine
            # returned by a worker uses the originating loop while it is alive.
            loop = result.get_loop() if isinstance(result, asyncio.Future) else worker.loop
            if isinstance(result, asyncio.Future):
                with _workers_lock:
                    if worker.late_claimed or _workers.get(worker.key) is not worker:
                        return
                    worker.late_task = result
            if not loop.is_closed():
                coroutine = _settle_late_awaitable(worker, result)
                try:
                    with _workers_lock:
                        if worker.late_claimed or _workers.get(worker.key) is not worker:
                            coroutine.close()
                            return
                        pending = asyncio.run_coroutine_threadsafe(coroutine, loop)
                except RuntimeError:
                    _close_late_coroutine(coroutine)
                    _close_late_coroutine(result)
                    if loop.is_closed():
                        if isinstance(result, asyncio.Future) and result.done():
                            result = result.result()
                            if _claim_late_completion(worker):
                                _finish_late_result(worker, result)
                            return
                        raise _LoopClosedBeforeCompletionError from None
                    raise
                while True:
                    try:
                        result = pending.result(timeout=0.1)
                        break
                    except concurrent.futures.TimeoutError:
                        with _workers_lock:
                            if worker.late_claimed or _workers.get(worker.key) is not worker:
                                return
                        if pending.done():
                            # Completion can race the wait timeout. Reading
                            # again preserves a successful result and still
                            # propagates a hook's own TimeoutError.
                            result = pending.result()
                            break
                        if loop.is_closed():
                            with _workers_lock:
                                settled = worker.late_settled
                                task = worker.late_task
                            if settled is not None:
                                success, value = settled
                                if not success:
                                    raise value from None
                                result = value
                                break
                            if task is not None and task.done():
                                # The hook finished, but the wait wrapper's
                                # completion callback never ran before closure.
                                pending.cancel()
                                _close_late_coroutine(coroutine)
                                try:
                                    result = task.result()
                                except asyncio.CancelledError:
                                    result = DeliveryOutcome.FAILURE
                                break
                            if isinstance(result, asyncio.Future) and result.done():
                                # The wrapper never started, but the Future
                                # returned by the factory has completed.
                                pending.cancel()
                                _close_late_coroutine(coroutine)
                                result = result.result()
                                break
                            pending.cancel()
                            _close_late_coroutine(coroutine)
                            _close_late_coroutine(result)
                            raise _LoopClosedBeforeCompletionError from None
            elif isinstance(result, asyncio.Future):
                if result.done():
                    result = result.result()
                else:
                    raise _LoopClosedBeforeCompletionError
            else:
                # A bare coroutine may capture resources from the old loop.
                # Closing it prevents an unawaited-coroutine warning, but does
                # not prove that the delivery has completed.
                _close_late_coroutine(result)
                raise _LoopClosedBeforeCompletionError
    except _LoopClosedBeforeCompletionError:
        _abandon_worker(worker)
        return
    except BaseException:
        if _claim_late_completion(worker):
            logger.exception("Late terminal delivery failed for %s", worker.key[2])
            _release_worker(worker)
        return
    if _claim_late_completion(worker):
        _finish_late_result(worker, result)


def _async_delivery_done(worker: _Worker, task: asyncio.Future[Any]) -> None:
    """Transfer a detached async result to the late acknowledgement path once."""
    with _workers_lock:
        if (
            _workers.get(worker.key) is not worker
            or worker.async_task is not task
            or worker.late_claimed
        ):
            return
        worker.async_task = None
        worker.async_loop = None
        worker.late_claimed = True
    _record_claimed_task(worker, task)


def _late_delivery_task_done(worker: _Worker, task: asyncio.Future[Any]) -> None:
    """Claim a detached factory's completed foreign-loop task once."""
    with _workers_lock:
        if (
            _workers.get(worker.key) is not worker
            or worker.late_task is not task
            or worker.late_claimed
        ):
            return
        worker.late_claimed = True
    _record_claimed_task(worker, task)


def _record_claimed_task(worker: _Worker, task: asyncio.Future[Any]) -> None:
    try:
        result = task.result()
    except asyncio.CancelledError:
        _release_worker(worker)
        return
    except BaseException:
        logger.warning("terminal delivery hook failed after cancellation", exc_info=True)
        _release_worker(worker)
        return
    try:
        threading.Thread(target=_finish_late_result, args=(worker, result), daemon=True).start()
    except Exception:
        logger.exception("Could not record late terminal delivery for %s", worker.key[2])
        _release_worker(worker)


async def _await_reserved_hook(
    worker: _Worker, awaitable: Awaitable[Any], timeout: float
) -> tuple[DeliveryOutcome, _Worker | None]:
    """Keep the reservation if the hook survives its timeout or caller cancellation."""
    task = asyncio.ensure_future(awaitable)
    task_loop = task.get_loop()
    current_loop = asyncio.get_running_loop()
    waiter: asyncio.Future[Any] = task
    if task_loop is not current_loop:
        # The returned Future may already belong to another loop. Observe it
        # there and wake this loop without moving or cancelling its wrapper.
        completed: concurrent.futures.Future[None] = concurrent.futures.Future()
        waiter = asyncio.wrap_future(completed)

        def signal_completion(_task: asyncio.Future[Any]) -> None:
            if not completed.done():
                completed.set_result(None)

        if task.done():
            signal_completion(task)
        else:
            try:
                task_loop.call_soon_threadsafe(task.add_done_callback, signal_completion)
            except RuntimeError:
                if not task_loop.is_closed():
                    raise
                if task.done():
                    try:
                        return task.result(), worker
                    except asyncio.CancelledError:
                        return DeliveryOutcome.FAILURE, worker
                with _workers_lock:
                    worker.async_loop = task_loop
                    worker.async_task = task
                _abandon_worker(worker)
                return DeliveryOutcome.FAILURE, None

    def detach() -> None:
        with _workers_lock:
            worker.async_loop = task_loop
            worker.async_task = task

        def attach_and_cancel() -> None:
            with _workers_lock:
                if worker.late_claimed or worker.async_task is not task:
                    return
            task.add_done_callback(lambda completed: _async_delivery_done(worker, completed))
            task.cancel()

        if task_loop is current_loop:
            attach_and_cancel()
        else:
            try:
                task_loop.call_soon_threadsafe(attach_and_cancel)
            except RuntimeError:
                if task.done():
                    _async_delivery_done(worker, task)
                else:
                    _abandon_worker(worker)

    try:
        done, _ = await asyncio.wait({waiter}, timeout=timeout)
    except asyncio.CancelledError:
        detach()
        raise
    if not done:
        with _workers_lock:
            worker.timed_out = True
        detach()
        return DeliveryOutcome.FAILURE, None
    try:
        return task.result(), worker
    except asyncio.CancelledError:
        return DeliveryOutcome.FAILURE, worker


def _start_late(worker: _Worker) -> None:
    with _workers_lock:
        if worker.late_started or worker.late_claimed:
            return
        worker.late_started = True
    threading.Thread(target=_finish_late, args=(worker,), daemon=True).start()


def _detach_worker(worker: _Worker) -> None:
    with _workers_lock:
        worker.detached = True
        finished = worker.finished
    if finished:
        _start_late(worker)


def _run_worker(worker: _Worker, call: Callable[[], Any]) -> None:
    try:
        worker.future.set_result(call())
    except BaseException as exc:
        if not isinstance(exc, Exception):
            exc = RuntimeError(f"synchronous delivery raised {type(exc).__name__}")
        worker.future.set_exception(exc)
    finally:
        with _workers_lock:
            worker.finished = True
            detached = worker.detached
        if detached:
            _start_late(worker)


async def _drain_deferred(db: Any, consumer: str, store: str) -> None:
    """Record completed memory-store deliveries even if their original loop ended."""
    loop = asyncio.get_running_loop()
    while True:
        with _workers_lock:
            worker = next(
                (
                    w
                    for key, w in _workers.items()
                    if key[:2] == (store, consumer)
                    and w.deferred
                    and (not store.startswith("sqlite-memory:private:") or w.loop is loop)
                ),
                None,
            )
            if worker is not None:
                worker.deferred = False
        if worker is None:
            return
        try:
            if store.startswith("sqlite-memory:private:"):
                async with db._holding_open_lock():
                    if db._engine is not worker.engine:
                        raise _MemoryStoreChangedError
                    await ack_delivery(db, worker.key[2], consumer)
            else:
                await ack_delivery(db, worker.key[2], consumer)
        except _MemoryStoreChangedError:
            _release_worker(worker)
        except BaseException:
            with _workers_lock:
                worker.deferred = True
            raise
        else:
            _release_worker(worker)


class _WorkerBusyError(Exception):
    """A process-local synchronous reservation or capacity is in use."""


class TerminalReconciler:
    """A process-local retry cursor; independent processes may deliver the same fact.

    The cursor rotates through eligible pending rows. Backoff and attempts are
    bounded, best effort, and reset on eviction or restart. ``start`` is the
    baseline the consumer registers with on first use. A pass reads pages of
    ``batch_size`` rows until the traversal wraps or ``pass_budget`` seconds have
    passed; the budget is checked between pages, so the default of zero attempts
    one page per pass.

    The process-local capacity tracks each delivery task through timeout and
    cancellation. If its loop closes before the task completes, the slot stays
    occupied until completion is observed, or until process exit if it is not
    observed. The event remains unacknowledged until a successful completion is
    recorded. Work a handler delegates beyond the task's lifetime is outside
    this guarantee; consumers should deduplicate by event ID.
    """

    def __init__(
        self,
        consumer: str,
        prepare: PrepareDelivery,
        *,
        batch_size: int = BATCH_SIZE,
        backoff_capacity: int = BACKOFF_CAPACITY,
        timeout: float = HANDLER_BUDGET_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        start: Literal["now", "all"] = "all",
        pass_budget: float = 0.0,
    ) -> None:
        if batch_size < 1 or backoff_capacity < batch_size or backoff_capacity > BACKOFF_CAPACITY:
            raise ValueError("invalid reconciliation batch or backoff capacity")
        if start not in ("now", "all"):
            raise ValueError("start must be 'now' or 'all'")
        if not math.isfinite(pass_budget) or pass_budget < 0:
            raise ValueError("pass budget must be a finite, non-negative number of seconds")
        self.consumer = consumer
        self.prepare = prepare
        self.start = start
        self.pass_budget = pass_budget
        self.batch_size = batch_size
        self.backoff_capacity = backoff_capacity
        self.timeout = timeout
        self.clock = clock
        self._store_identity: str | None = None
        self._cursor: tuple[float, str] | None = None
        self._backoff: OrderedDict[str, _Backoff] = OrderedDict()
        self._lock = asyncio.Lock()

    def _fail(self, transition_id: str) -> None:
        previous = self._backoff.pop(transition_id, None)
        attempts = (previous.attempts if previous else 0) + 1
        delay = min(2 ** min(attempts - 1, 12), BACKOFF_MAX_SECONDS)
        if len(self._backoff) >= self.backoff_capacity:
            self._backoff.popitem(last=False)
        self._backoff[transition_id] = _Backoff(attempts, self.clock() + delay)

    def _pending(self) -> tuple[set[str], int]:
        assert self._store_identity is not None
        _release_closed_loop_workers(self._store_identity, self.consumer)
        with _workers_lock:
            keys = {key[2] for key in _workers if key[:2] == (self._store_identity, self.consumer)}
        return keys, len(keys)

    def _report_capacity(
        self, store: str, result: dict[str, int | float], *, retired: bool = False
    ) -> None:
        """Snapshot shared outstanding work and rate-limit saturation warnings."""
        scope = (store, self.consumer)
        warn = False
        with _workers_lock:
            # Capture immutable fields together. Clock samples happen just
            # afterward, so ages may be slightly later than membership.
            active = [
                (
                    worker,
                    worker.started_at,
                    worker.deadline,
                    worker.timed_out,
                    worker.limit,
                    worker.clock,
                )
                for key, worker in _workers.items()
                if key[:2] == scope
            ]
            previous = _saturation_warnings.get(scope)
            previous_state = (
                (previous.clock, previous.last_at, previous.threshold, previous.version)
                if previous
                else None
            )
        clocks = [self.clock, *(entry[5] for entry in active)]
        if previous_state is not None:
            clocks.append(previous_state[0])
        # Injected clocks are caller code and may re-enter the reconciler.
        samples: dict[int, float] = {}
        for clock in clocks:
            if id(clock) not in samples:
                samples[id(clock)] = clock()
        ages = [max(0.0, samples[id(clock)] - start) for _, start, _, _, _, clock in active]
        outstanding = len(active)
        past_timeout = sum(
            timed_out or samples[id(clock)] >= deadline
            for _, _, deadline, timed_out, _, clock in active
        )
        oldest = max(ages, default=0.0)
        capacity = min([self.batch_size, *(entry[4] for entry in active)])
        saturated = (
            not retired
            and outstanding > 0
            and (bool(result["saturated"]) or outstanding >= capacity)
        )
        if saturated:
            result["saturated"] = 1
        result["outstanding"] = outstanding
        result["past_timeout"] = past_timeout
        result["oldest_age_seconds"] = oldest

        with _workers_lock:
            current = [worker for key, worker in _workers.items() if key[:2] == scope]
            current_warning = _saturation_warnings.get(scope)
            same_workers = [entry[0] for entry in active] == current
            same_warning = current_warning is previous and (
                previous_state is None
                or (
                    previous.clock is previous_state[0]
                    and previous.last_at == previous_state[1]
                    and previous.threshold == previous_state[2]
                    and previous.version == previous_state[3]
                )
            )
            if retired and same_workers and same_warning:
                _saturation_warnings.pop(scope, None)
            elif not retired and same_workers and same_warning:
                if saturated:
                    threshold = min(outstanding, capacity)
                    if previous is None:
                        _saturation_warnings[scope] = _SaturationWarning(
                            self.clock, samples[id(self.clock)], threshold
                        )
                        warn = True
                    else:
                        # The opening clock keeps the interval consistent even
                        # if reconcilers use different injected clocks.
                        previous.version += 1
                        now = samples[id(previous.clock)]
                        if now - previous.last_at >= SATURATION_WARNING_INTERVAL_SECONDS:
                            previous.last_at = now
                            warn = True
                        previous.threshold = min(previous.threshold, threshold)
                elif previous is not None and outstanding < previous.threshold:
                    del _saturation_warnings[scope]
        if warn:
            store_id = hashlib.sha256(store.encode("utf-8")).hexdigest()[:16]
            logger.warning(
                "Terminal delivery saturated store=%s consumer=%s outstanding=%d "
                "past_timeout=%d oldest_age_seconds=%.3f",
                store_id,
                self.consumer,
                outstanding,
                past_timeout,
                oldest,
            )

    async def _invoke(
        self, prepared: PreparedDelivery, transition_id: str, db: Any
    ) -> tuple[DeliveryOutcome, _Worker | None]:
        call = prepared.call
        assert self._store_identity is not None
        worker = _reserve_worker(
            self._store_identity,
            db,
            self.consumer,
            transition_id,
            self.batch_size,
            started_at=self.clock(),
            timeout_seconds=self.timeout,
            clock=self.clock,
        )
        if worker is None:
            raise _WorkerBusyError
        if inspect.iscoroutinefunction(call):
            try:
                return await _await_reserved_hook(worker, call(), timeout=self.timeout)
            except asyncio.CancelledError:
                # The helper detached the still-running task before propagating.
                raise
            except BaseException:
                _release_worker(worker)
                raise
        try:
            threading.Thread(target=_run_worker, args=(worker, call), daemon=True).start()
        except BaseException:
            _release_worker(worker)
            raise
        try:
            result = await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(worker.future)), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            with _workers_lock:
                worker.timed_out = True
            _detach_worker(worker)
            raise
        except asyncio.CancelledError:
            _detach_worker(worker)
            raise
        except BaseException:
            _release_worker(worker)
            raise
        if inspect.isawaitable(result):
            deadline = self.clock() + self.timeout
            with _workers_lock:
                worker.deadline = deadline
            try:
                return await _await_reserved_hook(worker, result, timeout=self.timeout)
            except asyncio.CancelledError:
                # The helper detached the still-running task before propagating.
                raise
            except BaseException:
                _release_worker(worker)
                raise
        return result, worker

    async def run_pass(self, db: Any) -> dict[str, int | float]:
        """Attempt bounded pages until the traversal wraps or the pass budget is spent.

        Only explicit outcomes are acknowledged.
        """
        async with self._lock:
            started = self.clock()
            store = _store_identity(db)
            if self._store_identity is None:
                self._store_identity = store
            elif self._store_identity != store:
                raise ValueError("a reconciler instance belongs to one state store")
            # Closed-loop completions and completed private-memory ACKs must be
            # reconciled even if this consumer was retired while work ran.
            _release_closed_loop_workers(store, self.consumer)
            await _drain_deferred(db, self.consumer, self._store_identity)
            try:
                registration = await register_consumer(db, self.consumer, start=self.start)
                counts = registration.counts
                retired = False
            except ConsumerRetiredError:
                retired = True
                counts = await eligibility_counts(db)
            result = {
                "attempted": 0,
                "acknowledged": 0,
                "not_applicable": 0,
                "no_session": 0,
                "ambiguous_project": 0,
                "failed": 0,
                "saturated": 0,
                "pages": 0,
                "past_ten_failures": sum(x.attempts > 10 for x in self._backoff.values()),
                "unclassified": counts.initial_null_count,
                "post_migration_null": counts.post_migration_null_count,
            }
            if retired:
                self._report_capacity(store, result, retired=True)
                return result
            while True:
                pending_ids, pending_count = self._pending()
                if pending_count >= self.batch_size:
                    result["saturated"] = 1
                    break
                now = self.clock()
                excluded = {key for key, entry in self._backoff.items() if entry.retry_at > now}
                excluded.update(pending_ids)
                rows = await reconcile_unacknowledged(
                    db,
                    self.consumer,
                    after=self._cursor,
                    exclude_ids=excluded,
                    limit=self.batch_size,
                )
                if not rows:
                    self._cursor = None
                    break
                result["pages"] += 1
                for row in rows:
                    transition_id = row["transition_id"]
                    pending_ids, pending_count = self._pending()
                    if pending_count >= self.batch_size:
                        result["saturated"] = 1
                        break
                    if transition_id in pending_ids:
                        continue
                    previous_cursor = self._cursor
                    self._cursor = (float(row["occurred_at"]), transition_id)
                    worker = None
                    try:
                        prepared = await asyncio.wait_for(
                            self.prepare(row, db), timeout=self.timeout
                        )
                        if not await _consumer_is_active(db, self.consumer):
                            self._cursor = previous_cursor
                            break
                        result["attempted"] += 1
                        if isinstance(prepared, PreparedDelivery):
                            outcome, worker = await self._invoke(prepared, transition_id, db)
                        else:
                            outcome = prepared
                        if outcome in _ACKNOWLEDGE_OUTCOMES:
                            await ack_delivery(db, transition_id, self.consumer)
                            self._backoff.pop(transition_id, None)
                            result["acknowledged"] += 1
                            if outcome is not DeliveryOutcome.SUCCESS:
                                result["not_applicable"] += 1
                            if outcome is DeliveryOutcome.NO_SESSION:
                                result["no_session"] += 1
                            elif outcome is DeliveryOutcome.AMBIGUOUS_PROJECT:
                                result["ambiguous_project"] += 1
                            continue
                    except _WorkerBusyError:
                        result["attempted"] -= 1
                        result["saturated"] = 1
                        self._cursor = previous_cursor
                        break
                    except asyncio.CancelledError:
                        # A cancelled pass can leave a resistant hook occupying
                        # capacity even when no later pass is scheduled.
                        self._report_capacity(store, result)
                        raise
                    except Exception:
                        logger.exception("Terminal delivery failed for %s", transition_id)
                    finally:
                        if worker is not None:
                            _release_worker(worker)
                    self._fail(transition_id)
                    result["failed"] += 1
                else:
                    # The page ran to its end without a stop; read the next one while
                    # the budget lasts.
                    if self.clock() - started < self.pass_budget:
                        continue
                break
            result["past_ten_failures"] = sum(x.attempts > 10 for x in self._backoff.values())
            self._report_capacity(store, result)
            return result
