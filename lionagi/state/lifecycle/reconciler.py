# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Bounded, at-least-once delivery of persisted terminal transitions."""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import json
import logging
import math
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
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
    get_owed_transition,
    is_acknowledged,
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

_STRICT_ACKNOWLEDGE_OUTCOMES = frozenset({DeliveryOutcome.SUCCESS, DeliveryOutcome.NOT_APPLICABLE})


def _observe_cancelled_hook(task: asyncio.Future[Any]) -> None:
    """Consume a late hook result after timeout or enclosing cancellation."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.warning("terminal delivery hook failed after cancellation", exc_info=True)


def _observe_durable_task(task: asyncio.Future[Any], transition_id: str, label: str) -> None:
    """Observe detached work without logging exception messages or tracebacks."""
    try:
        task.result()
    except asyncio.CancelledError:
        if label == "ACK":
            logger.warning("Detached ACK cancelled for %s", transition_id)
    except Exception as exc:
        logger.warning("Detached %s failed for %s: %s", label, transition_id, type(exc).__name__)
    else:
        if label == "ACK":
            logger.info("Detached ACK completed for %s", transition_id)


async def _await_hook(
    awaitable: Awaitable[Any],
    timeout: float,
    admission: _AttemptAdmission | None = None,
    *,
    safe_transition_id: str | None = None,
    late_ack: Callable[[], Coroutine[Any, Any, None]] | None = None,
) -> Any:
    """Treat a hook's own cancellation as failure, but propagate parent cancellation."""
    task = asyncio.ensure_future(awaitable)

    def retain_hook() -> None:
        if admission is not None:
            if late_ack is not None:

                def acknowledge_late(done: asyncio.Future[Any]) -> None:
                    try:
                        outcome = done.result()
                    except BaseException:
                        return
                    if not isinstance(outcome, DeliveryOutcome) or (
                        outcome not in _STRICT_ACKNOWLEDGE_OUTCOMES
                    ):
                        return
                    coroutine = None
                    try:
                        coroutine = late_ack()
                        acknowledgement = asyncio.create_task(coroutine)
                    except BaseException as exc:
                        if coroutine is not None:
                            coroutine.close()
                        logger.warning(
                            "Could not schedule late ACK for %s: %s",
                            admission.key[2],
                            type(exc).__name__,
                        )
                        return
                    admission.retain(acknowledgement, label="ACK")

                # This callback must retain the ACK before the hook's retention
                # callback releases the last admission reference.
                task.add_done_callback(acknowledge_late)
            admission.retain(task)
        elif safe_transition_id is not None:
            task.add_done_callback(
                lambda done: _observe_durable_task(done, safe_transition_id, "hook")
            )
        else:
            task.add_done_callback(_observe_cancelled_hook)

    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        retain_hook()
        task.cancel()
        raise
    if not done:
        retain_hook()
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
    url: str
    dialect: str
    keeper: sqlite3.Connection | None = None
    detached: bool = False
    finished: bool = False
    late_started: bool = False
    deferred: bool = False
    strict_outcomes: bool = False


_workers_lock = threading.Lock()
_workers: dict[tuple[str, str, str], _Worker] = {}
_attempts: dict[tuple[str, str, str], _AttemptAdmission] = {}


@dataclass(eq=False)
class _AttemptAdmission:
    """Keep an event reserved until its owner and detached async work settle."""

    key: tuple[str, str, str]
    limit: int
    db: Any
    loop: asyncio.AbstractEventLoop
    engine: Any
    url: str
    dialect: str
    keeper: sqlite3.Connection | None = None
    owner: bool = True
    pending: int = 0

    def retain(
        self,
        task: asyncio.Future[Any] | concurrent.futures.Future[Any],
        *,
        label: str = "hook",
        safe: bool = True,
        observe: bool = True,
    ) -> None:
        with _workers_lock:
            self.pending += 1

        def settled(done: Any) -> None:
            try:
                if observe and safe:
                    _observe_durable_task(done, self.key[2], label)
                elif observe:
                    _observe_cancelled_hook(done)
            finally:
                keeper = None
                with _workers_lock:
                    self.pending -= 1
                    if not self.owner and self.pending == 0 and _attempts.get(self.key) is self:
                        del _attempts[self.key]
                        keeper, self.keeper = self.keeper, None
                if keeper is not None:
                    keeper.close()

        task.add_done_callback(settled)

    def release(self) -> None:
        keeper = None
        with _workers_lock:
            self.owner = False
            if self.pending == 0 and _attempts.get(self.key) is self:
                del _attempts[self.key]
                keeper, self.keeper = self.keeper, None
        if keeper is not None:
            keeper.close()


def _open_shared_keeper(store: str, db: Any) -> sqlite3.Connection | None:
    if not store.startswith("sqlite-memory:shared:"):
        return None
    (filename,), options = SQLiteDialect_aiosqlite().create_connect_args(make_url(db.url))
    keeper = sqlite3.connect(filename, uri=bool(options.get("uri")), check_same_thread=False)
    try:
        keeper.execute("PRAGMA foreign_keys = ON")
    except BaseException:
        keeper.close()
        raise
    return keeper


def _reserve_attempt(
    store: str, consumer: str, transition_id: str, limit: int, db: Any
) -> _AttemptAdmission | None:
    key = (store, consumer, transition_id)
    with _workers_lock:
        if key in _attempts or key in _workers:
            return None
        workers = [worker for k, worker in _workers.items() if k[:2] == key[:2]]
        attempts = [item for k, item in _attempts.items() if k[:2] == key[:2]]
        active_keys = {item.key for item in (*workers, *attempts)}
        capacity = min([limit, *(item.limit for item in (*workers, *attempts))])
        if len(active_keys) >= capacity:
            return None
        keeper = _open_shared_keeper(store, db)
        admission = _AttemptAdmission(
            key, limit, db, asyncio.get_running_loop(), db._engine, db.url, db.dialect, keeper
        )
        _attempts[key] = admission
        return admission


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


def _readonly_delivery_store(db: Any) -> bool:
    """Refuse stores whose handle or SQLite URI forbids an ACK write."""
    if getattr(db, "readonly", False):
        return True
    if getattr(db, "dialect", None) != "sqlite":
        return False
    (filename,), options = SQLiteDialect_aiosqlite().create_connect_args(make_url(db.url))
    if not options.get("uri") or not filename.startswith("file:"):
        return False
    params = dict(parse_qsl(urlsplit(filename).query, keep_blank_values=True))
    return params.get("mode") == "ro" or params.get("immutable", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _reserve_worker(
    store: str,
    db: Any,
    consumer: str,
    transition_id: str,
    limit: int,
    *,
    strict_outcomes: bool = False,
    admission: _AttemptAdmission | None = None,
) -> _Worker | None:
    key = (store, consumer, transition_id)
    with _workers_lock:
        active = [worker for k, worker in _workers.items() if k[:2] == key[:2]]
        attempts = [item for k, item in _attempts.items() if k[:2] == key[:2]]
        own = _attempts.get(key)
        if key in _workers or (own is not None and own is not admission):
            return None
        active_keys = {item.key for item in (*active, *attempts)}
        capacity = min([limit, *(item.limit for item in (*active, *attempts))])
        if len(active_keys) > capacity:
            return None
        if key not in active_keys and len(active_keys) >= capacity:
            return None
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        # A loop closing can cancel its asyncio wrapper, but must never cancel
        # the underlying record for a thread that is still executing.
        future.set_running_or_notify_cancel()
        # Pin shared-memory generations until the retained work settles.
        keeper = _open_shared_keeper(store, db)
        worker = _Worker(
            key,
            db,
            asyncio.get_running_loop(),
            future,
            limit,
            db._engine,
            db.url,
            db.dialect,
            keeper,
            strict_outcomes=strict_outcomes,
        )
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


def _keeper_ack(worker: _Worker | _AttemptAdmission) -> None:
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


class _LateAckDeferredError(RuntimeError):
    """The loop owning a private memory store is not available."""


def _existing_sqlite_rw_url(url: str) -> str:
    """Preserve SQLite URL options while refusing file creation on open."""
    parsed = make_url(url)
    (filename,), options = SQLiteDialect_aiosqlite().create_connect_args(parsed)
    query = dict(parsed.query)
    if query.get("mode") == "ro":
        raise PermissionError("read-only SQLite store cannot accept a late acknowledgement")
    if options.get("uri") and filename.startswith("file:"):
        location = parsed.database
    else:
        location = Path(filename).absolute().as_uri()
    query.update({"mode": "rw", "uri": "true"})
    return parsed.set(database=location, query=query).render_as_string(hide_password=False)


async def _acknowledge_late(target: _Worker | _AttemptAdmission) -> None:
    """Acknowledge against the captured store without initializing its schema."""
    expected = target.key[0]
    if expected.startswith("sqlite-memory:shared:"):
        # The keeper retains this exact generation after the caller closes.
        if isinstance(target, _Worker):
            _keeper_ack(target)
        else:
            future: concurrent.futures.Future[None] = concurrent.futures.Future()
            future.set_running_or_notify_cancel()
            target.retain(future, label="keeper", observe=False)

            def write_keeper() -> None:
                try:
                    _keeper_ack(target)
                except BaseException as exc:
                    if not isinstance(exc, Exception):
                        exc = RuntimeError(f"keeper acknowledgement raised {type(exc).__name__}")
                    future.set_exception(exc)
                else:
                    future.set_result(None)

            try:
                threading.Thread(target=write_keeper, daemon=True).start()
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    exc = RuntimeError(f"keeper worker start raised {type(exc).__name__}")
                future.set_exception(exc)
            wrapped = asyncio.wrap_future(future)
            # The ACK waiter can be cancelled while the non-cancellable keeper
            # worker is still running. Consume its eventual result separately.
            wrapped.add_done_callback(
                lambda done: _observe_durable_task(done, target.key[2], "keeper")
            )
            await asyncio.shield(wrapped)
        return
    if expected.startswith("sqlite-memory:"):
        if target.db._engine is not target.engine or target.db._engine is None:
            raise _MemoryStoreChangedError

        async def acknowledge_private() -> None:
            async with target.db._holding_open_lock():
                if target.db._engine is not target.engine:
                    raise _MemoryStoreChangedError
                await ack_delivery(target.db, target.key[2], target.key[1])

        if asyncio.get_running_loop() is target.loop:
            await acknowledge_private()
            return
        if not target.loop.is_running():
            raise _LateAckDeferredError
        coroutine = acknowledge_private()
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, target.loop)
        except RuntimeError:
            coroutine.close()
            raise _LateAckDeferredError from None
        await asyncio.wait_for(asyncio.wrap_future(future), timeout=HANDLER_BUDGET_SECONDS)
        return

    from lionagi.state.db import StateDB, _install_begin_immediate
    from lionagi.state.engine import make_engine

    db = StateDB(url=target.url)
    if target.dialect == "sqlite" and _store_identity(db) != expected:
        raise RuntimeError("SQLite store identity changed before late acknowledgement")

    def check_opened(dbapi_conn) -> None:
        """Detect a one-way path replacement, but not an ABA path swap."""
        cursor = dbapi_conn.cursor()
        try:
            cursor.execute("PRAGMA database_list")
            opened = next((row[2] for row in cursor.fetchall() if row[1] == "main"), "")
            stat = os.stat(opened)
            if f"sqlite-inode:{stat.st_dev}:{stat.st_ino}" != expected:
                raise RuntimeError("SQLite store identity changed during late acknowledgement")
        finally:
            cursor.close()

    # StateDB.open() applies schema; the retained operation must not initialize it.
    engine_url = _existing_sqlite_rw_url(db.url) if db.dialect == "sqlite" else db.url
    db._engine = make_engine(
        engine_url, before_settings=check_opened if db.dialect == "sqlite" else None
    )
    if db.dialect == "sqlite":
        _install_begin_immediate(db._engine.sync_engine)
    try:
        await ack_delivery(db, target.key[2], target.key[1])
    finally:
        await db.close()


def _finish_late(worker: _Worker) -> None:
    """A detached worker may finish after the event loop that started it is gone."""
    try:
        result = worker.future.result()
        if inspect.isawaitable(result):

            async def await_result() -> Any:
                return await _await_hook(
                    result,
                    timeout=HANDLER_BUDGET_SECONDS,
                    safe_transition_id=worker.key[2] if worker.strict_outcomes else None,
                )

            result = asyncio.run(await_result())
        accepted_outcomes = (
            _STRICT_ACKNOWLEDGE_OUTCOMES if worker.strict_outcomes else _ACKNOWLEDGE_OUTCOMES
        )
        if result not in accepted_outcomes:
            logger.warning("Late terminal delivery failed for %s", worker.key[2])
            _release_worker(worker)
            return
        try:
            asyncio.run(_acknowledge_late(worker))
        except _MemoryStoreChangedError:
            logger.warning("Private memory store closed before late ACK for %s", worker.key[2])
        except Exception as exc:
            if worker.key[0].startswith("sqlite-memory:"):
                if worker.strict_outcomes:
                    logger.warning(
                        "Late ACK deferred for %s: %s", worker.key[2], type(exc).__name__
                    )
                else:
                    logger.exception("Late memory acknowledgement deferred for %s", worker.key[2])
                with _workers_lock:
                    worker.deferred = True
                return
            raise
    except Exception as exc:
        if worker.strict_outcomes:
            logger.warning("Late delivery failed for %s: %s", worker.key[2], type(exc).__name__)
        else:
            logger.exception(
                "Late terminal delivery or acknowledgement failed for %s", worker.key[2]
            )
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


@dataclass(frozen=True, slots=True)
class AttemptResult:
    """Result of one exact-event attempt, separate from registration."""

    status: Literal["acknowledged", "failed", "uncertain", "busy", "not_owed"]
    outcome: DeliveryOutcome | None = None
    attempted: bool = False


class TerminalReconciler:
    """A process-local retry cursor; independent processes may deliver the same fact.

    The cursor rotates through eligible pending rows. Backoff and attempts are
    bounded, best effort, and reset on eviction or restart. ``start`` is the
    baseline the consumer registers with on first use. A pass reads pages of
    ``batch_size`` rows until the traversal wraps or ``pass_budget`` seconds have
    passed; the budget is checked between pages, so the default of zero attempts
    one page per pass. ``prepare`` must be effect-free: it may inspect data and
    construct a delivery, but must not invoke the delivery effect itself.
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
        with _workers_lock:
            keys = {key[2] for key in _workers if key[:2] == (self._store_identity, self.consumer)}
            keys.update(
                key[2] for key in _attempts if key[:2] == (self._store_identity, self.consumer)
            )
        return keys, len(keys)

    async def _invoke(
        self,
        prepared: PreparedDelivery,
        transition_id: str,
        db: Any,
        admission: _AttemptAdmission | None = None,
        *,
        strict_outcomes: bool = False,
    ) -> tuple[DeliveryOutcome, _Worker | None]:
        call = prepared.call

        async def acknowledge_late() -> None:
            assert admission is not None
            await _acknowledge_late(admission)

        late_ack = acknowledge_late if strict_outcomes and admission is not None else None
        if inspect.iscoroutinefunction(call):
            return await _await_hook(
                call(), timeout=self.timeout, admission=admission, late_ack=late_ack
            ), None
        assert self._store_identity is not None
        worker = _reserve_worker(
            self._store_identity,
            db,
            self.consumer,
            transition_id,
            self.batch_size,
            strict_outcomes=strict_outcomes,
            admission=admission,
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
                result = await _await_hook(
                    result, timeout=self.timeout, admission=admission, late_ack=late_ack
                )
            except BaseException:
                _release_worker(worker)
                raise
        return result, worker

    async def _attempt_one(
        self,
        db: Any,
        transition_id: str,
    ) -> AttemptResult:
        """Attempt one owed event under the shared exact/replay adapter admission."""
        if _readonly_delivery_store(db):
            logger.warning("Terminal delivery refused for %s: read-only state store", transition_id)
            return AttemptResult("failed", attempted=False)
        store = _store_identity(db)
        if self._store_identity is None:
            self._store_identity = store
        elif self._store_identity != store:
            raise ValueError("a reconciler instance belongs to one state store")
        try:
            admission = _reserve_attempt(store, self.consumer, transition_id, self.batch_size, db)
        except Exception as exc:  # noqa: BLE001 -- no effect started
            logger.warning(
                "Terminal admission failed for %s: %s", transition_id, type(exc).__name__
            )
            return AttemptResult("failed")
        if admission is None:
            return AttemptResult("busy")
        started = False
        ack_uncertain = False
        observed_outcome: DeliveryOutcome | None = None

        async def perform() -> AttemptResult:
            nonlocal started, ack_uncertain, observed_outcome
            row = await get_owed_transition(db, self.consumer, transition_id)
            if row is None:
                return AttemptResult("not_owed")
            preparation = asyncio.ensure_future(self.prepare(row, db))
            try:
                prepared = await asyncio.wait_for(asyncio.shield(preparation), timeout=self.timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                admission.retain(preparation)
                preparation.cancel()
                raise
            # Another process may have acknowledged or retired while preparation ran.
            if await get_owed_transition(db, self.consumer, transition_id) is None:
                return AttemptResult("not_owed")
            # This is the final active check; a later retirement permits this
            # already-admitted effect and its ACK to finish.
            if not await _consumer_is_active(db, self.consumer):
                return AttemptResult("not_owed")
            started = True
            worker = None
            try:
                if isinstance(prepared, PreparedDelivery):
                    outcome, worker = await self._invoke(
                        prepared, transition_id, db, admission, strict_outcomes=True
                    )
                else:
                    outcome = prepared
                accepted = outcome in _STRICT_ACKNOWLEDGE_OUTCOMES
                if not accepted:
                    return AttemptResult("failed", outcome, attempted=True)
                observed_outcome = outcome
                ack_uncertain = True
                acknowledgement = asyncio.create_task(
                    ack_delivery(db, transition_id, self.consumer)
                )
                try:
                    await asyncio.shield(acknowledgement)
                except asyncio.CancelledError:
                    # The transaction might have committed; prevent another
                    # same-process attempt until its actual result is observed.
                    admission.retain(acknowledgement, label="ACK")
                    raise
                except Exception:
                    # A failed response can follow a successful commit. Resolve
                    # against the durable ledger before reporting a retry debt.
                    try:
                        acknowledged = await is_acknowledged(db, transition_id, self.consumer)
                    except Exception:
                        return AttemptResult("uncertain", outcome, attempted=True)
                    if not acknowledged:
                        ack_uncertain = False
                        return AttemptResult("failed", outcome, attempted=True)
                ack_uncertain = False
                return AttemptResult("acknowledged", outcome, attempted=True)
            finally:
                if worker is not None:
                    _release_worker(worker)

        try:
            return await asyncio.wait_for(perform(), timeout=self.timeout)
        except _WorkerBusyError:
            return AttemptResult("busy")
        except asyncio.TimeoutError:
            if ack_uncertain:
                return AttemptResult("uncertain", observed_outcome, attempted=True)
            return AttemptResult("failed", attempted=started)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Terminal delivery failed for %s: %s", transition_id, type(exc).__name__)
            return AttemptResult("failed", attempted=started)
        finally:
            admission.release()

    async def attempt_exact(self, db: Any, transition_id: str) -> AttemptResult:
        """Attempt exactly one existing event without enrolling a consumer.

        The caller owns routing and the surrounding post-commit budget. This
        method is not connected to the legacy callback path by itself.
        """
        return await self._attempt_one(db, transition_id)

    async def attempt_replay(self, db: Any, transition_id: str) -> AttemptResult:
        """Unwired replay adapter for the same exact-event attempt contract."""
        return await self._attempt_one(db, transition_id)

    async def run_pass(self, db: Any) -> dict[str, int]:
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
            return result
