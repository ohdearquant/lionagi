# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Bounded, at-least-once delivery of persisted terminal transitions."""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import logging
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any
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


def _observe_cancelled_hook(task: asyncio.Future[Any]) -> None:
    """Consume a late hook result after timeout or enclosing cancellation."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.warning("terminal delivery hook failed after cancellation", exc_info=True)


async def _await_hook(awaitable: Awaitable[Any], timeout: float) -> Any:
    """Treat a hook's own cancellation as failure, but propagate parent cancellation."""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        task.add_done_callback(_observe_cancelled_hook)
        task.cancel()
        raise
    if not done:
        task.add_done_callback(_observe_cancelled_hook)
        task.cancel()
        return DeliveryOutcome.FAILURE
    try:
        return task.result()
    except asyncio.CancelledError:
        return DeliveryOutcome.FAILURE


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
    """One process-wide synchronous delivery reservation, independent of an event loop."""

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
    deferred: bool = False


_workers_lock = threading.Lock()
_workers: dict[tuple[str, str, str], _Worker] = {}


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
    store: str, db: Any, consumer: str, transition_id: str, limit: int
) -> _Worker | None:
    key = (store, consumer, transition_id)
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
        _workers[key] = worker
        return worker


def _release_worker(worker: _Worker) -> None:
    keeper = None
    with _workers_lock:
        if _workers.get(worker.key) is worker:
            del _workers[worker.key]
            keeper, worker.keeper = worker.keeper, None
    if keeper is not None:
        keeper.close()


class _MemoryStoreChangedError(RuntimeError):
    """A private in-memory store was closed or replaced before acknowledgement."""


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


def _finish_late(worker: _Worker) -> None:
    """A detached worker may finish after the event loop that started it is gone."""
    try:
        result = worker.future.result()
        if inspect.isawaitable(result):

            async def await_result() -> Any:
                return await _await_hook(result, timeout=HANDLER_BUDGET_SECONDS)

            result = asyncio.run(await_result())
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


def _start_late(worker: _Worker) -> None:
    with _workers_lock:
        if worker.late_started:
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
    bounded, best effort, and reset on eviction or restart.
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
    ) -> None:
        if batch_size < 1 or backoff_capacity < batch_size or backoff_capacity > BACKOFF_CAPACITY:
            raise ValueError("invalid reconciliation batch or backoff capacity")
        self.consumer = consumer
        self.prepare = prepare
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
        with _workers_lock:
            keys = {key[2] for key in _workers if key[:2] == (self._store_identity, self.consumer)}
        return keys, len(keys)

    async def _invoke(
        self, prepared: PreparedDelivery, transition_id: str, db: Any
    ) -> tuple[DeliveryOutcome, _Worker | None]:
        call = prepared.call
        if inspect.iscoroutinefunction(call):
            return await _await_hook(call(), timeout=self.timeout), None
        assert self._store_identity is not None
        worker = _reserve_worker(
            self._store_identity, db, self.consumer, transition_id, self.batch_size
        )
        if worker is None:
            raise _WorkerBusyError
        try:
            threading.Thread(target=_run_worker, args=(worker, call), daemon=True).start()
        except BaseException:
            _release_worker(worker)
            raise
        try:
            result = await asyncio.wait_for(
                asyncio.shield(asyncio.wrap_future(worker.future)), timeout=self.timeout
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _detach_worker(worker)
            raise
        except BaseException:
            _release_worker(worker)
            raise
        if inspect.isawaitable(result):
            try:
                result = await _await_hook(result, timeout=self.timeout)
            except BaseException:
                _release_worker(worker)
                raise
        return result, worker

    async def run_pass(self, db: Any) -> dict[str, int]:
        """Attempt at most one bounded page; acknowledge only explicit outcomes."""
        async with self._lock:
            store = _store_identity(db)
            if self._store_identity is None:
                self._store_identity = store
            elif self._store_identity != store:
                raise ValueError("a reconciler instance belongs to one state store")
            await _drain_deferred(db, self.consumer, self._store_identity)
            try:
                registration = await register_consumer(db, self.consumer, start="all")
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
                "past_ten_failures": sum(x.attempts > 10 for x in self._backoff.values()),
                "unclassified": counts.initial_null_count,
                "post_migration_null": counts.post_migration_null_count,
            }
            if retired:
                return result
            pending_ids, pending_count = self._pending()
            if pending_count >= self.batch_size:
                result["saturated"] = 1
                return result
            now = self.clock()
            excluded = {key for key, entry in self._backoff.items() if entry.retry_at > now}
            excluded.update(pending_ids)
            rows = await reconcile_unacknowledged(
                db, self.consumer, after=self._cursor, exclude_ids=excluded, limit=self.batch_size
            )
            if not rows:
                self._cursor = None
                return result
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
                    prepared = await asyncio.wait_for(self.prepare(row, db), timeout=self.timeout)
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
                except Exception:
                    logger.exception("Terminal delivery failed for %s", transition_id)
                finally:
                    if worker is not None:
                        _release_worker(worker)
                self._fail(transition_id)
                result["failed"] += 1
            result["past_ten_failures"] = sum(x.attempts > 10 for x in self._backoff.values())
            return result
