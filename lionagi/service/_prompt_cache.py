# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The prompt-cache entries a Branch has written, and the per-call context that carries them.

A Branch keeps, in ``metadata["prompt_cache"]``, one row per cache entry its completed requests
wrote or read, keyed by the entry's prefix certificate (a running hash over the compiled request
through the marked block, computed by the provider adapter). The chat path opens a call context
around each request; the adapter reads the rows there to choose where its marks go and leaves its
placement on the call; when the call completes, ``settle`` updates the rows from the placement
and the response usage and returns the call's cache record.

Rows are written only from a completed request's own placement and usage, never at compile time.
Evidence that the provider holds an entry is kept per route (an account name, or ``direct``) and
per process: a row restored in another process, as a resumed run's Branch is, or copied into a
clone, starts with no evidence, because the provider may no longer hold what another process
wrote. A row that does not have the shape written here is dropped when the rows are read.
"""

from __future__ import annotations

import math
import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal
from uuid import uuid4

__all__ = (
    "CEILING",
    "LEDGER_KEY",
    "TTL_SECONDS",
    "PromptCacheCall",
    "activate_call",
    "cache_intent",
    "clone_ledger",
    "conversation_call",
    "current_call",
    "current_intent",
    "established",
    "expires_at",
    "ledger_for",
    "note_attempt",
    "placement_routes",
    "prompt_cache_call",
    "settle",
)

Intent = Literal["continuing", "final", "side"]

LEDGER_KEY = "prompt_cache"
# A bound for a pathological run, not a working limit: rows leave by expiry long before it.
CEILING = 512
TTL_SECONDS = {"5m": 300.0, "1h": 3600.0}

_PROCESS = uuid4().hex

_intent: ContextVar[Intent] = ContextVar("lionagi_prompt_cache_intent", default="continuing")


@contextmanager
def cache_intent(intent: Intent) -> Iterator[None]:
    """Declare what follows the requests made inside: more of the conversation (``continuing``,
    the default), nothing (``final``), or nothing that shares it (``side``)."""
    token = _intent.set(intent)
    try:
        yield
    finally:
        _intent.reset(token)


def current_intent() -> Intent:
    return _intent.get()


@dataclass(slots=True)
class PromptCacheCall:
    """One request's view of the rows: read at placement, written when it completes.

    ``ledger`` is None for a side request, which reads and writes no conversation rows.
    ``anchors`` are (message, block) places in the sent messages that a later request should
    still reach.
    """

    ledger: dict | None
    intent: Intent
    anchors: tuple[tuple[int, int], ...] = ()
    placement: dict | None = None
    attempts: list[tuple[str, float]] = field(default_factory=list)


_call: ContextVar[PromptCacheCall | None] = ContextVar("lionagi_prompt_cache_call", default=None)


def current_call() -> PromptCacheCall | None:
    return _call.get()


@contextmanager
def prompt_cache_call(
    ledger: dict | None, intent: Intent | None = None, *, anchors: tuple[tuple[int, int], ...] = ()
) -> Iterator[PromptCacheCall]:
    call = PromptCacheCall(ledger=ledger, intent=intent or current_intent(), anchors=anchors)
    token = _call.set(call)
    try:
        yield call
    finally:
        _call.reset(token)


def conversation_call(metadata: dict, imodel: Any):
    """The call context for one request of a conversation: its branch's rows when the
    endpoint places marks, else none."""
    if not getattr(getattr(imodel, "endpoint", None), "places_prompt_cache", False):
        return nullcontext()
    return prompt_cache_call(ledger_for(metadata))


@contextmanager
def activate_call(call: PromptCacheCall | None) -> Iterator[None]:
    """Make the call built in the caller's context current where the request is sent."""
    token = _call.set(call)
    try:
        yield
    finally:
        _call.reset(token)


def note_attempt(route: str | None) -> None:
    """Record that an attempt of the current request starts now, on ``route``."""
    call = _call.get()
    if call is not None:
        call.attempts.append((route or "unlabelled", time.time()))


def placement_routes() -> frozenset[str]:
    """Every route a request built now can take: the configured accounts, or ``direct``."""
    from lionagi.config import settings

    accounts = settings.ANTHROPIC_ACCOUNTS
    return frozenset(spec.name for spec in accounts) if accounts else frozenset({"direct"})


def _time(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _valid_row(cert: Any, entry: Any) -> bool:
    if not isinstance(cert, str) or not isinstance(entry, dict):
        return False
    routes = entry.get("routes")
    length = entry.get("length")
    hit_at = entry.get("hit_at")
    ttl = entry.get("ttl")
    return (
        isinstance(ttl, str)
        and ttl in TTL_SECONDS
        and isinstance(entry.get("tier"), str)
        and _time(entry.get("marked_at"))
        and (hit_at is None or _time(hit_at))
        and (length is None or (isinstance(length, int) and not isinstance(length, bool)))
        and isinstance(routes, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in routes.items())
    )


def ledger_for(metadata: dict) -> dict:
    """The Branch's rows, created when absent; a malformed ledger starts over, a malformed row
    is dropped, so a stored value never fails a request after it was sent."""
    ledger = metadata.get(LEDGER_KEY)
    evicted_until = ledger.get("evicted_until") if isinstance(ledger, dict) else None
    if (
        not isinstance(ledger, dict)
        or not isinstance(ledger.get("entries"), dict)
        or not (evicted_until is None or _time(evicted_until))
    ):
        ledger = {"version": 1, "entries": {}, "evicted_until": None}
        metadata[LEDGER_KEY] = ledger
    entries = ledger["entries"]
    for cert in [cert for cert, entry in entries.items() if not _valid_row(cert, entry)]:
        del entries[cert]
    return ledger


def expires_at(entry: dict) -> float:
    last = max(entry.get("marked_at") or 0.0, entry.get("hit_at") or 0.0)
    return last + TTL_SECONDS.get(entry.get("ttl"), TTL_SECONDS["5m"])


def established(entry: dict, routes: frozenset[str] | set[str]) -> bool:
    """Whether this process saw the entry written or read on every one of ``routes``."""
    held = entry.get("routes") or {}
    return bool(routes) and all(held.get(route) == _PROCESS for route in routes)


def clone_ledger(ledger: Any) -> dict | None:
    """A copy for a cloned Branch: the same rows, none of them evidenced for the clone."""
    if not isinstance(ledger, dict) or not isinstance(ledger.get("entries"), dict):
        return None
    entries = {
        cert: {**entry, "routes": {}}
        for cert, entry in ledger["entries"].items()
        if isinstance(entry, dict)
    }
    return {"version": 1, "entries": entries, "evicted_until": ledger.get("evicted_until")}


def _count(usage: dict, key: str) -> int | None:
    # An absent count is unknown, never zero: a response that does not report its cache split
    # says nothing about what the provider wrote.
    value = usage.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _complete_usage(api_call: Any) -> dict | None:
    from lionagi.protocols.generic.event import EventStatus

    if getattr(api_call, "status", None) != EventStatus.COMPLETED:
        return None
    response = getattr(api_call, "response", None)
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return None
    counts = {
        key: _count(usage, key)
        for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    }
    if any(value is None for value in counts.values()):
        return None
    split = usage.get("cache_creation")
    if isinstance(split, dict):
        for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"):
            if (value := _count(split, key)) is not None:
                counts[key] = value
    return counts


def _evict(ledger: dict, record: dict) -> None:
    entries = ledger["entries"]
    if len(entries) <= CEILING:
        return
    # Five-minute rows first, then the shortest prefixes, then the soonest to expire.
    order = sorted(
        entries,
        key=lambda cert: (
            entries[cert].get("ttl") == "1h",
            entries[cert].get("length") or 0,
            expires_at(entries[cert]),
        ),
    )
    watermark = ledger.get("evicted_until") or 0.0
    for cert in order[: len(entries) - CEILING]:
        watermark = max(watermark, expires_at(entries.pop(cert)))
    ledger["evicted_until"] = watermark
    record.setdefault("degraded", []).append("ledger_evicted")


def settle(call: PromptCacheCall, api_call: Any) -> dict:
    """Update the rows from the completed request and return its cache record.

    A request with no complete usage writes nothing and records ``read: unevaluated``.
    """
    record = dict(call.placement or {})
    record.setdefault("intent", call.intent)
    usage = _complete_usage(api_call)
    if usage is None or not call.attempts or call.placement is None:
        record["read"] = "unevaluated"
        record["hit"] = "none"
        return record
    route, started = call.attempts[-1]
    read = usage["cache_read_input_tokens"]
    record.update(usage=usage, route=route, started_at=started)

    target = record.get("target")
    if record.get("g1") == "met" and isinstance(target, dict) and target.get("length"):
        if read >= target["length"]:
            record["read"] = "at_or_above"
        elif started > (target.get("expires_at") or 0.0):
            record["read"] = "below_expired"
        else:
            record["read"] = "below_live"
    else:
        record["read"] = {"size": read}

    blind = bool(record.get("server_tools"))
    landing = [
        row for row in record.get("landing") or () if route in (row.get("established_on") or ())
    ]
    hit = "none"
    if read > 0:
        hit = "unidentified"
        matches = [row for row in landing if row.get("length") == read]
        if not blind and len(matches) == 1:
            match = matches[0]
            deeper_unknown = any(
                row["index"] > match["index"] and row.get("length") is None for row in landing
            )
            if not deeper_unknown:
                hit = {"identified": match["cert"]}
    record["hit"] = hit

    ledger = call.ledger
    if ledger is None:
        return record
    entries = ledger["entries"]
    now = time.time()
    for cert in [cert for cert, entry in entries.items() if expires_at(entry) <= now]:
        del entries[cert]
    marks = record.get("marks") or []
    for mark in marks:
        entry = entries.setdefault(mark["cert"], {"length": None, "hit_at": None, "routes": {}})
        entry.update(ttl=mark["ttl"], tier=mark["tier"], marked_at=started)
        entry.setdefault("routes", {})[route] = _PROCESS
    if marks and not blind:
        deepest = max(marks, key=lambda mark: mark["index"])
        entries[deepest["cert"]]["length"] = read + usage["cache_creation_input_tokens"]
    if isinstance(hit, dict) and (entry := entries.get(hit["identified"])) is not None:
        entry["hit_at"] = started
        entry.setdefault("routes", {})[route] = _PROCESS
    _evict(ledger, record)
    return record
