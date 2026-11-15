# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Which aiosqlite connection-worker threads a test started and left running."""

from __future__ import annotations

import asyncio
import threading


def _is_aiosqlite_worker(thread: threading.Thread) -> bool:
    # aiosqlite starts its worker as Thread(target=_connection_worker_thread) without a
    # name, so the target is the stable mark; older builds named it sqlite_<path>.
    target = getattr(thread, "_target", None)
    return (
        getattr(target, "__name__", "") == "_connection_worker_thread"
        or "_connection_worker_thread" in thread.name
        or thread.name.startswith("sqlite")
    )


def aiosqlite_threads() -> frozenset[threading.Thread]:
    return frozenset(t for t in threading.enumerate() if _is_aiosqlite_worker(t))


def new_aiosqlite_threads(before: frozenset[threading.Thread]) -> frozenset[threading.Thread]:
    """Workers alive now that were not alive at ``before``.

    A worker another test left behind that exits while this one runs is not this
    test's leak, so a comparison of counts would misread it; a set difference does not.
    """
    return aiosqlite_threads() - before


async def settled_new_aiosqlite_threads(
    before: frozenset[threading.Thread], *, expected: int = 0, timeout: float = 1.0
) -> frozenset[threading.Thread]:
    """The new workers once their number reaches ``expected`` or ``timeout`` passes.

    A closed connection joins its worker shortly after ``close()`` returns.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        new = new_aiosqlite_threads(before)
        if len(new) == expected or loop.time() >= deadline:
            return new
        await asyncio.sleep(0.05)
