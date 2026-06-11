# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Reactive, typed event dispatch over a session's Flow."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from lionagi.ln.concurrency import maybe_await
from lionagi.ln.types import Filter, RoleFilter, TypeFilter, all_of
from lionagi.protocols._concepts import Observable, Observer

from ..protocols.generic.flow import Flow
from ..protocols.generic.progression import Progression
from .signal import Signal

__all__ = ("SessionObserver", "RoleFilter", "_sanitize_signal_payload")

Handler = Callable[[Any, "SessionObserver"], Any]
Predicate = Callable[[Any], bool]
Gate = Callable[[Any], Any]

# Maximum byte size for a persisted signal payload.  Payloads exceeding this
# cap are truncated: the dict is serialised to JSON, sliced, and a
# ``truncated`` + ``original_bytes`` marker is injected so downstream
# consumers know the data is partial.
_PAYLOAD_BYTE_CAP: int = 16_384  # 16 KB

# Signal fields that are part of Signal base and must not be promoted into the
# payload dict (they are either redundant with columns or not meaningful there).
_BASE_SIGNAL_FIELDS: frozenset[str] = frozenset(
    {"id", "ln_id", "timestamp", "data", "emitter_role"}
)


def _sanitize_signal_payload(sig: Any) -> dict[str, Any]:
    """Build a JSON-safe, size-bounded payload dict from a Signal instance.

    Serialisation policy:
    - Non-base fields from ``type(sig).model_fields`` are collected into a
      raw dict, with ``MessageAdded.data`` replaced by a compact reference
      to avoid duplicating message content in ``session_signals``.
    - The raw dict is then serialised to a JSON string using
      ``safe_fallback=True`` (unknown objects fall back to repr-with-type),
      and immediately parsed back to a plain Python dict.  This guarantees
      that every value stored is a JSON-native type — no non-serialisable
      object can survive into ``_to_json_column(payload)``.
    - If the resulting JSON exceeds ``_PAYLOAD_BYTE_CAP`` bytes, the payload
      is replaced by ``{truncated: True, original_bytes: N, data: "<clip>"}``.
    """
    import json as _json  # noqa: PLC0415

    from lionagi.ln import json_dumps as _jd  # noqa: PLC0415
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

    # Serialise with safe_fallback so no TypeError escapes, then parse back to
    # a dict of JSON-native types.  This is the single serialisation gate:
    # after this point, ``payload`` contains only str/int/float/bool/list/dict.
    safe_json: str | None = None
    try:
        safe_json = _jd(raw, safe_fallback=True)
        payload: dict[str, Any] = _json.loads(safe_json)
    except Exception:  # noqa: BLE001
        payload = {"sanitize_error": repr(sig)[:256]}

    # Apply byte cap on the FINAL serialized form, not the intermediate
    # safe_json string.  safe_json may be under the cap, but after wrapping in
    # a truncation-marker dict and re-serializing (with JSON escaping of
    # backslashes, quotes, etc.) the stored column can be 2× larger.
    #
    # Strategy:
    #  1. Measure the final serialized payload.  If under cap, done.
    #  2. If over cap, build a truncation-marker dict with a data slice whose
    #     byte length starts at (cap - marker_overhead) and shrinks until the
    #     whole re-serialized dict fits.  The loop terminates quickly because
    #     each iteration exactly measures the excess; at most a few rounds.
    try:
        if safe_json is not None:
            original_bytes = safe_json.encode("utf-8")
        else:
            original_bytes = _jd(payload, safe_fallback=True).encode("utf-8")
        original_len = len(original_bytes)

        if original_len > _PAYLOAD_BYTE_CAP:
            # Estimate marker overhead: serialise the marker with an empty data
            # string to get the fixed-cost JSON bytes, then allow the remainder
            # of the cap budget for the escaped data content.
            _marker_empty = _jd(
                {"truncated": True, "original_bytes": original_len, "data": ""},
                safe_fallback=True,
            )
            # +2 for the two quote chars around the data string value
            overhead = len(_marker_empty.encode("utf-8")) - 2
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
                final = _jd(candidate, safe_fallback=True).encode("utf-8")
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

    def observe(
        self,
        *keys: type | Filter | Predicate | Any,
        handler: Handler | None = None,
        role: str | None = None,
    ) -> Any:
        """Subscribe a handler to AND-composed conditions. Usable as a decorator."""
        keys_list = list(keys)
        if handler is None and len(keys_list) >= 2 and _looks_like_handler(keys_list[-1]):
            handler = keys_list.pop()

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

    def gate(self, check: Gate) -> SessionObserver:
        """Set the governance gate for event dispatch and pre-invoke authorization."""
        self._gate = check
        return self

    async def authorize(self, action: Any) -> bool:
        """Pre-invoke gate. Returns True when no gate set. Denials recorded as GateDenied."""
        if self._gate is None:
            return True
        try:
            allowed = bool(await maybe_await(self._gate(action)))
        except Exception:
            allowed = False
        if not allowed:
            from .signal import GateDenied

            self.flow.add_item(GateDenied(data=action))
        return allowed

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
    ) -> None:
        """Register a subscription that persists every emitted Signal to StateDB.

        Wires an async handler onto self so that each call to :meth:`emit`
        appends a row to ``session_signals`` via :meth:`StateDB.insert_session_signal`.

        When *db* is supplied (an already-open :class:`~lionagi.state.db.StateDB`
        instance held by the CLI lifecycle), signals are written through that
        connection without any extra open/close overhead — one write per signal,
        the same cost as a message-persistence write.  When *db* is ``None`` (the
        standalone / unit-test fallback), a fresh connection is opened per signal;
        this is correct for isolated use but carries per-signal connection overhead
        so it should not be used on production chatty sessions.

        Calling this more than once for the same session_id is idempotent only
        if the caller holds a reference and calls :meth:`unbind_db_persistence`
        first; otherwise a second handler is added and each signal is written twice.
        The handler is stored on ``self._db_persist_handler`` so the caller can
        detach it at teardown.
        """
        import time as _time

        # Capture the caller-supplied db reference.  In production CLI paths this
        # is the long-lived StateDB opened by setup_agent_persist /
        # setup_orchestration_persist — the same instance used for message writes.
        _bound_db = db

        async def _persist(event: Any, _ctx: Any = None) -> None:
            from lionagi.session.signal import Signal  # noqa: PLC0415

            sig = event if isinstance(event, Signal) else None
            if sig is None:
                return
            kind = type(sig).__name__
            op_id = getattr(sig, "op_id", "") or ""
            ts = _time.time()
            # Build a JSON-safe, size-bounded payload.  _sanitize_signal_payload
            # never raises: unknown objects fall back to repr, MessageAdded stores
            # only a compact message reference, and oversized payloads are capped.
            payload = _sanitize_signal_payload(sig)

            try:
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
                    from lionagi.state.db import DEFAULT_DB_PATH, StateDB  # noqa: PLC0415

                    if not DEFAULT_DB_PATH.exists():
                        return
                    async with StateDB(DEFAULT_DB_PATH) as _db:
                        await _db.insert_session_signal(
                            session_id=session_id,
                            kind=kind,
                            op_id=op_id,
                            ts=ts,
                            payload=payload,
                        )
            except Exception:  # noqa: BLE001, S110
                pass  # persistence is best-effort — never break the session bus

        from lionagi.session.signal import Signal  # noqa: PLC0415

        handler = self.observe(Signal, handler=_persist)
        self._db_persist_handler = handler

    def unbind_db_persistence(self) -> None:
        """Remove the handler registered by :meth:`bind_db_persistence`."""
        handler = getattr(self, "_db_persist_handler", None)
        if handler is not None:
            self.unobserve(handler)
            self._db_persist_handler = None

    def __repr__(self) -> str:
        return f"SessionObserver(events={len(self.flow.items)}, subscriptions={len(self._subs)})"
