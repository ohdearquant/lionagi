from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


def run_async(coro: Awaitable[T]) -> T:
    """Run a coroutine synchronously.

    Deprecated: prefer converting callers to ``async def`` and using ``await``
    directly (pytest-asyncio manages the event loop).  This helper remains for
    sync contexts (e.g. sync fixtures, ``TestClient`` wrappers) that cannot be
    made async without broader refactoring.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
