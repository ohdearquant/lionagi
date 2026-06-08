# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import logging
import os
import signal
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


async def ndjson_from_cli(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> AsyncIterator[dict]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    # Capture PGID immediately — if we wait until teardown, the child may have
    # exited and been reaped, and os.getpgid(proc.pid) would raise
    # ProcessLookupError. start_new_session=True makes pgid == proc.pid.
    # Guard against mocked subprocesses in tests where proc.pid may not be a
    # real int: a MagicMock.pid coerces to 1 via __int__, and
    # os.killpg(1, SIGTERM) signals init/the CI runner.
    # os.killpg is POSIX-only: on Windows leave _pgid None so the group-kill
    # path is skipped and cleanup falls through to proc.terminate()/kill().
    _pgid: int | None = (
        proc.pid if hasattr(os, "killpg") and isinstance(proc.pid, int) and proc.pid > 1 else None
    )

    decoder = codecs.getincrementaldecoder("utf-8")()
    json_decoder = json.JSONDecoder()
    buffer: str = ""

    if proc.stdout is None:
        raise RuntimeError("Failed to capture stdout from subprocess")

    # Bounded stderr drain — without this a stderr-heavy session deadlocks
    # when the OS pipe buffer fills before stdout EOF.
    stderr_cap = 256 * 1024
    stderr_chunks: list[bytes] = []
    stderr_total = 0

    async def _drain_stderr() -> None:
        nonlocal stderr_total
        if proc.stderr is None:
            return
        try:
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                remaining = stderr_cap - stderr_total
                if remaining > 0:
                    take = chunk[:remaining]
                    stderr_chunks.append(take)
                    stderr_total += len(take)
        except Exception as exc:
            log.debug("stderr drain ended: %s", exc)

    stderr_task = asyncio.create_task(_drain_stderr())

    try:
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break

            buffer += decoder.decode(chunk)

            while buffer:
                buffer = buffer.lstrip()
                if not buffer:
                    break
                try:
                    obj, idx = json_decoder.raw_decode(buffer)
                    yield obj
                    buffer = buffer[idx:]
                except json.JSONDecodeError:
                    break

        buffer += decoder.decode(b"", final=True)
        buffer = buffer.strip()
        if buffer:
            try:
                obj, idx = json_decoder.raw_decode(buffer)
                yield obj
            except json.JSONDecodeError:
                log.error("Skipped unrecoverable JSON tail: %.120s...", buffer)

        rc = await proc.wait()
        if rc != 0:
            drain_truncated = False
            try:
                await asyncio.wait_for(asyncio.shield(stderr_task), timeout=2.0)
            except asyncio.TimeoutError:
                drain_truncated = True
            except asyncio.CancelledError:
                raise
            err = b"".join(stderr_chunks).decode(errors="replace").strip()
            if drain_truncated:
                err = (err or "") + " [stderr drain timed out]"
            raise RuntimeError(err or f"CLI subprocess exited with code {rc}")

    finally:
        pgid = _pgid
        if pgid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGTERM)
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            if pgid is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pgid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()

        # Reap the stderr drain task — contextlib.suppress(Exception) does NOT
        # catch CancelledError (BaseException), so we suppress it explicitly.
        stderr_task.cancel()
        try:
            await stderr_task
        except (asyncio.CancelledError, Exception):  # noqa: S110, BLE001
            pass


def resolve_cli_workspace(repo: Path | None, workspace: str | None) -> Path:
    if repo is None:
        repo = Path.cwd()
    if not workspace:
        return repo

    ws_path = Path(workspace)

    if ws_path.is_absolute():
        raise ValueError(f"Workspace path must be relative, got absolute: {workspace}")

    if ".." in ws_path.parts:
        raise ValueError(f"Directory traversal detected in workspace path: {workspace}")

    repo_resolved = repo.resolve()
    result = (repo / ws_path).resolve()

    try:
        result.relative_to(repo_resolved)
    except ValueError:
        raise ValueError(
            f"Workspace path escapes repository bounds. "
            f"Repository: {repo_resolved}, Workspace: {result}"
        ) from None

    return result


def build_declarative_cli_args(model_instance: Any) -> list[str]:
    flagged: list[tuple[int, dict, Any]] = []
    for field_name, field_info in type(model_instance).model_fields.items():
        extra = field_info.json_schema_extra
        if not extra or "cli_flag" not in extra:
            continue
        val = getattr(model_instance, field_name)
        if val is None:
            continue
        if isinstance(val, list) and not val:
            continue
        if val is False and extra.get("cli_kind") != "bool_pair":
            continue
        flagged.append((extra["cli_order"], extra, val))

    flagged.sort(key=lambda x: x[0])

    args: list[str] = []
    for _, extra, val in flagged:
        flag = extra["cli_flag"]
        kind = extra.get("cli_kind", "value")

        if kind == "bool":
            if val:
                args.append(flag)

        elif kind == "bool_pair":
            if val is True:
                args.append(flag)
            elif val is False and extra.get("cli_neg_flag"):
                args.append(extra["cli_neg_flag"])

        elif kind == "list_args":
            args.append(flag)
            args.extend(str(v) for v in val)

        elif kind == "json_value":
            serialized = json.dumps(val) if isinstance(val, dict | list) else str(val)
            args.extend([flag, serialized])

        elif kind == "repeat":
            for v in val:
                args.extend([flag, str(v)])

        else:  # "value"
            args.extend([flag, str(val)])

    return args
