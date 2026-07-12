# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Typed schedule_run outcome signals and a handler registry for the scheduler daemon.

Mint site: ``SchedulerEngine._fire_inner()``, immediately after each of the
three ``_guarded_terminal_status("schedule_run", ...)`` calls returns
``True`` — the one choke point every scheduled run's terminal write already
passes through (in-process, synchronous with the commit, no polling
latency). This module stays agnostic about *where* that mint happens: the
signal classes and :class:`SchedulerSignalBus` only need the same
status/reason_code/entity_id fields a generic post-commit hook on
``LifecycleService.transition()`` would eventually carry, so promoting the
mint site later is a call-site move, not a redesign of this module.

``LifecycleService`` and ``StateDB`` schema are untouched by this module —
it is scheduler-local, imitating the shape of the existing in-run DAG
signal bus (``lionagi.session.signal`` / ``lionagi.session.observer``)
without reusing its Flow/route/stream machinery, none of which a scheduler
daemon process needs (``schedule_runs`` is already the durable record).

Failure semantics: :meth:`SchedulerSignalBus.emit` never swallows a handler
exception. Handlers run concurrently with ``return_exceptions=True``; any
failures are raised together as an :class:`ExceptionGroup` after every
handler has had a chance to run. The mint call site (``engine.py``) catches
that group, writes a durable ``admin_events`` row describing the failure,
and lets the tick loop continue — a broken handler must never be invisible
and must never stop unrelated schedules from firing.
"""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Callable
from typing import Any

from lionagi.ln.concurrency import ExceptionGroup, gather
from lionagi.session.signal import Signal

__all__ = (
    "ScheduleRunSucceeded",
    "ScheduleRunFailed",
    "ScheduleRunCancelled",
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
    """Mint the ``ScheduleRun*`` signal matching *new_status*.

    ``entity_id``, ``new_status``, and ``reason_code`` are exactly the
    fields every ``_guarded_terminal_status()`` caller already has in hand
    (the same fields a generic transition post-commit hook would receive);
    ``schedule_id``/``action_kind``/``chain_depth``/``trigger_context`` are
    scheduler-local enrichment the call site supplies today from its own
    locals, not from the transition outcome itself.
    """
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


class SchedulerSignalBus:
    """Stripped-down sibling of :class:`~lionagi.session.observer.SessionObserver`.

    Only ``observe``/``unobserve``/``emit`` — no ``Flow``/``Progression``
    storage, no ``route()``/``stream()``, no DB auto-persistence; the
    ``schedule_runs`` table is already the durable record for what these
    signals describe. Matching is ``isinstance``-based against any ``type``
    key, AND-composed with any callable predicate keys (e.g. filtering on
    ``reason_code``) — no topic/pattern machinery.
    """

    def __init__(self) -> None:
        self._subs: list[tuple[tuple[type, ...], tuple[Predicate, ...], Handler]] = []

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

        Type matching (``isinstance`` against a subscription's registered
        types) happens up front to select candidate subscriptions — it is
        cheap and cannot raise. Predicate matching happens *inside* the same
        protected region as handler invocation, gathered concurrently with
        ``return_exceptions=True``: a predicate that raises becomes a
        collected dispatch failure exactly like a handler exception, rather
        than aborting ``emit()`` before sibling subscriptions ever run. Any
        failures are raised together as one :class:`ExceptionGroup` once
        every candidate has had a chance to run — never a blanket
        ``except Exception: pass``.

        ``asyncio.CancelledError`` (and any other non-``Exception``
        ``BaseException``) is excluded from that group — the stdlib
        ``ExceptionGroup`` cannot nest a bare ``BaseException`` — and is
        re-raised directly instead, since cancellation must propagate and is
        not a handler bug to record.
        """
        candidates = [entry for entry in self._subs if not entry[0] or isinstance(signal, entry[0])]
        if not candidates:
            return []

        async def _invoke(
            entry: tuple[tuple[type, ...], tuple[Predicate, ...], Handler],
        ) -> Any:
            _types, predicates, handler = entry
            if not all(pred(signal) for pred in predicates):
                return _NO_MATCH
            out = handler(signal)
            if inspect.isawaitable(out):
                out = await out
            return out

        raw = await gather(*(_invoke(entry) for entry in candidates), return_exceptions=True)

        cancellations = [
            r for r in raw if isinstance(r, BaseException) and not isinstance(r, Exception)
        ]
        errors = [r for r in raw if isinstance(r, Exception)]
        results = [r for r in raw if r is not _NO_MATCH and not isinstance(r, BaseException)]

        if cancellations:
            cancellation = cancellations[0]
            if errors:
                raise cancellation from ExceptionGroup(
                    f"{len(errors)} scheduler signal handler(s) failed for {type(signal).__name__}",
                    errors,
                )
            raise cancellation
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

    Production wiring calls this once at daemon startup (see the module-level
    ``scheduler`` singleton in ``engine.py``). The one registered handler
    today is a log-only proof of the API, not a product feature — this
    function is the single place future default handlers get added.
    """
    bus.observe(ScheduleRunFailed, handler=_log_schedule_run_failed)


async def record_handler_failure(exc_group: BaseException, signal: Signal) -> None:
    """Write a durable ``admin_events`` row describing a handler-dispatch failure.

    Called by the mint call site after :meth:`SchedulerSignalBus.emit` raises
    an :class:`ExceptionGroup` — the schedule_run/invocation row is already
    committed by the time this runs, so a failure here never corrupts that
    write; it only means the diagnostic record itself couldn't be persisted,
    which is logged and swallowed the same way ``bind_db_persistence``'s
    best-effort persistence is (session/observer.py) — a narrow, deliberate
    exception for exactly this one built-in recorder, not the general
    handler-dispatch contract above.
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
