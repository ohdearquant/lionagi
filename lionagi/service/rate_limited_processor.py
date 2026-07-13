# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
import logging
from typing import Any

import anyio
from anyio import get_cancelled_exc_class
from typing_extensions import Self, override

from lionagi.ln.concurrency import Lock
from lionagi.protocols.types import Executor, Processor

from .connections.api_calling import APICalling

__all__ = (
    "RateLimitedAPIProcessor",
    "RateLimitedAPIExecutor",
)


class RateLimitedAPIProcessor(Processor):
    event_type = APICalling

    def __init__(
        self,
        queue_capacity: int,
        capacity_refresh_time: float,
        interval: float | None = None,
        limit_requests: int = None,
        limit_tokens: int = None,
        concurrency_limit: int | None = None,
    ):
        super().__init__(
            queue_capacity=queue_capacity,
            capacity_refresh_time=capacity_refresh_time,
            concurrency_limit=concurrency_limit,
        )
        self.limit_tokens = limit_tokens
        self.limit_requests = limit_requests
        self.interval = interval or self.capacity_refresh_time
        self._available_requests = self.limit_requests
        self._available_tokens = self.limit_tokens
        self._rate_limit_replenisher_task: asyncio.Task | None = None
        self._lock = Lock()

    async def start_replenishing(self):
        """Start replenishing rate limit capacities at regular intervals. See
        docs/internals/runtime.md for the cancellation-handling rationale."""
        try:
            await self.start()
            while not self.is_stopped():
                await anyio.sleep(self.interval)

                # Reset available counters to their configured limits
                async with self._lock:
                    if self.limit_requests is not None:
                        self._available_requests = self.limit_requests
                    if self.limit_tokens is not None:
                        self._available_tokens = self.limit_tokens

                # Re-drive deferred work — forward() is one-shot, so without this drain
                # rate-limited events would sit PENDING until invoke()'s safety timeout.
                if not self.queue.empty():
                    await self.process()

        except get_cancelled_exc_class():
            logging.debug("Rate limit replenisher task cancelled.")
        except Exception as e:
            logging.error(f"Error in rate limit replenisher: {e}")

    @override
    async def stop(self) -> None:
        """Stop the replenishment task; swallows the Python 3.11+ re-raised
        ``CancelledError`` so callers closing multiple iModels don't abort early."""
        if self._rate_limit_replenisher_task:
            self._rate_limit_replenisher_task.cancel()
            try:
                await self._rate_limit_replenisher_task
            except get_cancelled_exc_class():
                pass
            finally:
                self._rate_limit_replenisher_task = None
        await super().stop()

    @override
    @classmethod
    async def create(
        cls,
        queue_capacity: int,
        capacity_refresh_time: float,
        interval: float | None = None,
        limit_requests: int = None,
        limit_tokens: int = None,
        concurrency_limit: int | None = None,
    ) -> Self:
        self = cls(
            interval=interval,
            queue_capacity=queue_capacity,
            capacity_refresh_time=capacity_refresh_time,
            limit_requests=limit_requests,
            limit_tokens=limit_tokens,
            concurrency_limit=concurrency_limit,
        )
        # TODO(#1043 Phase 2): migrate to anyio task group (structured concurrency)
        self._rate_limit_replenisher_task = asyncio.create_task(self.start_replenishing())
        return self

    @override
    async def request_permission(self, required_tokens: int = None, **kwargs: Any) -> bool:
        # No limits configured, just check queue capacity
        if self._available_requests is None and self._available_tokens is None:
            return self.queue.qsize() < self.queue_capacity

        async with self._lock:
            # Check both limits before decrementing either to avoid
            # leaking request budget when token check fails.
            if self._available_requests is not None:
                if self._available_requests < 1:
                    return False

            if self._available_tokens is not None and required_tokens:
                if self._available_tokens < required_tokens:
                    return False

            # Both checks passed — now decrement.
            if self._available_requests is not None:
                self._available_requests -= 1
            if self._available_tokens is not None and required_tokens:
                self._available_tokens -= required_tokens

        return True

    @override
    async def handle_denied(self, event: Any) -> bool:
        """Rate-limit denial is a DEFERRAL, not a rejection: returns ``False`` so
        ``process()`` re-enqueues the event (stays ``PENDING``) instead of terminalizing it."""
        return False


class RateLimitedAPIExecutor(Executor):
    processor_type = RateLimitedAPIProcessor

    def __init__(
        self,
        queue_capacity: int,
        capacity_refresh_time: float,
        interval: float | None = None,
        limit_requests: int = None,
        limit_tokens: int = None,
        strict_event_type: bool = False,
        concurrency_limit: int | None = None,
    ):
        config = {
            "queue_capacity": queue_capacity,
            "capacity_refresh_time": capacity_refresh_time,
            "interval": interval,
            "limit_requests": limit_requests,
            "limit_tokens": limit_tokens,
            "concurrency_limit": concurrency_limit,
        }
        super().__init__(processor_config=config, strict_event_type=strict_event_type)
        self.config = config
        self.interval = interval
        self.limit_requests = limit_requests
        self.limit_tokens = limit_tokens
        self.concurrency_limit = concurrency_limit or queue_capacity
