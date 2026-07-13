# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import shutil
from collections.abc import AsyncIterator, Callable
from functools import partial
from pathlib import Path
from typing import Any

from lionagi.libs.path_safety import contain_and_resolve, has_traversal
from lionagi.libs.schema.as_readable import as_readable
from lionagi.ln._proc import aterminate_process_group

log = logging.getLogger(__name__)

# Sentinel that means "do not pass stdin to create_subprocess_exec at all"
# (inherits the parent process stdin, matching the old Gemini/Pi behaviour).
_INHERIT_STDIN = object()


async def ndjson_from_cli(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdin: Any = asyncio.subprocess.DEVNULL,
    tail_repair: Callable[[str], dict | None] | None = None,
) -> AsyncIterator[dict]:
    """Yield dicts from an NDJSON-emitting CLI subprocess; tail_repair handles malformed final chunks."""
    kwargs: dict[str, Any] = dict(
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    if stdin is not _INHERIT_STDIN:
        kwargs["stdin"] = stdin
    proc = await asyncio.create_subprocess_exec(*cmd, **kwargs)
    # Capture PGID immediately — waiting until teardown risks ProcessLookupError
    # if the child already exited. See docs/internals/runtime.md.

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
                if tail_repair is not None:
                    try:
                        repaired = tail_repair(buffer)
                        if repaired is not None:
                            yield repaired
                            log.warning("Repaired malformed JSON fragment at stream end")
                        else:
                            log.error("Skipped unrecoverable JSON tail: %.120s...", buffer)
                    except Exception:  # noqa: BLE001
                        log.error("Skipped unrecoverable JSON tail: %.120s...", buffer)
                else:
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
        await aterminate_process_group(proc, grace=5.0)

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

    if has_traversal(ws_path):
        raise ValueError(f"Directory traversal detected in workspace path: {workspace}")

    return contain_and_resolve(ws_path, repo)


def validate_message_prompt(data: dict) -> dict:
    """Derive prompt/system_prompt from messages when prompt is unset (shared by Gemini, Pi, Codex request models)."""
    from lionagi import ln

    if data.get("prompt"):
        return data

    if not (msg := data.get("messages")):
        raise ValueError("messages or prompt required")

    prompts = []
    for message in msg:
        if message["role"] != "system":
            content = message["content"]
            if isinstance(content, dict | list):
                prompts.append(ln.json_dumps(content))
            else:
                prompts.append(content)
        elif message["role"] == "system" and not data.get("system_prompt"):
            data["system_prompt"] = message["content"]

    data["prompt"] = "\n".join(prompts)
    return data


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


def discover_cli(binary: str) -> tuple[bool, str | None]:
    """Return (available, resolved_path_or_name) for a CLI binary discovered on PATH."""
    candidate = shutil.which(binary) or binary
    if shutil.which(candidate):
        return True, candidate
    return False, None


def make_cli_flag(
    flag: str,
    order: int,
    kind: str = "value",
    *,
    neg_flag: str | None = None,
) -> dict[str, Any]:
    """Build a json_schema_extra dict describing a declarative CLI flag (see build_declarative_cli_args)."""
    d: dict[str, Any] = {"cli_flag": flag, "cli_order": order, "cli_kind": kind}
    if neg_flag:
        d["cli_neg_flag"] = neg_flag
    return d


print_readable = partial(as_readable, md=True, display_str=True)
