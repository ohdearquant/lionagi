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

Beside the rows, ``protected`` records the decision about the conversation's first round
(ADR-0130 Amendment 4): whether its compiled messages stay ahead of every later fold, so the
entry through them is read by every later request. It is decided once, at the compile of the
first request that carries the round whole, and kept across a ledger reset, resume and clone.
"""

from __future__ import annotations

import hashlib
import json
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
    "PROTECTION_VERSION",
    "TTL_SECONDS",
    "PromptCacheCall",
    "activate_call",
    "cache_anchors",
    "cache_intent",
    "clone_ledger",
    "conversation_call",
    "current_call",
    "current_intent",
    "decide_protection",
    "established",
    "expires_at",
    "ledger_for",
    "note_attempt",
    "placement_routes",
    "prompt_cache_call",
    "protection",
    "settle",
)

Intent = Literal["continuing", "final", "side"]

LEDGER_KEY = "prompt_cache"
# A bound for a pathological run, not a working limit: rows leave by expiry long before it.
CEILING = 512
TTL_SECONDS = {"5m": 300.0, "1h": 3600.0}
PROTECTION_KEY = "protected"
PROTECTION_VERSION = 1
# The first round stays ahead of every fold when its compiled size is at most a tenth of the cap.
HEAD_BUDGET_DIVISOR = 10

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
    # The protected first round as this request compiled it: ``kept`` when its messages hash as
    # they did at the decision, ``changed`` when they do not (a cache miss, never a re-decision).
    frontier: str | None = None


_call: ContextVar[PromptCacheCall | None] = ContextVar("lionagi_prompt_cache_call", default=None)


def current_call() -> PromptCacheCall | None:
    return _call.get()


@contextmanager
def prompt_cache_call(
    ledger: dict | None,
    intent: Intent | None = None,
    *,
    anchors: tuple[tuple[int, int], ...] = (),
    frontier: str | None = None,
) -> Iterator[PromptCacheCall]:
    call = PromptCacheCall(
        ledger=ledger, intent=intent or current_intent(), anchors=anchors, frontier=frontier
    )
    token = _call.set(call)
    try:
        yield call
    finally:
        _call.reset(token)


def conversation_call(
    metadata: dict,
    imodel: Any,
    *,
    anchors: tuple[tuple[int, int], ...] = (),
    frontier: str | None = None,
):
    """The call context for one request of a conversation: its branch's rows when the
    endpoint places marks, else none."""
    if not getattr(getattr(imodel, "endpoint", None), "places_prompt_cache", False):
        return nullcontext()
    return prompt_cache_call(ledger_for(metadata), anchors=anchors, frontier=frontier)


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
        # The rows start over; a decision about the first round stands.
        kept = ledger.get(PROTECTION_KEY) if isinstance(ledger, dict) else None
        ledger = {"version": 1, "entries": {}, "evicted_until": None}
        if _valid_protection(kept):
            ledger[PROTECTION_KEY] = kept
        metadata[LEDGER_KEY] = ledger
    entries = ledger["entries"]
    for cert in [cert for cert, entry in entries.items() if not _valid_row(cert, entry)]:
        del entries[cert]
    return ledger


def _count_value(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_protection(record: Any) -> bool:
    if not isinstance(record, dict):
        return False
    members = record.get("members")
    frontier = record.get("frontier")
    projection = record.get("projection")
    return (
        record.get("version") == PROTECTION_VERSION
        and record.get("state") == "decided"
        and isinstance(record.get("included"), bool)
        and isinstance(members, list)
        and bool(members)
        and all(isinstance(member, str) for member in members)
        and isinstance(frontier, dict)
        and _count_value(frontier.get("message"))
        and _count_value(frontier.get("slot"))
        and isinstance(frontier.get("hash"), str)
        and isinstance(projection, dict)
        and (projection.get("max_chars") is None or _count_value(projection.get("max_chars")))
    )


def protection(metadata: dict) -> dict | None:
    """The decision about the first round, or None while it is pending (or stored malformed)."""
    ledger = metadata.get(LEDGER_KEY)
    record = ledger.get(PROTECTION_KEY) if isinstance(ledger, dict) else None
    return record if _valid_protection(record) else None


def _through(messages: list[dict], message: int, slot: int) -> list[dict]:
    """The compiled messages up to and including block ``slot`` of message ``message``, a
    string content read as the one text block the provider reads it as."""
    head = []
    for entry in messages[: message + 1]:
        content = entry.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}] if content.strip() else []
        head.append({**entry, "content": content})
    if isinstance(head[-1]["content"], list):
        head[-1]["content"] = head[-1]["content"][: slot + 1]
    return head


def _hash(messages: list[dict]) -> str:
    text = json.dumps(messages, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def decide_protection(
    metadata: dict,
    view: dict,
    messages: list[dict],
    *,
    cap: int,
    scale: float,
    max_chars: int | None,
    decided_by: str,
) -> dict:
    """Decide, once, whether the first round stays ahead of every fold, and record it.

    ``view`` is the compiler's cache view of this request: the round's member ids and its
    frontier, the (message, block) place where the round's compiled material ends. The round is
    included when its compiled size, counted as the input cap counts, is at most a tenth of
    ``cap``; equality fits.
    """
    from lionagi.protocols.messages._result_projection import PROJECTION_VERSION
    from lionagi.service.connections.api_calling import estimate_payload_input_tokens

    message, slot = view["frontier"]
    head = _through(messages, message, slot)
    size = math.ceil(estimate_payload_input_tokens({"messages": head}) * scale)
    record = {
        "version": PROTECTION_VERSION,
        "state": "decided",
        "included": size * HEAD_BUDGET_DIVISOR <= cap,
        "members": list(view["round"]),
        "frontier": {"message": message, "slot": slot, "hash": _hash(head)},
        "cap": cap,
        "head_budget": cap / HEAD_BUDGET_DIVISOR,
        "size": size,
        "scale": scale,
        "projection": {"max_chars": max_chars, "version": PROJECTION_VERSION},
        "decided_at": time.time(),
        "decided_by": decided_by,
    }
    ledger_for(metadata)[PROTECTION_KEY] = record
    return record


def cache_anchors(
    metadata: dict, view: dict, messages: list[dict], decide: Any = None
) -> tuple[tuple[tuple[int, int], ...], str | None]:
    """The anchors a request names for its marks, and how it compiled the protected round.

    The first round is decided here when it is still pending and this request carries it whole
    (``decide`` is the fold's decider; without a fold nothing is decided). The anchors are the
    end of an included first round and the newest compaction note.
    """
    record = protection(metadata)
    frontier = view.get("frontier")
    if record is None and frontier is not None and view.get("round") and decide is not None:
        record = decide(view, messages)
    anchors: list[tuple[int, int]] = []
    state = None
    if record is not None and record["included"] and frontier is not None:
        anchors.append(tuple(frontier))
        same = _hash(_through(messages, *frontier)) == record["frontier"]["hash"]
        state = "kept" if same else "changed"
    if (note := view.get("note")) is not None and tuple(note) not in anchors:
        anchors.append(tuple(note))
    return tuple(anchors), state


def expires_at(entry: dict) -> float:
    last = max(entry.get("marked_at") or 0.0, entry.get("hit_at") or 0.0)
    return last + TTL_SECONDS.get(entry.get("ttl"), TTL_SECONDS["5m"])


def established(entry: dict, routes: frozenset[str] | set[str]) -> bool:
    """Whether this process saw the entry written or read on every one of ``routes``."""
    held = entry.get("routes") or {}
    return bool(routes) and all(held.get(route) == _PROCESS for route in routes)


def clone_ledger(ledger: Any, id_map: dict[str, Any] | None = None) -> dict | None:
    """A copy for a cloned Branch: the same rows, none of them evidenced for the clone, and the
    first round's decision with its members renamed to the clone's messages (``id_map``: source
    id to clone id). A decision naming a message the clone does not hold is left behind, and
    the clone's first round is pending again."""
    if not isinstance(ledger, dict) or not isinstance(ledger.get("entries"), dict):
        return None
    entries = {
        cert: {**entry, "routes": {}}
        for cert, entry in ledger["entries"].items()
        if isinstance(entry, dict)
    }
    copied = {"version": 1, "entries": entries, "evicted_until": ledger.get("evicted_until")}
    record = ledger.get(PROTECTION_KEY)
    if _valid_protection(record) and id_map is not None:
        members = [id_map.get(member) for member in record["members"]]
        if all(member is not None for member in members):
            copied[PROTECTION_KEY] = {
                **record,
                "members": [str(member) for member in members],
                "frontier": dict(record["frontier"]),
                "projection": dict(record["projection"]),
            }
    return copied


def _count(usage: dict, key: str) -> int | None:
    # An absent count is unknown, never zero: a response that does not report its cache split
    # says nothing about what the provider wrote.
    value = usage.get(key)
    return value if _count_value(value) else None


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
    if call.frontier is not None:
        record["frontier"] = call.frontier
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
