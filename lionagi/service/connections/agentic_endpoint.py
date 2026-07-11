# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from typing import ClassVar

from .endpoint import Endpoint
from .endpoint_config import EndpointConfig

__all__ = ("AgenticEndpoint",)


class AgenticEndpoint(Endpoint):
    """Base for CLI/in-process agentic endpoints; subclasses implement ``stream()`` yielding ``StreamChunk``."""

    is_cli: ClassVar[bool] = True

    # True when the transport yields its first StreamChunk shortly after the
    # subprocess spawns (e.g. an ndjson "system"/"init" event), so a stalled
    # first chunk reliably signals a dead worker. False for transports that
    # buffer all output until the run completes, where a slow-but-healthy
    # call looks identical to a dead one until the whole result arrives —
    # gates run.py's default liveness watchdog (LIONAGI_WORKER_LIVENESS_TIMEOUT).
    streams_first_output_early: ClassVar[bool] = False

    DEFAULT_CONCURRENCY_LIMIT: ClassVar[int] = 3
    DEFAULT_QUEUE_CAPACITY: ClassVar[int] = 10

    def __init__(self, config: dict | EndpointConfig = None, **kwargs):
        super().__init__(config=config, **kwargs)
        self._session_id: str | None = None

    @property
    def provider_session_id(self) -> str | None:
        return self._session_id

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @session_id.setter
    def session_id(self, value: str | None):
        self._session_id = value

    def _create_http_session(self):
        raise NotImplementedError("Agentic endpoints do not use HTTP sessions")

    async def _call_aiohttp(self, *a, **kw):
        raise NotImplementedError("Agentic endpoints do not use aiohttp")

    async def _stream_aiohttp(self, payload: dict, headers: dict, **kwargs):
        raise NotImplementedError("Agentic endpoints do not use aiohttp streaming")
