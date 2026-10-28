# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Private durable ownership of CLI worker spawn attempts."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping
from contextlib import AbstractContextManager
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from lionagi._cli_worker_context import _bind_cli_worker_recorder
from lionagi.ln.concurrency.cancel import CancelScope
from lionagi.providers._provider_errors import _WorkerSpawnAdmissionError

from .db import (
    _validate_cli_worker_session_id,
    _validate_cli_worker_spawn,
    _validate_cli_worker_uuid,
)

if TYPE_CHECKING:
    from .db import StateDB

__all__ = ()

_IDENTITY_FIELDS = frozenset(
    {"pid", "pgid", "pid_create_time", "pid_host", "pid_boot_time", "process_identity_mode"}
)
_CONTINUATION_KEY = object()


class _WorkerSpawnOwner:
    def __init__(
        self,
        db: StateDB,
        *,
        session_id: str,
        generation_id: str,
        run_id: str | None = None,
        engine_run_id: str | None = None,
        branch_id: str | None = None,
    ) -> None:
        try:
            if db is None:
                raise ValueError("a worker spawn owner requires its bound store")
            session_id = _validate_cli_worker_session_id(session_id)
            generation_id = _validate_cli_worker_uuid(generation_id, field="generation_id")
            for value in (run_id, engine_run_id, branch_id):
                if value is not None and (not isinstance(value, str) or not value):
                    raise ValueError("diagnostic association must be nonempty text or None")
        except (TypeError, ValueError) as exc:
            raise _WorkerSpawnAdmissionError("invalid worker spawn owner association") from exc
        self._db = db
        self._session_id = session_id
        self._generation_id = generation_id
        self._run_id = run_id
        self._engine_run_id = engine_run_id
        self._branch_id = branch_id
        self._closing = False
        self._closed = False
        self._continuation_prepared = False
        self._admissions: set[_WorkerSpawnAdmission] = set()
        self._tasks: set[asyncio.Future[Any]] = set()
        self._drained = asyncio.Event()
        self._drained.set()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def generation_id(self) -> str:
        return self._generation_id

    def bind(self) -> AbstractContextManager[None]:
        return _bind_cli_worker_recorder(self)

    def admit(self) -> _WorkerSpawnAdmission:
        if self._closing:
            raise _WorkerSpawnAdmissionError("worker spawn owner is closing")
        admission = _WorkerSpawnAdmission(self, spawn_id=str(uuid.uuid4()))
        self._admissions.add(admission)
        self._drained.clear()
        return admission

    def _retain(self, task: asyncio.Future[Any]) -> None:
        if task not in self._tasks:
            self._tasks.add(task)
            self._drained.clear()
            task.add_done_callback(self._task_finished)

    def _task_finished(self, task: asyncio.Future[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled():
            # Its consumer still receives the same result or exception. Drain
            # ownership also retrieves it if the original caller was cancelled.
            task.exception()
        self._check_drained()

    def _check_drained(self) -> None:
        if not self._admissions and not self._tasks:
            self._drained.set()

    async def aclose(self) -> None:
        """Close admission and drain owned work before propagating cancellation."""
        self._closing = True
        cancelled: asyncio.CancelledError | None = None
        # Shield structured cancellation; a native Task.cancel() is remembered
        # separately because it may still interrupt this task's event wait.
        with CancelScope(shield=True):
            while self._admissions or self._tasks:
                try:
                    await self._drained.wait()
                except asyncio.CancelledError as exc:
                    if cancelled is None:
                        cancelled = exc
            self._closed = True
        if cancelled is not None:
            raise cancelled


class _WorkerSpawnAdmission:
    def __init__(self, owner: _WorkerSpawnOwner, *, spawn_id: str) -> None:
        self._owner = owner
        self._spawn_id = spawn_id
        self._payload: Mapping[str, Any] | None = None
        self._tasks: set[asyncio.Future[Any]] = set()
        self._recording = False
        self._finished = False

    def retain(self, task: asyncio.Future[Any]) -> None:
        if self._finished:
            raise _WorkerSpawnAdmissionError("worker spawn admission is finished")
        self._tasks.add(task)
        self._owner._retain(task)

    async def record(self, identity: Mapping[str, object]) -> None:
        if self._finished:
            raise _WorkerSpawnAdmissionError("worker spawn admission is finished")
        if self._recording:
            raise _WorkerSpawnAdmissionError("worker spawn registration is already in progress")
        try:
            if not isinstance(identity, Mapping) or set(identity) != _IDENTITY_FIELDS:
                raise ValueError("worker identity has incomplete or unknown fields")
            payload = _validate_cli_worker_spawn(
                {
                    "spawn_id": self._spawn_id,
                    "session_id": self._owner.session_id,
                    "generation_id": self._owner.generation_id,
                    "run_id": self._owner._run_id,
                    "engine_run_id": self._owner._engine_run_id,
                    "branch_id": self._owner._branch_id,
                    "pid": identity["pid"],
                    "pgid": identity["pgid"],
                    "pid_create_time": identity["pid_create_time"],
                    "pid_host": identity["pid_host"],
                    "pid_boot_time": identity["pid_boot_time"],
                    "process_identity_mode": identity["process_identity_mode"],
                }
            )
        except (TypeError, ValueError) as exc:
            raise _WorkerSpawnAdmissionError("invalid observed worker identity") from exc
        if self._payload is None:
            self._payload = MappingProxyType(payload)
        elif payload != self._payload:
            raise _WorkerSpawnAdmissionError("worker spawn attempt identity changed")
        self._recording = True
        try:
            await self._owner._db.register_cli_worker_spawn(dict(self._payload))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise _WorkerSpawnAdmissionError("worker spawn registration refused") from exc
        finally:
            self._recording = False

    def finish(self) -> None:
        if self._finished:
            return
        current = asyncio.current_task()
        if self._recording or any(not task.done() and task is not current for task in self._tasks):
            raise _WorkerSpawnAdmissionError("worker spawn still has owned work pending")
        self._finished = True
        self._tasks.clear()
        self._owner._admissions.discard(self)
        self._owner._check_drained()


class _WorkerSpawnContinuation:
    __slots__ = ("_session_id", "_generation_id", "_consumed")

    def __init__(self, owner: _WorkerSpawnOwner, *, _key: object) -> None:
        if _key is not _CONTINUATION_KEY:
            raise _WorkerSpawnAdmissionError("worker continuation is not admitted")
        self._session_id = owner.session_id
        self._generation_id = owner.generation_id
        self._consumed = False

    def _consume(self) -> tuple[str, str]:
        if self._consumed:
            raise _WorkerSpawnAdmissionError("worker continuation was already consumed")
        self._consumed = True
        return self._session_id, self._generation_id


def _prepare_worker_spawn_continuation(
    owner: _WorkerSpawnOwner, *, db_close_witness: object
) -> _WorkerSpawnContinuation:
    if (
        db_close_witness is not owner._db
        or not owner._closed
        or owner._admissions
        or owner._tasks
        or owner._continuation_prepared
    ):
        raise _WorkerSpawnAdmissionError("worker continuation requires a drained and closed owner")
    owner._continuation_prepared = True
    return _WorkerSpawnContinuation(owner, _key=_CONTINUATION_KEY)


def _burn_worker_spawn_continuation(continuation: object) -> tuple[str, str]:
    if type(continuation) is not _WorkerSpawnContinuation:
        raise _WorkerSpawnAdmissionError("worker continuation is required")
    return continuation._consume()


async def _consume_worker_spawn_continuation(
    continuation: object,
    db: StateDB,
    *,
    run_id: str | None = None,
    engine_run_id: str | None = None,
    branch_id: str | None = None,
) -> _WorkerSpawnOwner:
    session_id, generation_id = _burn_worker_spawn_continuation(continuation)
    try:
        session = await db.get_session(session_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise _WorkerSpawnAdmissionError("worker continuation session read refused") from exc
    if (
        not isinstance(session, Mapping)
        or session.get("id") != session_id
        or session.get("status") != "running"
        or session.get("cli_worker_generation") != generation_id
    ):
        raise _WorkerSpawnAdmissionError(
            "worker continuation no longer matches its running session"
        )
    return _WorkerSpawnOwner(
        db,
        session_id=session_id,
        generation_id=generation_id,
        run_id=run_id,
        engine_run_id=engine_run_id,
        branch_id=branch_id,
    )
