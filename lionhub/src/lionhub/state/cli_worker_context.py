# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Private context binding for durable CLI worker ownership."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Protocol

__all__ = ()


class _CliWorkerAdmission(Protocol):
    async def record(self, identity: Mapping[str, object]) -> None:
        """Commit this attempt's observed identity before exposing its child."""
        ...

    def retain(self, task: asyncio.Future[Any]) -> None:
        """Keep owned spawn or cleanup work alive until the admission finishes."""
        ...

    def finish(self) -> None:
        """Idempotently finish an attempt after registration or owned cleanup."""
        ...


class _CliWorkerRecorder(Protocol):
    def admit(self) -> _CliWorkerAdmission:
        """Admit one OS spawn, refusing once the owner begins closing."""
        ...


_CLI_WORKER_RECORDER: ContextVar[_CliWorkerRecorder | None] = ContextVar(
    "lionagi_cli_worker_recorder", default=None
)


def _get_cli_worker_recorder() -> _CliWorkerRecorder | None:
    return _CLI_WORKER_RECORDER.get()


@contextmanager
def _bind_cli_worker_recorder(recorder: _CliWorkerRecorder | None) -> Iterator[None]:
    """Bind an owner for descendant tasks; explicit None masks an outer owner."""
    token = _CLI_WORKER_RECORDER.set(recorder)
    try:
        yield
    finally:
        _CLI_WORKER_RECORDER.reset(token)
