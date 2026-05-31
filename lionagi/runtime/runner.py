# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import os
import shlex
import signal
from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from lionagi.runtime.control import ControlVerb, RunnerHandle, RunnerState


class PlayRunner(Protocol):
    async def start(self, plan: Any, **kwargs: Any) -> str: ...

    async def control(self, handle_id: str, verb: ControlVerb, reason: str) -> RunnerHandle: ...

    async def status(self, handle_id: str) -> RunnerHandle: ...

    async def logs(self, handle_id: str, since: datetime | None = None) -> AsyncIterator[str]: ...


@dataclass
class _RunningEntry:
    process: asyncio.subprocess.Process
    handle: RunnerHandle
    lock: asyncio.Lock
    stdout_lines: deque[tuple[datetime, str]]
    task: asyncio.Task[None] | None = None


_DEFAULT_CANCEL_GRACE_SECONDS = 5.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_plan_map(plan: Any, key: str, *, default: Any = None) -> Any:
    if isinstance(plan, Mapping):
        return plan.get(key, default)
    return default


def _coerce_session_id(plan: Any, kwargs: dict[str, Any]) -> str:
    session_id = kwargs.get("session_id") or _coerce_plan_map(plan, "session_id")
    if session_id is None:
        raise ValueError("session_id is required")
    return str(session_id)


def _coerce_runner_type(plan: Any, kwargs: dict[str, Any]) -> str:
    runner_type = kwargs.get("runner_type")
    if not runner_type:
        runner_type = _coerce_plan_map(plan, "runner_type")
    return str(runner_type or "local")


def _coerce_command(plan: Any) -> list[str]:
    command = _coerce_plan_map(plan, "command")
    if isinstance(plan, str) and command is None:
        command = plan
    if command is None:
        command = _coerce_plan_map(plan, "cmd")
    if isinstance(command, str):
        return shlex.split(command)
    if isinstance(command, list | tuple):
        return [str(item) for item in command]
    raise TypeError(f"Unsupported plan command shape: {type(command)!r}")


def _coerce_metadata(plan: Any, kwargs: dict[str, Any]) -> dict:
    metadata = kwargs.get("metadata")
    if isinstance(metadata, dict):
        return dict(metadata)
    raw = _coerce_plan_map(plan, "metadata")
    if isinstance(raw, dict):
        return dict(raw)
    return {}


async def _tail_stdout(entry: _RunningEntry) -> None:
    if entry.process.stdout is None:
        return
    while True:
        data = await entry.process.stdout.readline()
        if not data:
            break
        line = data.decode("utf-8", errors="replace").rstrip("\r\n")
        entry.stdout_lines.append((_now(), line))


class LocalRunner:
    """Local subprocess runner for local flow/play execution."""

    def __init__(self) -> None:
        self._entries: dict[str, _RunningEntry] = {}

    async def start(self, plan: Any, **kwargs: Any) -> str:
        command = _coerce_command(plan)
        if not command:
            raise ValueError("command must not be empty")

        session_id = _coerce_session_id(plan, dict(kwargs))
        runner_type = _coerce_runner_type(plan, dict(kwargs))
        metadata = _coerce_metadata(plan, dict(kwargs))

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        handle = RunnerHandle(
            session_id=session_id,
            state=RunnerState.PREPARING,
            runner_type=runner_type,
            pid=process.pid,
            started_at=_now(),
            metadata=metadata,
        )
        entry = _RunningEntry(
            process=process,
            handle=handle,
            lock=asyncio.Lock(),
            stdout_lines=deque(maxlen=2048),
            task=None,
        )
        entry.task = asyncio.create_task(_tail_stdout(entry))
        self._entries[session_id] = entry
        handle.state = RunnerState.RUNNING
        return session_id

    async def control(self, handle_id: str, verb: ControlVerb, reason: str) -> RunnerHandle:
        _ = reason
        entry = self._entries.get(handle_id)
        if entry is None:
            raise KeyError(f"Unknown handle {handle_id!r}")

        async with entry.lock:
            if verb == ControlVerb.PAUSE:
                self._require_process_running(entry)
                _signal(entry, signal.SIGSTOP)
                entry.handle.state = RunnerState.PAUSED
                return entry.handle

            if verb == ControlVerb.RESUME:
                self._require_process_running(entry)
                _signal(entry, signal.SIGCONT)
                entry.handle.state = RunnerState.RUNNING
                return entry.handle

            if verb == ControlVerb.CANCEL:
                await self._cancel(entry, graceful=True)
                return entry.handle

            if verb == ControlVerb.KILL:
                await self._cancel(entry, graceful=False)
                return entry.handle

            if verb == ControlVerb.RETRY:
                raise ValueError("retry is not supported by LocalRunner")

            raise ValueError(f"Unsupported verb: {verb!s}")

    async def status(self, handle_id: str) -> RunnerHandle:
        entry = self._entries.get(handle_id)
        if entry is None:
            raise KeyError(f"Unknown handle {handle_id!r}")

        if entry.process.returncode is not None:
            if entry.handle.state == RunnerState.CANCELLING:
                entry.handle.state = RunnerState.KILLED
            elif entry.handle.state not in {
                RunnerState.COMPLETED,
                RunnerState.FAILED,
                RunnerState.TIMED_OUT,
                RunnerState.KILLED,
            }:
                if entry.process.returncode == 0:
                    entry.handle.state = RunnerState.COMPLETED
                else:
                    entry.handle.state = RunnerState.FAILED
            return entry.handle

        if self._pid_dead(entry.process.pid):
            if entry.handle.state not in {
                RunnerState.CANCELLING,
                RunnerState.KILLED,
                RunnerState.COMPLETED,
                RunnerState.FAILED,
                RunnerState.TIMED_OUT,
            }:
                entry.handle.state = RunnerState.FAILED

        return entry.handle

    async def logs(self, handle_id: str, since: datetime | None = None) -> AsyncIterator[str]:
        entry = self._entries.get(handle_id)
        if entry is None:
            raise KeyError(f"Unknown handle {handle_id!r}")

        cutoff = since.timestamp() if since is not None else None
        for _, line in tuple(entry.stdout_lines):
            if cutoff is None:
                yield line
            else:
                # compare on emitted time in the deque tuple
                ts = _.timestamp()
                if ts >= cutoff:
                    yield line

    async def _cancel(self, entry: _RunningEntry, *, graceful: bool) -> None:
        if entry.process.returncode is not None:
            if entry.process.returncode == 0:
                entry.handle.state = RunnerState.COMPLETED
            else:
                entry.handle.state = RunnerState.FAILED
            return

        entry.handle.state = RunnerState.CANCELLING
        if graceful:
            _signal(entry, signal.SIGTERM)
            deadline = asyncio.get_event_loop().time() + _DEFAULT_CANCEL_GRACE_SECONDS
            while asyncio.get_event_loop().time() < deadline:
                if entry.process.returncode is not None:
                    if entry.process.returncode == 0:
                        entry.handle.state = RunnerState.COMPLETED
                    else:
                        entry.handle.state = RunnerState.FAILED
                    return
                await asyncio.sleep(0.05)

        _signal(entry, signal.SIGKILL)
        await _wait_for_process(entry.process)
        entry.handle.state = RunnerState.KILLED

    @staticmethod
    def _require_process_running(entry: _RunningEntry) -> None:
        if entry.process.returncode is not None:
            raise RuntimeError("process is not running")

    @staticmethod
    def _pid_dead(pid: int | None) -> bool:
        if pid is None:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False


def _signal(entry: _RunningEntry, sig: signal.Signals) -> None:
    pid = entry.process.pid
    if pid is None:
        raise RuntimeError("process has no pid")
    try:
        os.kill(pid, sig)
    except ProcessLookupError as exc:
        raise RuntimeError(f"process {pid} not found") from exc


async def _wait_for_process(process: asyncio.subprocess.Process) -> None:
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except TimeoutError:
        pass


__all__ = ["LocalRunner", "PlayRunner"]
