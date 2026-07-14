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
handler has had a chance to run. A handler-raised ``CancelledError`` cannot be
nested in ``ExceptionGroup``, so it is surfaced as the distinct
``SchedulerHandlerCancelled`` marker. The mint call site (``engine.py``)
records either form. Cancellation of the emitter task remains a plain
``CancelledError`` and propagates, so a broken handler is visible without
stopping unrelated schedules or swallowing scheduler shutdown.
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


@dataclass
class _RunSignalCounters:
    """Per-``run_id`` coordination counters, accumulated across every signal
    :meth:`SchedulerSignalBus.emit` dispatches for that run.

    ``emitted`` is a signal-type-name histogram: today's mint site fires
    exactly one terminal ``ScheduleRun*`` signal per run (see
    ``engine.py._fire_inner``), so in practice every value is 1, but the
    histogram shape survives a future run minting more than one signal
    without a counter-shape change. ``received`` counts deliveries where a
    subscription's types AND predicates both matched (regardless of what
    the handler returned or whether it raised); ``acted_on`` counts only
    the subset where the handler additionally returned a truthy "acted"
    marker — the opt-in convention that keeps this measure-only (no
    handler is required to participate; a non-participating handler simply
    contributes to ``received``, never to ``acted_on``).
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
    storage, no ``route()``/``stream()``, no DB auto-persistence; the
    ``schedule_runs`` table is already the durable record for what these
    signals describe. Matching is ``isinstance``-based against any ``type``
    key, AND-composed with any callable predicate keys (e.g. filtering on
    ``reason_code``) — no topic/pattern machinery.

    Also accumulates per-``run_id`` coordination counters (signals
    emitted/received/acted-on — see :class:`_RunSignalCounters`) so a
    caller can report them at that run's finalize via
    :meth:`pop_run_counters`. The bus itself is the least invasive place to
    keep them: it already sees every signal and every handler dispatch, and
    it requires no change to the handler API (``observe``/the ``Handler``
    signature are untouched).
    """

    def __init__(self) -> None:
        self._subs: list[tuple[tuple[type, ...], tuple[Predicate, ...], Handler]] = []
        self._counters: dict[str, _RunSignalCounters] = {}

    def pop_run_counters(self, run_id: str) -> dict[str, Any] | None:
        """Remove and return *run_id*'s accumulated signal counters as a
        plain dict, or ``None`` if no signal was ever emitted for it.

        Pop, not peek: the bus is a long-lived per-daemon singleton (one
        per :class:`SchedulerEngine`, spanning every schedule it ever
        fires), so counters must not accumulate for the process's whole
        lifetime past the one terminal flush each run_id gets (see
        ``lionagi.studio.services.scheduler_state.flush_run_telemetry``,
        the intended sole caller).
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
        ``ExceptionGroup`` cannot nest a bare ``BaseException``. A handler
        cancellation is therefore re-raised as ``SchedulerHandlerCancelled``
        so the mint site can record it without confusing it with cancellation
        of the task that is running ``emit``.
        """
        candidates = [entry for entry in self._subs if not entry[0] or isinstance(signal, entry[0])]

        # Emitted counts regardless of whether any handler is even
        # listening -- it describes what the mint site dispatched, not
        # what got delivered (that's `received`, below).
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
            # Received: candidate matched (isinstance, above) AND every
            # predicate passed -- counted before invocation so a handler
            # that raises still counts as delivered, just not acted-on.
            if run_id:
                self._counters[run_id].received += 1
            out = handler(signal)
            if inspect.isawaitable(out):
                out = await out
            # Acted-on: the opt-in truthy-return convention (see
            # _RunSignalCounters docstring) -- a non-participating handler
            # returning None/falsy stays received-only.
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

    Production wiring calls this once at daemon startup (see the module-level
    ``scheduler`` singleton in ``engine.py``). The one registered handler
    today is a log-only proof of the API, not a product feature — this
    function is the single place future default handlers get added.
    """
    bus.observe(ScheduleRunFailed, handler=_log_schedule_run_failed)


async def record_handler_failure(exc_group: BaseException, signal: Signal) -> None:
    """Write a durable ``admin_events`` row describing a handler-dispatch failure.

    Called by the mint call site after :meth:`SchedulerSignalBus.emit` raises
    an :class:`ExceptionGroup` or handler-originated cancellation — the schedule_run/invocation row is already
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
