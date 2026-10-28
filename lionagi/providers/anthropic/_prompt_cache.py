# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Where the cache marks of one Messages API request go (ADR-0127 Amendment 3).

The request is read as the API reads it: tools, then system blocks, then each message's content
blocks. Every block gets a prefix certificate: a running SHA-256 over the request through that
block, in three tiers (tools, system, messages), each seeded with the settings its cache depends
on and chained to the tier before it. Each message adds a boundary token carrying its role and
every message-level field except its content, so a block moved across a message boundary, or a
message inserted, changes every later certificate. The only edit before hashing is the removal of
``cache_control`` from the block itself. Equal certificates mean the requests are identical up to
that block; they say nothing about whether the provider still holds the entry.

A mark reaches an entry when it sits at or after the entry's block and at most 19 lookback
positions past it; a run of consecutive ``tool_use`` blocks, or of ``tool_result`` blocks, in one
message is one position.

Marks are chosen in this order, each while a slot of the four is free and the order of lifetimes
stays legal (no one-hour mark after a five-minute mark): a read of the deepest entry known to be
held on every route the request can take; the end of the turn, when more of the conversation
follows; one hour on the end of the tools and system blocks; a probe of each deeper entry whose
evidence is unknown, deepest first; the anchors the compiler names, when more follows. Marks the
caller placed are kept where they are and count toward the four.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from lionagi.service import _prompt_cache as ledger_mod

__all__ = ("CERT_VERSION", "LOOKBACK", "MAX_MARKS", "Point", "place")

CERT_VERSION = "cert-v1"
LOOKBACK = 20
MAX_MARKS = 4
_UNMARKABLE = frozenset({"thinking", "redacted_thinking"})
_RUN_TYPES = frozenset({"tool_use", "tool_result"})
_SERVER_TOOL_PREFIXES = ("web_search", "web_fetch", "code_execution")
_FIVE = {"type": "ephemeral"}
_HOUR = {"type": "ephemeral", "ttl": "1h"}


@dataclass(slots=True)
class Point:
    """One block of the request: where it sits, what precedes it, and whether it takes a mark."""

    index: int
    position: int
    tier: str
    cert: str
    message: int | None
    slot: int
    markable: bool
    mark: str | None
    # Lifetimes of the caller's marks inside the block's own content, in the order sent.
    nested: tuple[str, ...] = ()


def _canon(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str
    ).encode()


def _step(state: bytes, part: bytes) -> bytes:
    return hashlib.sha256(state + part).digest()


def _ttl(cache_control: Any) -> str:
    return "1h" if isinstance(cache_control, dict) and cache_control.get("ttl") == "1h" else "5m"


def _is_blank_text(block: dict) -> bool:
    return block.get("type") == "text" and not (
        isinstance(block.get("text"), str) and block["text"].strip()
    )


def _content_blocks(content: Any) -> list:
    if isinstance(content, str):
        # The API reads a string as one text block, so both forms certify alike.
        return [{"type": "text", "text": content}] if content.strip() else []
    return list(content) if isinstance(content, list) else []


def _caller_mark(block: Any) -> str | None:
    # Only a mark on the block itself takes a lookback position here. Marks inside a tool
    # result's own content count toward the four but are not credited with reaching an entry.
    if isinstance(block, dict) and block.get("cache_control") is not None:
        return _ttl(block["cache_control"])
    return None


def _nested_marks(block: Any) -> tuple[str, ...]:
    inner = block.get("content") if isinstance(block, dict) else None
    if not isinstance(inner, list):
        return ()
    return tuple(
        _ttl(b["cache_control"])
        for b in inner
        if isinstance(b, dict) and b.get("cache_control") is not None
    )


def points(payload: dict) -> tuple[list[Point], int]:
    """Every block of the request in the order the API reads it, and the marks already on it."""
    out: list[Point] = []
    caller = 0
    output_config = payload.get("output_config")
    state = _step(
        b"",
        _canon(
            {
                "cert": CERT_VERSION,
                "model": payload.get("model"),
                "thinking": payload.get("thinking"),
                "effort": output_config.get("effort") if isinstance(output_config, dict) else None,
            }
        ),
    )
    position = 0

    def add(block: Any, tier: str, message: int | None, slot: int, same_run: bool) -> None:
        nonlocal state, position, caller
        body = (
            {k: v for k, v in block.items() if k != "cache_control"}
            if isinstance(block, dict)
            else block
        )
        state = _step(state, _canon(body))
        if not same_run:
            position += 1
        markable = (
            isinstance(block, dict)
            and block.get("type") not in _UNMARKABLE
            and not _is_blank_text(block)
        )
        mark = _caller_mark(block)
        nested = _nested_marks(block)
        caller += (mark is not None) + len(nested)
        out.append(
            Point(len(out), position, tier, state.hex(), message, slot, markable, mark, nested)
        )

    for slot, tool in enumerate(payload.get("tools") or ()):
        add(tool, "tools", None, slot, False)
    state = _step(state, _canon({"speed": payload.get("speed")}))
    for slot, block in enumerate(payload.get("system") or ()):
        add(block, "system", None, slot, False)
    state = _step(state, _canon({"tool_choice": payload.get("tool_choice")}))
    for number, message in enumerate(payload.get("messages") or ()):
        if not isinstance(message, dict):
            state = _step(state, b"<msg>" + _canon(message))
            continue
        envelope = {k: v for k, v in message.items() if k != "content"}
        state = _step(state, b"<msg>" + _canon(envelope))
        previous = None
        for slot, block in enumerate(_content_blocks(message.get("content"))):
            kind = block.get("type") if isinstance(block, dict) else None
            add(block, "messages", number, slot, kind in _RUN_TYPES and kind == previous)
            previous = kind
    return out, caller


def _reaches(mark: Point, entry: Point) -> bool:
    return mark.index >= entry.index and mark.position - entry.position < LOOKBACK


def _legal(ttls: dict[float, str]) -> bool:
    seen_short = False
    for index in sorted(ttls):
        if ttls[index] == "5m":
            seen_short = True
        elif seen_short:
            return False
    return True


def _server_tools(payload: dict) -> bool:
    for tool in payload.get("tools") or ():
        kind = tool.get("type") if isinstance(tool, dict) else None
        if isinstance(kind, str) and kind.startswith(_SERVER_TOOL_PREFIXES):
            return True
    return False


def place(payload: dict, call: ledger_mod.PromptCacheCall | None) -> dict:
    """Mark ``payload`` in place and return the placement record.

    With no call context (a payload built outside a conversation request), the rows are empty and
    the request continues, which marks the end of the turn and one hour on the stable segment.
    """
    import time

    now = time.time()
    intent = call.intent if call is not None else "continuing"
    rows = call.ledger["entries"] if call is not None and call.ledger is not None else {}
    evicted_until = (call.ledger or {}).get("evicted_until") if call is not None else None
    anchors = call.anchors if call is not None else ()
    routes = ledger_mod.placement_routes()

    blocks, caller_count = points(payload)
    ttls: dict[int, str] = {p.index: p.mark for p in blocks if p.mark is not None}
    roles: dict[int, list[str]] = {index: ["caller"] for index in ttls}
    # The caller's marks inside a block's content are read before the block's own mark, so for
    # the order of lifetimes they sit between that block and the one before it.
    nested = {
        p.index - 1 + (k + 1) / (len(p.nested) + 1): ttl
        for p in blocks
        for k, ttl in enumerate(p.nested)
    }
    degraded: list[str] = []

    live = {
        p.index: rows[p.cert]
        for p in blocks
        if p.markable and p.cert in rows and ledger_mod.expires_at(rows[p.cert]) > now
    }
    established = [i for i, row in live.items() if ledger_mod.established(row, routes)]
    unknown = [i for i in live if i not in established]
    partly = [i for i in unknown if any(ledger_mod.established(live[i], {r}) for r in routes)]

    def free() -> bool:
        return caller_count + sum(1 for i in ttls if "caller" not in roles[i]) < MAX_MARKS

    def add(index: int, ttl: str, role: str) -> bool:
        if index in ttls:
            roles[index].append(role)
            return True
        if not free():
            return False
        trial = {**nested, **ttls, index: ttl}
        if not _legal(trial):
            degraded.append(f"ttl_order:{role}")
            return False
        ttls[index] = ttl
        roles[index] = [role]
        return True

    def reached(index: int) -> bool:
        return any(_reaches(blocks[m], blocks[index]) for m in ttls)

    last_message = len(payload.get("messages") or ()) - 1
    tail = next(
        (
            p
            for p in reversed(blocks)
            if p.tier == "messages" and p.markable and p.message == last_message
        ),
        None,
    )
    stable = next((p for p in reversed(blocks) if p.tier != "messages" and p.markable), None)
    conversation = intent != "side"
    continuing = intent == "continuing"
    # Anchors the compiler names, as (message, block) places in the sent messages.
    anchor_points = [
        point
        for anchor in (anchors if continuing else ())
        if (
            point := next(
                (
                    p
                    for p in blocks
                    if p.tier == "messages" and p.markable and (p.message, p.slot) == anchor
                ),
                None,
            )
        )
        is not None
    ]

    # 1. Read the deepest entry held on every route.
    target = max(established) if established and conversation else None
    slot_free = caller_count < MAX_MARKS
    order_refused = False
    if target is not None and not reached(target):
        # A mark this request places anyway serves as the read when it reaches the entry.
        serving = [(tail, "tail")] if continuing and tail is not None else []
        serving += [(point, "anchor") for point in anchor_points]
        server = next(
            ((point, role) for point, role in serving if _reaches(point, blocks[target])), None
        )
        if server is not None:
            point, role = server
            if add(point.index, "5m", role):
                roles[point.index].append("read")
            else:
                order_refused = free()
        elif not add(target, live[target].get("ttl") or "5m", "read"):
            order_refused = free()

    # 2. The end of the turn, when more of the conversation follows.
    if continuing and tail is not None:
        add(tail.index, "5m", "tail")
    # 3. One hour on the end of the tools and system blocks.
    if stable is not None:
        if stable.index in ttls and "caller" in roles[stable.index]:
            # A caller's mark here is raised to one hour when every earlier mark is one hour.
            earlier = [t for i, t in ttls.items() if i < stable.index]
            if all(t == "1h" for t in earlier):
                ttls[stable.index] = "1h"
            elif ttls[stable.index] == "5m":
                degraded.append("ttl_order:stable")
            roles[stable.index].append("stable")
        else:
            add(stable.index, "1h", "stable")
    # 4. Probe deeper entries whose evidence is unknown, deepest first.
    if conversation:
        floor = target if target is not None else -1
        for index in sorted((i for i in unknown if i > floor), reverse=True):
            if reached(index):
                continue
            if not add(index, live[index].get("ttl") or "5m", "probe") and not free():
                degraded.append("probe_slots")
                break
    # 5. The anchors, when more of the conversation follows. Each takes a mark of its own: a
    # later mark that reaches it reads an entry there but never writes or refreshes one.
    for point in anchor_points:
        add(point.index, "5m", "anchor")

    # The guarantee is judged on the marks as they will be sent.
    g1: Any
    if target is None:
        g1 = {"excluded": "route_unselected" if partly else "no_established_candidate"}
    elif evicted_until and evicted_until > now:
        g1 = {"excluded": "ledger_evicted"}
    elif not slot_free and not reached(target):
        g1 = {"excluded": "caller_marks"}
    elif order_refused:
        g1 = {"excluded": "ttl_order"}
    elif live[target].get("length") is None:
        g1 = {"excluded": "length_unknown"}
    else:
        g1 = "met" if reached(target) else "defect"

    _write(payload, blocks, ttls, roles)
    marks = [
        {
            "index": i,
            "position": blocks[i].position,
            "tier": blocks[i].tier,
            "cert": blocks[i].cert,
            "ttl": ttls[i],
            "roles": roles[i],
        }
        for i in sorted(ttls)
    ]
    landing = [
        {
            "index": i,
            "cert": blocks[i].cert,
            "length": live[i].get("length"),
            "established_on": sorted(r for r in routes if ledger_mod.established(live[i], {r})),
        }
        for i in sorted(live)
        if reached(i)
    ]
    record: dict[str, Any] = {
        "cert_version": CERT_VERSION,
        "intent": intent,
        "routes": sorted(routes),
        "blocks": len(blocks),
        "marks": marks,
        "landing": landing,
        "candidates": {"established": len(established), "unknown": len(unknown)},
        "g1": g1,
        "degraded": degraded,
        "server_tools": _server_tools(payload),
    }
    if target is not None:
        record["target"] = {
            "index": target,
            "cert": blocks[target].cert,
            "length": live[target].get("length"),
            "ttl": live[target].get("ttl"),
            "expires_at": ledger_mod.expires_at(live[target]),
        }
    return record


def _write(
    payload: dict, blocks: list[Point], ttls: dict[int, str], roles: dict[int, list[str]]
) -> None:
    """Write the chosen marks, copying each list and block it changes, never the caller's."""
    copied = set()
    for index, ttl in ttls.items():
        point = blocks[index]
        if roles[index] == ["caller"]:
            continue
        mark = dict(_HOUR if ttl == "1h" else _FIVE)
        if point.tier not in copied:
            payload[point.tier] = list(payload[point.tier])
            copied.add(point.tier)
        if point.tier != "messages":
            blocks_of_tier = payload[point.tier]
            blocks_of_tier[point.slot] = {**blocks_of_tier[point.slot], "cache_control": mark}
        else:
            message = payload["messages"][point.message]
            content = _content_blocks(message.get("content"))
            content[point.slot] = {**content[point.slot], "cache_control": mark}
            payload["messages"][point.message] = {**message, "content": content}
