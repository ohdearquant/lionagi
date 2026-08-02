# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Typed schedule_run outcome signals and a handler registry for the scheduler daemon.

Scheduler-local sibling of ``lionagi.session.signal``/``observer``, without
their Flow/route/stream machinery (``schedule_runs`` is already the durable
record). See docs/internals/studio.md#lionagistudioschedulersignalspy for
the mint site and failure-propagation contract.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from lionagi.ln.concurrency import ExceptionGroup, gather
from lionagi.session.signal import Signal

__all__ = (
    "ScheduleRunSucceeded",
    "ScheduleRunFailed",
    "ScheduleRunCancelled",
    "SchedulerHandlerCancelled",
    "SchedulerSignalBus",
    "Handler",
    "Predicate",
    "build_schedule_run_signal",
    "register_default_handlers",
    "record_handler_failure",
)

_log = logging.getLogger(__name__)

Handler = Callable[[Signal], Any]
Predicate = Callable[[Signal], bool]

_NO_MATCH = object()


class SchedulerHandlerCancelled(asyncio.CancelledError):
    """Marks a ``CancelledError`` raised by a signal handler, not the emitter task."""


class ScheduleRunSucceeded(Signal):
    """A scheduled run's terminal write recorded ``completed``."""

    schedule_id: str = ""
    run_id: str = ""
    reason_code: str = ""
    action_kind: str = ""
    chain_depth: int = 0
    trigger_context: dict = {}


class ScheduleRunFailed(Signal):
    """A scheduled run's terminal write recorded ``failed``."""

    schedule_id: str = ""
    run_id: str = ""
    reason_code: str = ""
    action_kind: str = ""
    chain_depth: int = 0
    trigger_context: dict = {}
    error_detail: str = ""


class ScheduleRunCancelled(Signal):
    """A scheduled run's terminal write recorded ``cancelled``."""

    schedule_id: str = ""
    run_id: str = ""
    reason_code: str = ""
    action_kind: str = ""
    chain_depth: int = 0
    trigger_context: dict = {}


_SIGNAL_BY_STATUS: dict[str, type[Signal]] = {
    "completed": ScheduleRunSucceeded,
    "failed": ScheduleRunFailed,
    "cancelled": ScheduleRunCancelled,
}


def build_schedule_run_signal(
    *,
    entity_id: str,
    new_status: str,
    reason_code: str,
    schedule_id: str = "",
    action_kind: str = "",
    chain_depth: int = 0,
    trigger_context: dict | None = None,
    error_detail: str = "",
) -> Signal:
    """Mint the ``ScheduleRun*`` signal matching *new_status*."""
    cls = _SIGNAL_BY_STATUS.get(new_status)
    if cls is None:
        raise ValueError(f"no schedule_run signal registered for status {new_status!r}")
    kwargs: dict[str, Any] = {
        "run_id": entity_id,
        "schedule_id": schedule_id,
        "reason_code": reason_code,
        "action_kind": action_kind,
        "chain_depth": chain_depth,
        "trigger_context": trigger_context or {},
    }
    if cls is ScheduleRunFailed:
        kwargs["error_detail"] = error_detail
    return cls(**kwargs)


@dataclass
class _RunSignalCounters:
    """Per-``run_id`` coordination counters accumulated across every signal
    :meth:`SchedulerSignalBus.emit` dispatches for that run.

    ``received`` counts deliveries where type and predicates both matched;
    ``acted_on`` counts the subset where the handler additionally returned a
    truthy "acted" marker (opt-in; non-participating handlers stay
    received-only).
    """

    emitted: dict[str, int] = field(default_factory=dict)
    received: int = 0
    acted_on: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "emitted": dict(self.emitted),
            "received": self.received,
            "acted_on": self.acted_on,
        }


class SchedulerSignalBus:
    """Stripped-down sibling of :class:`~lionagi.session.observer.SessionObserver`.

    Only ``observe``/``unobserve``/``emit`` — no ``Flow``/``Progression``
    storage, no ``route()``/``stream()``, no DB auto-persistence (the
    ``schedule_runs`` table is already the durable record). Matching is
    ``isinstance`` against any ``type`` key, AND-composed with any callable
    predicate keys — no topic/pattern machinery. Also accumulates
    per-``run_id`` coordination counters (see :class:`_RunSignalCounters`),
    read back via :meth:`pop_run_counters`.
    """

    def __init__(self) -> None:
        self._subs: list[tuple[tuple[type, ...], tuple[Predicate, ...], Handler]] = []
        self._counters: dict[str, _RunSignalCounters] = {}

    def pop_run_counters(self, run_id: str) -> dict[str, Any] | None:
        """Remove and return *run_id*'s accumulated signal counters as a
        plain dict, or ``None`` if no signal was ever emitted for it.

        Pop, not peek: the bus is a long-lived per-daemon singleton, so
        counters must not accumulate past the one terminal flush each
        run_id gets (see ``services.scheduler_state.flush_run_telemetry``).
        """
        counters = self._counters.pop(run_id, None)
        return counters.to_dict() if counters is not None else None

    def observe(self, *keys: type | Predicate, handler: Handler) -> Handler:
        """Register *handler* for signals matching all *keys* (types AND predicates)."""
        types_ = tuple(k for k in keys if isinstance(k, type))
        predicates = tuple(k for k in keys if not isinstance(k, type))
        self._subs.append((types_, predicates, handler))
        return handler

    def unobserve(self, handler: Handler) -> int:
        """Remove all subscriptions for *handler*; returns the count removed."""
        before = len(self._subs)
        self._subs = [sub for sub in self._subs if sub[2] is not handler]
        return before - len(self._subs)

    async def emit(self, signal: Signal) -> list[Any]:
        """Dispatch *signal* to every matching handler.

        Type matching happens up front to select candidates; predicate
        matching happens inside the same protected region as handler
        invocation, gathered concurrently with ``return_exceptions=True`` so
        one raising predicate/handler doesn't abort dispatch to the rest.
        Failures raise together as one :class:`ExceptionGroup` once every
        candidate has run. ``CancelledError`` can't nest in an
        ``ExceptionGroup``, so a handler cancellation is re-raised as
        ``SchedulerHandlerCancelled`` instead, distinct from cancellation of
        the task running ``emit`` itself.
        """
        candidates = [entry for entry in self._subs if not entry[0] or isinstance(signal, entry[0])]

        # Counts regardless of whether any handler is listening -- describes
        # what was dispatched, not what got delivered (`received`, below).
        run_id = getattr(signal, "run_id", "") or ""
        if run_id:
            counters = self._counters.setdefault(run_id, _RunSignalCounters())
            type_name = type(signal).__name__
            counters.emitted[type_name] = counters.emitted.get(type_name, 0) + 1

        if not candidates:
            return []

        async def _invoke(
            entry: tuple[tuple[type, ...], tuple[Predicate, ...], Handler],
        ) -> Any:
            _types, predicates, handler = entry
            if not all(pred(signal) for pred in predicates):
                return _NO_MATCH
            # Counted before invocation so a raising handler still counts as
            # delivered, just not acted-on.
            if run_id:
                self._counters[run_id].received += 1
            out = handler(signal)
            if inspect.isawaitable(out):
                out = await out
            if run_id and out:
                self._counters[run_id].acted_on += 1
            return out

        raw = await gather(*(_invoke(entry) for entry in candidates), return_exceptions=True)

        cancellations = [
            r for r in raw if isinstance(r, BaseException) and not isinstance(r, Exception)
        ]
        errors = [r for r in raw if isinstance(r, Exception)]
        results = [r for r in raw if r is not _NO_MATCH and not isinstance(r, BaseException)]

        if cancellations:
            cancellation = cancellations[0]
            handler_cancelled = SchedulerHandlerCancelled(str(cancellation))
            if errors:
                raise handler_cancelled from ExceptionGroup(
                    f"{len(errors)} scheduler signal handler(s) failed for {type(signal).__name__}",
                    errors,
                )
            raise handler_cancelled from cancellation
        if errors:
            raise ExceptionGroup(
                f"{len(errors)} scheduler signal handler(s) failed for {type(signal).__name__}",
                errors,
            )
        return results


def _log_schedule_run_failed(signal: ScheduleRunFailed) -> None:
    """Worked-example default handler: log-only, proving the observe/emit contract end to end."""
    _log.warning(
        "Scheduled run failed: schedule=%s run=%s reason=%s",
        signal.schedule_id,
        signal.run_id,
        signal.reason_code,
    )


def register_default_handlers(bus: SchedulerSignalBus) -> None:
    """Register the scheduler daemon's default signal handlers on *bus*.

    Called once at daemon startup. The one handler today is a log-only proof
    of the API, not a product feature.
    """
    bus.observe(ScheduleRunFailed, handler=_log_schedule_run_failed)


async def record_handler_failure(exc_group: BaseException, signal: Signal) -> None:
    """Write a durable ``admin_events`` row describing a handler-dispatch failure.

    Called after :meth:`SchedulerSignalBus.emit` raises. The schedule_run
    row is already committed by the time this runs, so a failure here only
    means the diagnostic itself couldn't be persisted -- logged and
    swallowed, same as ``bind_db_persistence``'s best-effort write.
    """
    from lionagi.state.db import StateDB  # noqa: PLC0415

    errors = getattr(exc_group, "exceptions", (exc_group,))
    try:
        async with StateDB() as db:
            await db.insert_admin_event(
                action="scheduler_signal_handler_failed",
                target_id=getattr(signal, "run_id", None) or None,
                actor="scheduler",
                details={
                    "signal_type": type(signal).__name__,
                    "signal_id": str(signal.id),
                    "created_at": time.time(),
                    "errors": [f"{type(e).__name__}: {e}" for e in errors],
                },
            )
    except Exception:  # noqa: BLE001
        _log.exception(
            "Failed to record scheduler signal handler failure for %s (run_id=%s)",
            type(signal).__name__,
            getattr(signal, "run_id", ""),
        )
