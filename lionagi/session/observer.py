# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Reactive, typed event dispatch over a session's Flow."""

from __future__ import annotations

import inspect
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from lionagi.ln.concurrency import Lock, maybe_await
from lionagi.ln.types import Filter, RoleFilter, TypeFilter, all_of
from lionagi.protocols._concepts import Observable, Observer

from ..protocols.generic.flow import Flow
from ..protocols.generic.progression import Progression
from .signal import Signal

__all__ = ("SessionObserver", "RoleFilter", "_sanitize_signal_payload")

logger = logging.getLogger(__name__)

Handler = Callable[[Any, "SessionObserver"], Any]
Predicate = Callable[[Any], bool]
Gate = Callable[[Any], Any]

# PAYLOAD cap (not frame cap) for the persisted payload JSON column; SSE
# envelope overhead (~176B) can push frames over this. See _sanitize_signal_payload.
_PAYLOAD_BYTE_CAP: int = 16_384  # 16 KB (payload column only; see note above)

# How long signal persistence stays off after the store refuses the writer lock.
# A refusal means another process held the writer for the whole busy timeout,
# which in practice is a maintenance pass lasting minutes. The first signal after
# the window pays one busy wait to learn whether the writer is free; the signals
# inside it are dropped at once.
_PERSIST_SKIP_WINDOW_SECONDS: float = 30.0

# SQLite's primary result codes for a writer refused the lock (SQLITE_BUSY,
# SQLITE_LOCKED). sqlite3 errors carry them as ``sqlite_errorcode`` from Python
# 3.11; the messages cover older interpreters.
_SQLITE_LOCK_CODES: frozenset[int] = frozenset({5, 6})
_SQLITE_LOCK_MESSAGES: tuple[str, ...] = (
    "database is locked",
    "database is busy",
    "database table is locked",
    "database schema is locked",
)

# Signal fields that are part of Signal base and must not be promoted into the
# payload dict (they are either redundant with columns or not meaningful there).
_BASE_SIGNAL_FIELDS: frozenset[str] = frozenset(
    {"id", "ln_id", "timestamp", "data", "emitter_role"}
)


def _sanitize_signal_payload(sig: Any) -> dict[str, Any]:
    """Build a JSON-safe, size-bounded payload dict from a Signal; oversized payloads are truncated."""
    from lionagi.ln import json_dumpb as _jdb  # noqa: PLC0415
    from lionagi.ln import json_loads as _jl  # noqa: PLC0415
    from lionagi.session.signal import MessageAdded  # noqa: PLC0415

    raw: dict[str, Any] = {}

    for _field in type(sig).model_fields:
        if _field in _BASE_SIGNAL_FIELDS:
            continue
        val = getattr(sig, _field, None)
        if val is None or val == "":
            continue
        raw[_field] = val

    # Handle sig.data per signal kind.
    if isinstance(sig, MessageAdded):
        msg = sig.data
        if msg is not None:
            ref: dict[str, Any] = {}
            for _attr in ("id", "role", "sender", "recipient"):
                v = getattr(msg, _attr, None)
                if v is not None:
                    ref[_attr] = str(v)
            raw["message_ref"] = ref
    elif sig.data is not None:
        try:
            from pydantic import BaseModel as _BaseModel  # noqa: PLC0415

            raw["data"] = (
                sig.data.model_dump() if isinstance(sig.data, _BaseModel) else str(sig.data)
            )
        except Exception:  # noqa: BLE001
            raw["data"] = repr(sig.data)

    # Serialise with safe_fallback so no TypeError escapes, then parse back —
    # single serialisation gate: after this, payload is str/int/float/bool/list/dict only.
    original_bytes: bytes | None = None
    try:
        original_bytes = _jdb(raw, safe_fallback=True)
        payload: dict[str, Any] = _jl(original_bytes)
    except Exception:  # noqa: BLE001
        payload = {"sanitize_error": repr(sig)[:256]}

    # Byte cap applies to the FINAL serialized form: re-serializing the
    # truncation marker can nearly double the size.
    try:
        if original_bytes is None:
            original_bytes = _jdb(payload, safe_fallback=True)
        original_len = len(original_bytes)

        if original_len > _PAYLOAD_BYTE_CAP:
            # Estimate marker overhead via an empty-data marker's fixed JSON
            # cost, then budget the remainder for escaped data content.
            _marker_empty = _jdb(
                {"truncated": True, "original_bytes": original_len, "data": ""},
                safe_fallback=True,
            )
            # +2 for the two quote chars around the data string value
            overhead = len(_marker_empty) - 2
            data_budget = max(0, _PAYLOAD_BYTE_CAP - overhead)

            # Clip the original bytes to the estimated data budget, then
            # iterate until the final serialized dict actually fits.
            clip_len = data_budget
            for _ in range(8):  # max 8 halvings; terminates well before that
                clipped = original_bytes[:clip_len].decode("utf-8", errors="replace")
                candidate = {
                    "truncated": True,
                    "original_bytes": original_len,
                    "data": clipped,
                }
                final = _jdb(candidate, safe_fallback=True)
                if len(final) <= _PAYLOAD_BYTE_CAP:
                    payload = candidate
                    break
                # Shrink clip proportionally to the overshoot.
                excess = len(final) - _PAYLOAD_BYTE_CAP
                clip_len = max(0, clip_len - excess)
            else:
                # Fallback: minimal marker with no data.
                payload = {"truncated": True, "original_bytes": original_len, "data": ""}
    except Exception:  # noqa: BLE001, S110
        pass  # cap failure is non-fatal; the safe payload is still usable

    return payload


def _is_lock_refusal(exc: BaseException) -> bool:
    """Whether *exc* itself, not anything it wraps or chains, is SQLite refusing the writer lock."""
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int) and code & 0xFF in _SQLITE_LOCK_CODES:
        return True
    return type(exc).__name__ == "OperationalError" and any(
        marker in str(exc).lower() for marker in _SQLITE_LOCK_MESSAGES
    )


def _is_store_refusal(exc: BaseException) -> bool:
    """Whether *exc* is the store's compatibility-mode refusal.

    Imported lazily so this module stays import-light, and read from the exception's own class
    rather than a copy: a refusal raised by a store handle is the same class object here.
    """
    from lionagi.state.compat import StateCompatibilityRefused  # noqa: PLC0415

    return isinstance(exc, StateCompatibilityRefused)


def _writer_lock_refusal(exc: BaseException) -> BaseException | None:
    """Return the driver error for a writer-lock refusal, if one is wrapped by *exc*."""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = getattr(current, "orig", None)
    for current in reversed(chain):
        if _is_lock_refusal(current):
            return current
    return None


def _is_writer_lock_failure(exc: BaseException) -> bool:
    """Whether *exc* or its wrapped driver error is a writer-lock refusal."""
    return _writer_lock_refusal(exc) is not None


@dataclass(slots=True)
class _SignalPersistState:
    """One binding's signal persistence: what it could not confirm it wrote and its
    skip window.

    Each binding has its own, so a write still in flight from a previous binding
    changes only the state it was bound with.
    """

    session_id: str
    dropped: int = 0
    # The first write this binding's store refused in compatibility mode, if any. A refusal is
    # not a drop: the store declined the write for good, so the run's teardown must fail with
    # this rather than read back a status it could not persist (ADR-0128 D6).
    refusal: Any = None
    # Signals taken and not yet finished with, from before their fields are read to the
    # end of the write. None is confirmed until it finishes, so a count read while they
    # are in flight includes them.
    in_flight: int = 0
    # Monotonic end of the skip window; None while persistence is on.
    skip_until: float | None = None
    # Whether the one try at a window's end is in flight.
    try_in_flight: bool = False
    # Whether this binding has told its caller that a skip window opened; it does so once.
    window_reported: bool = False
    # Writes go one at a time, so a write queued behind one the store refused
    # finds the window that refusal opened instead of waiting out its own.
    lock: Lock = field(default_factory=Lock)

    @property
    def unconfirmed(self) -> int:
        """Signals not known to be written: the dropped ones and the writes in flight."""
        return self.dropped + self.in_flight

    def admit(self) -> str | None:
        """``"write"``, ``"try"`` (the caller now holds the window's one try), or None to drop.

        Reads the window and claims the try with no await in between, so emits
        running in other tasks see one consistent state.
        """
        if self.skip_until is None:
            return "write"
        if self.try_in_flight or time.monotonic() < self.skip_until:
            return None
        self.try_in_flight = True
        return "try"


class _KeyAndRoleFilter(Filter):
    """Conjunction of a payload filter (key) and a role filter (event envelope)."""

    __slots__ = ("_key", "_role")

    def __init__(self, key: Filter, role: RoleFilter) -> None:
        self._key = key
        self._role = role

    def matches(self, payload: Any) -> list[Any]:
        return list(self._key.matches(payload))

    def __repr__(self) -> str:
        return f"({self._key!r} & {self._role!r})"


def _payload(obj: Any) -> Any:
    return obj.data if isinstance(obj, Signal) else obj


def _is_condition(x: Any) -> bool:
    return isinstance(x, type | Filter) or hasattr(x, "__as_filter__")


def _looks_like_handler(x: Any) -> bool:
    return callable(x) and not _is_condition(x)


class SessionObserver(Observer):
    """Typed, reactive event dispatch over a session-scoped Flow."""

    def __init__(self, session: Any = None) -> None:
        self.session = session
        self.flow: Flow = Flow(name="session-events")
        self._subs: list[tuple[Filter, Handler]] = []
        self._routes: list[tuple[Predicate, str]] = []
        self._gate: Gate | None = None
        # The latest binding's persistence state; kept after unbind so its count
        # can still be read.
        self._persist: _SignalPersistState | None = None

    @property
    def persist_refusal(self) -> Any:
        """The first write the latest bound persistence sink saw the store refuse, if any.

        A refusal means the signal was not persisted and will not be retried, so a run's teardown
        fails with it instead of reading back a status it could not write (ADR-0128 D6).
        """
        return self._persist.refusal if self._persist is not None else None

    @property
    def persist_dropped(self) -> int:
        """Signals the latest bound persistence sink could not confirm it wrote since it was bound.

        A write still in flight counts until it finishes, so the count read at unbind stays an
        upper bound whatever those writes do afterwards. A sink bound without ``db`` when there
        is no state store at all counts nothing.
        """
        return self._persist.unconfirmed if self._persist is not None else 0

    def observe(
        self,
        *keys: type | Filter | Predicate | Any,
        handler: Handler | None = None,
        role: str | None = None,
    ) -> Any:
        """Subscribe a handler to AND-composed conditions. Usable as a decorator."""
        keys_list = list(keys)
        if handler is None and len(keys_list) >= 2 and _looks_like_handler(keys_list[-1]):
            handler = keys_list.pop()  # type: ignore

        key_flt: Filter | None = all_of(*keys_list) if keys_list else None
        if role is not None:
            role_flt = RoleFilter(role)
            flt: Filter = role_flt if key_flt is None else _KeyAndRoleFilter(key_flt, role_flt)
        elif key_flt is not None:
            flt = key_flt
        else:
            raise TypeError("observe() requires at least one condition or 'role'")

        def _register(fn: Handler) -> Handler:
            self._subs.append((flt, fn))
            return fn

        return _register if handler is None else _register(handler)

    def unobserve(self, handler: Handler) -> int:
        """Remove all subscriptions for handler. Returns count removed."""
        before = len(self._subs)
        self._subs = [(f, h) for (f, h) in self._subs if h is not handler]
        return before - len(self._subs)

    def route(self, condition: Predicate, *, into: str) -> SessionObserver:
        self._routes.append((condition, into))
        return self

    def gate(self, check: Gate | None) -> SessionObserver:
        """Set the governance gate for event dispatch and pre-invoke authorization."""
        if check is None:
            raise TypeError("gate() requires a callable; use clear_gate() to remove the gate")
        self._gate = check
        return self

    def clear_gate(self) -> SessionObserver:
        """Remove the governance gate; dispatch and authorize() allow everything again."""
        self._gate = None
        return self

    async def authorize(self, action: Any) -> bool:
        """Pre-invoke gate. Returns True when no gate set; denials recorded as GateDenied."""
        if self._gate is None:
            return True
        from lionagi.protocols.action.gate import adapt_session_gate

        result = await adapt_session_gate(self._gate)(action)
        if not result.allowed:
            from .signal import GateDenied

            self.flow.add_item(GateDenied(data=action))
        return result.allowed

    async def emit(self, event: Any) -> list[Any]:
        """Gate -> store -> route -> dispatch. Returns handler results."""
        if not isinstance(event, Observable):
            event = Signal(data=event)
        payload = _payload(event)

        allowed = True
        if self._gate is not None:
            try:
                allowed = bool(await maybe_await(self._gate(payload)))
            except Exception:
                allowed = False

        self.flow.add_item(event)
        if not allowed:
            return []

        for condition, name in self._routes:
            if condition(payload):
                self._ensure_stream(name).append(event)

        ctx = self.session if self.session is not None else self
        sync_results: list[Any] = []
        coros: list[Any] = []
        for flt, handler in self._subs:
            for matched in self._match(flt, event, payload):
                out = handler(matched, ctx)
                if inspect.isawaitable(out):
                    coros.append(out)
                else:
                    sync_results.append(out)
        if coros:
            from lionagi.ln.concurrency import gather as _gather

            async_results: list[Any] = list(await _gather(*coros))
        else:
            async_results = []
        return sync_results + async_results

    @staticmethod
    def _match(flt: Filter, event: Any, payload: Any) -> list[Any]:
        """Match filter against event. Handles RoleFilter and TypeFilter specially."""
        if isinstance(flt, RoleFilter):
            return flt.matches(event)
        if isinstance(flt, _KeyAndRoleFilter):
            if not flt._role.matches(event):
                return []
            return list(flt._key.matches(payload))
        matched = list(flt.matches(payload))
        if event is not payload and isinstance(flt, TypeFilter) and isinstance(event, flt.type_):
            matched.append(event)
        return matched

    def stream(self, name: str) -> list[Any]:
        try:
            prog = self.flow.get_progression(name)
        except Exception:
            return []
        return [self.flow.items[uid] for uid in prog]

    def by_type(self, event_type: type) -> list[Any]:
        """Stored items whose payload matches event_type (unwraps Signals)."""
        flt = TypeFilter(event_type)
        return [e for e in self.flow.items if self._match(flt, e, _payload(e))]

    def _ensure_stream(self, name: str) -> Progression:
        try:
            return self.flow.get_progression(name)
        except Exception:
            prog = Progression(name=name)
            self.flow.add_progression(prog)
            return prog

    def bind_db_persistence(
        self,
        session_id: str,
        db: Any = None,
        on_window_opened: Callable[[], None] | None = None,
    ) -> None:
        """Register a subscription persisting every Signal to StateDB; pass db to reuse an open connection.

        Writes go one at a time. When the store refuses the writer lock,
        persistence is skipped for ``_PERSIST_SKIP_WINDOW_SECONDS`` with no busy
        wait, writes queued behind the refused one included; the first signal
        after the window tries once, and another refusal reopens it. Every signal
        the sink cannot confirm it wrote is counted in :attr:`persist_dropped`: a
        skipped or failed write, a signal whose fields or payload cannot be read, a
        write cancelled while queued or writing, and a write still in flight when the
        count is read. A sink bound without ``db`` when there is no state store at all
        writes and counts nothing.
        The cancelled and in-flight writes may have committed or may still land, so
        the count can exceed what the store is missing by those. A new binding
        starts its own count and window.

        *on_window_opened*, when given, is called once per binding, on the event loop, the
        first time a skip window opens. It runs inside the sink's own guard: an error it
        raises is logged at debug level and goes no further, and it must not block.
        """
        _bound_db = db
        # One sink at a time: a new binding replaces the previous one instead of
        # adding a second subscription that would write every signal twice and
        # queue new emits behind the previous binding's writes.
        previous = getattr(self, "_db_persist_handler", None)
        if previous is not None:
            self.unobserve(previous)
            self._db_persist_handler = None
        state = _SignalPersistState(session_id)
        self._persist = state

        def _persist(event: Any, _ctx: Any = None) -> Any:
            from lionagi.session.signal import Signal  # noqa: PLC0415

            sig = event if isinstance(event, Signal) else None
            if sig is None:
                return
            # Count at dispatch so an unbind sees handlers whose coroutines have not started.
            state.in_flight += 1

            async def _persist_signal() -> None:
                trying = False
                try:
                    # Unknown objects fall back to repr, MessageAdded stores a compact ref and
                    # oversized payloads are capped. A signal whose fields or payload still
                    # cannot be read (a getter or repr that raises) is counted like a failed
                    # write, opens no skip window, and the emit goes on.
                    try:
                        kind = type(sig).__name__
                        op_id = getattr(sig, "op_id", "") or ""
                        ts = time.time()
                        payload = _sanitize_signal_payload(sig)
                    except Exception:  # noqa: BLE001 — persistence never breaks the session bus
                        state.dropped += 1
                        return

                    admission = state.admit()
                    if admission is None:
                        state.dropped += 1
                        return
                    trying = admission == "try"

                    async with state.lock:
                        if not trying:
                            # Read again once the lock is held: a write that queued behind one
                            # the store refused finds the window that refusal opened.
                            admission = state.admit()
                            if admission is None:
                                state.dropped += 1
                                return
                            trying = admission == "try"
                        if _bound_db is not None:
                            # Fast path: reuse the caller's already-open connection.
                            await _bound_db.insert_session_signal(
                                session_id=session_id,
                                kind=kind,
                                op_id=op_id,
                                ts=ts,
                                payload=payload,
                            )
                        else:
                            # Fallback: open a fresh connection (standalone / test use).
                            from lionagi.state.db import (  # noqa: PLC0415
                                StateDB,
                                state_db_known_absent,
                            )

                            if state_db_known_absent():
                                # No store to write to: nothing is written or counted.
                                return
                            async with StateDB() as _db:
                                await _db.insert_session_signal(
                                    session_id=session_id,
                                    kind=kind,
                                    op_id=op_id,
                                    ts=ts,
                                    payload=payload,
                                )
                except Exception as exc:  # noqa: BLE001 — persistence never breaks the session bus
                    # A store refusal is not a failed write to count and move past: the store
                    # declined this signal for good, so it is kept for teardown to fail the run
                    # with (ADR-0128 D6). The first refusal is kept and every later one logged,
                    # so none goes unreported, and none is counted dropped -- the run is told,
                    # not left believing the signal landed.
                    if _is_store_refusal(exc):
                        if state.refusal is None:
                            state.refusal = exc
                        else:
                            logger.error(
                                "signal persistence for session %s refused another signal: %s",
                                session_id,
                                exc,
                            )
                        return
                    # The signal is already in flight while its error is classified and logged.
                    try:
                        refusal = _writer_lock_refusal(exc)
                        if refusal is not None:
                            opening = state.skip_until is None
                            state.skip_until = time.monotonic() + _PERSIST_SKIP_WINDOW_SECONDS
                            if opening:
                                if on_window_opened is not None and not state.window_reported:
                                    state.window_reported = True
                                    try:
                                        on_window_opened()
                                    except Exception:  # noqa: BLE001 — the caller's record never touches the sink
                                        logger.debug(
                                            "signal persistence window callback failed",
                                            exc_info=True,
                                        )
                                logger.warning(
                                    "signal persistence for session %s paused for %.0fs: the state "
                                    "store refused the writer lock (%r); signals emitted meanwhile "
                                    "are dropped and counted",
                                    session_id,
                                    _PERSIST_SKIP_WINDOW_SECONDS,
                                    refusal,
                                )
                    except Exception:  # noqa: BLE001, S110 — persistence never breaks the session bus
                        pass
                    finally:
                        state.dropped += 1
                except BaseException:
                    # Cancellation leaves the write unconfirmed, even if it committed first.
                    state.dropped += 1
                    raise
                else:
                    state.skip_until = None
                finally:
                    state.in_flight -= 1
                    if trying:
                        state.try_in_flight = False

            return _persist_signal()

        from lionagi.session.signal import Signal  # noqa: PLC0415

        handler = self.observe(Signal, handler=_persist)
        self._db_persist_handler = handler

    def unbind_db_persistence(self) -> None:
        """Remove the handler registered by :meth:`bind_db_persistence`.

        Logs how many signals the sink could not confirm it wrote, writes still in
        flight included, when there are any.
        """
        handler = getattr(self, "_db_persist_handler", None)
        state = self._persist
        if handler is not None and state is not None and state.unconfirmed:
            logger.warning(
                "signal persistence for session %s could not confirm it wrote %d signal(s) "
                "to the state store",
                state.session_id,
                state.unconfirmed,
            )
        if handler is not None:
            self.unobserve(handler)
            self._db_persist_handler = None

    def __repr__(self) -> str:
        return f"SessionObserver(events={len(self.flow.items)}, subscriptions={len(self._subs)})"
