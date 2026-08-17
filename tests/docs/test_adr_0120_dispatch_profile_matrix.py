# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Freeze ADR-0120's legacy dispatcher compatibility profiles.

The table is duplicated here in full and matched against the document by exact
prose. That is the mechanism, not an oversight: a comparison loose enough to
survive rewording is also loose enough to let a profile's meaning drift while
the test stays green, and the whole point of freezing these rows is that they
are the record a later migration will be checked against. Editing a row is
meant to require editing it in both places, so that changing what the system
promises cannot happen quietly. Reflowing the table will fail this test; the
answer is to reflow the copy here too, not to loosen the match.
"""

from __future__ import annotations

import importlib
from pathlib import Path

ADR_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs/adr/ADR-0120-interception-observation-and-durable-delivery-planes.md"
)

EXPECTED_PROFILES = {
    "HookBus blocking points": (
        "sequential; declaration-owned interceptor",
        "raise first ordinary failure",
        "emitter cancellation propagates; no deadline",
        "no predicate; await handler result",
    ),
    "HookBus observational points": (
        "sequential; declaration-owned interceptor",
        "isolate/log; `StopHook` ends the chain",
        "emitter cancellation propagates; no deadline",
        "no predicate; await handler result",
    ),
    "Broadcaster": (
        "async entry; sequential; invoke first and classify only `asyncio.iscoroutine()` results",
        "isolate/log ordinary `Exception`",
        "handler/emitter cancellation propagates; no deadline",
        "type mismatch raises before dispatch; non-coroutine awaitables are not awaited",
    ),
    "SessionObserver legacy observation": (
        "async entry; invoke every handler inline/sequentially, classify by returned value, then gather returned awaitables",
        "invocation/filter failure stops immediately; only after all invocations succeed, returned-awaitable failures unwrap one or raise a group and cancel remaining awaitables",
        "emitter cancellation propagates; no deadline",
        "filter/route failure propagates; `GATHER_AFTER_INVOCATIONS`",
    ),
    "message-added sync": (
        "sync preflight rejects declared async before mutation; sequential drain",
        "unwrap one failure, group several `BaseException` values",
        "caught handler failure surfaced after drain; no deadline",
        "no predicate; sync returned awaitable is discarded (deprecated compatibility)",
    ),
    "message-added async": (
        "declaration classification; sequential drain; declared async awaited",
        "unwrap one failure, group several `BaseException` values",
        "caught handler failure surfaced after drain; no deadline",
        "no predicate; sync returned awaitable is discarded (deprecated compatibility)",
    ),
    "SchedulerSignalBus": (
        "async entry; concurrent invocation; classify/await returned values in each task",
        "`RAISE_GROUP` even for one ordinary failure",
        "handler cancellation wins and becomes `SchedulerHandlerCancelled`; if ordinary errors also exist their `ExceptionGroup` is its cause; emitter cancellation propagates; no deadline",
        "predicate failure joins the ordinary-error group",
    ),
    "TerminalCallbackRegistry": (
        "async entry; declaration classification; concurrent; declared sync offloaded",
        "ordinary failure log/isolate",
        "handler cancellation is re-raised inside the task group; emitter cancellation propagates; shared-budget expiry silently cancels async work and returns; abandoned sync thread work may continue",
        "registration filter cannot execute user code; returned awaitable is awaited",
    ),
}

PUBLIC_IMPORTS = (
    ("lionagi", "Broadcaster", "lionagi.service.broadcaster"),
    ("lionagi", "HookedEvent", "lionagi.service.hooks.hooked_event"),
    ("lionagi", "HookRegistry", "lionagi.service.hooks.hook_registry"),
    ("lionagi.hooks", "HookBus", "lionagi.hooks.bus"),
    ("lionagi.protocols.types", "MessageManager", "lionagi.protocols.messages.manager"),
    ("lionagi.session.observer", "SessionObserver", "lionagi.session.observer"),
    (
        "lionagi.state.lifecycle",
        "TerminalCallbackRegistry",
        "lionagi.state.lifecycle.callbacks",
    ),
    (
        "lionagi.studio.scheduler.signals",
        "SchedulerSignalBus",
        "lionagi.studio.scheduler.signals",
    ),
)


def _profile_rows() -> tuple[tuple[str, tuple[str, ...]], ...]:
    text = ADR_PATH.read_text(encoding="utf-8")
    start = text.index("| Named profile |")
    end = text.index("\n\nThe Scheduler constructor", start)
    rows: list[tuple[str, tuple[str, ...]]] = []
    for line in text[start:end].splitlines()[2:]:
        cells = tuple(cell.strip() for cell in line.strip().strip("|").split("|"))
        if cells:
            rows.append((cells[0], cells[1:]))
    return tuple(rows)


def test_normative_profile_table_is_exact_and_closed() -> None:
    rows = _profile_rows()
    names = tuple(name for name, _profile in rows)

    assert names == tuple(EXPECTED_PROFILES)
    assert len(names) == len(set(names))
    assert dict(rows) == EXPECTED_PROFILES


def test_public_import_paths_resolve_to_their_pre_migration_modules() -> None:
    """Pin both halves of the façade contract: the path and what is behind it.

    The import is one assertion and the owning module is the other. Pinning the
    owner is the point rather than a side effect: the migration moves mechanics
    behind these façades while the public path stays stable, so the public path
    alone cannot tell a completed move from a pending one. A failure here means
    an implementation changed modules, which is a thing to do deliberately and
    to record, not a thing to discover afterwards.
    """
    for module_name, symbol, owner_module in PUBLIC_IMPORTS:
        value = getattr(importlib.import_module(module_name), symbol)
        assert value.__module__ == owner_module
