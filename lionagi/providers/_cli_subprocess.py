# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import logging
import os
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from lionagi.libs.path_safety import contain_and_resolve, has_traversal
from lionagi.libs.schema.as_readable import as_readable
from lionagi.ln._proc import (
    aterminate_process_group,
    group_member_pids,
    kill_group_now,
)
from lionagi.ln.concurrency.utils import maybe_await

log = logging.getLogger(__name__)

# Sentinel that means "do not pass stdin to create_subprocess_exec at all"
# (inherits the parent process stdin, matching the old Gemini/Pi behaviour).
_INHERIT_STDIN = object()


def spawned_pgid(pid: int) -> int:
    """The process group of a just-spawned child.

    Falls back to the child's own pid: every spawn here uses
    ``start_new_session``, so the child leads its own group by construction and
    that pid IS the group id whenever the read fails because the child has
    already exited.
    """
    try:
        return os.getpgid(pid)
    except OSError:
        return pid


def spawned_create_time(pid: int) -> float | None:
    """When the process at *pid* started, or None if that cannot be established.

    The pid and the group id are both recyclable: once a process is reaped the
    kernel is free to hand its numbers to anything, so a reader that has only
    those two integers cannot tell this child from a stranger that arrived
    later. The start time is what binds them to one process, and it is readable
    only here, while the child is known to exist.

    None is not a claim. It means the probe found no process or errored, and a
    consumer must treat it as "no identity was captured" rather than as a
    statement about the child.
    """
    import psutil

    try:
        proc = psutil.Process(pid)
        if proc.status() == psutil.STATUS_ZOMBIE:
            return None
        return proc.create_time()
    except (psutil.Error, OSError):
        return None


@dataclass(frozen=True)
class SpawnedProcess:
    """The identity of a CLI child, as read at the moment it came into being.

    ``pid`` and ``pgid`` are the handles a later sweep signals. ``create_time``
    is what makes them an identity rather than two integers the OS may have
    reissued in the meantime, so a consumer that intends to act on this record
    later must compare a live start-time read against it and refuse to signal
    when they disagree or when this field is None.

    The group is the initial one, and a process group is not a containment
    boundary: a child or descendant that calls ``setsid()`` leaves it, and this
    record then says nothing about that process. Callers who need "nothing the
    leg started survives" must either require non-daemonizing CLIs or use a
    platform containment primitive; the group alone will not give it to them.
    """

    pid: int
    pgid: int
    create_time: float | None


class RedactedEnv(dict):
    """A child environment that does not print itself.

    ``repr=False`` on the field keeps the mapping out of a request's own
    representation, and that is not the whole channel: pydantic renders the
    input of a failing model-level validator verbatim, so a request rejected
    for an unrelated reason — an empty prompt, say — prints every variable
    beside it. Carrying the redaction on the value rather than at each error
    site is what stops a validator added later from reopening the leak by not
    knowing about it.

    It is a real mapping in every other respect, so nothing downstream has to
    know it exists.
    """

    def __repr__(self) -> str:
        return f"<env: {len(self)} variable(s)>"

    __str__ = __repr__


def raise_if_env_is_not_a_string_map(value: Mapping) -> None:
    """Reject a malformed environment without quoting anything out of it.

    A string key is a variable NAME and naming it is what makes the error
    actionable. A key of any other type is not a name, and printing it prints
    whatever the caller put there — a tuple holding a token, for instance — so
    those are reported by position and type only. Values are never printed at
    all, whatever their type.

    Raises TypeError rather than ValueError because pydantic converts
    ValueError into a validation error that quotes the entire rejected input,
    and lets anything else through untouched.
    """
    named: list[str] = []
    unnamed: list[str] = []
    for index, (key, val) in enumerate(value.items()):
        if isinstance(key, str):
            if not isinstance(val, str):
                named.append(f"{key!r} (value is {type(val).__name__})")
        else:
            unnamed.append(f"entry {index} (key is {type(key).__name__})")
    if named or unnamed:
        raise TypeError(
            "env must map strings to strings; these entries do not: "
            + ", ".join([*sorted(named), *unnamed])
        )


def redact_runtime_fields_in_place(data) -> None:
    """Make the runtime-only values in a raw request mapping unprintable.

    Called at the top of every model-level ``mode="before"`` validator, because
    that is the one place a validator holds the WHOLE raw input: pydantic
    renders the input of a failing validator verbatim, so a request rejected
    for an unrelated reason prints the child environment beside the reason.
    ``exclude`` and ``repr=False`` do not reach this channel — they govern the
    model, and this runs before a model exists.

    In place, and only substituting a mapping for an equal one that prints
    differently, so nothing downstream sees a different value.
    """
    if not isinstance(data, dict):
        return
    env = data.get("env")
    if isinstance(env, Mapping) and not isinstance(env, RedactedEnv):
        data["env"] = RedactedEnv(env)


def _kill_abandoned_spawn(task: asyncio.Future) -> None:
    """End the group of a child whose creator was cancelled before it resumed.

    Runs as a done-callback because by then nothing is left to await from: the
    coroutine that asked for the child has already unwound. The exception is
    retrieved either way, or asyncio reports it as never-retrieved at exit and
    a cancelled spawn starts looking like a defect in the spawn.
    """
    if task.cancelled():
        return
    if task.exception() is not None:
        return
    proc = task.result()
    kill_group_now(getattr(proc, "pid", None))


async def end_child_group(proc: Any, *, grace: float = 5.0) -> None:
    """End every member of the child's group, and survive being cancelled.

    Two things this does that awaiting the graceful helper alone does not.

    It drains the GROUP rather than the process. The graceful helper returns as
    soon as the process it holds a handle to is gone, and a descendant that
    ignores SIGTERM outlives a parent that does not — so the group is read
    afterwards and killed if anyone is still in it. A group that answers with
    members is still the group whose id was recorded, because a group id is not
    reissued while it has members.

    It cannot be interrupted into leaving something running. The graceful pass
    waits out a grace period, and that wait is a cancellation point that a
    runner being torn down is exactly where it meets. So a synchronous kill
    runs in a ``finally`` when that pass did not finish: no await, so nothing
    can interpose.

    Both kills fire only on positive evidence that the group id is still this
    child's, because the other direction is worse than an orphan. After a
    completed graceful pass the child has been waited, so its pid — and hence
    this group id — may already belong to someone else; the evidence there is
    a live member, since an occupied group is never reissued. On the cancelled
    path the child has not been waited at all, so the id is unambiguously ours.

    What it therefore cannot close, rather than papering over it: a scan that
    could not read the whole process table and saw no members leaves emptiness
    unproved, and this refuses to signal on that, because an unprovable group
    and a reissued one look the same from here.
    """
    pgid = getattr(proc, "pid", None)
    graceful_done = False
    try:
        await aterminate_process_group(proc, grace=grace)
        graceful_done = True
        if isinstance(pgid, int):
            members, _complete = group_member_pids(pgid)
            if members:
                kill_group_now(pgid)
    finally:
        if not graceful_done and getattr(proc, "returncode", None) is None:
            kill_group_now(pgid)


def observe_spawned(pid: int) -> SpawnedProcess:
    """Read pid, group and start time as one observation of one process.

    The group and the start time are two facts read separately by pid, and a
    pid the OS reassigns between them answers the later read as the replacement
    process. A record assembled from those two answers would describe no
    process that ever existed, so the group read is bracketed by the start
    time: read before, read again after, required to be unchanged. A failed
    bracket yields ``create_time=None``, which already means "no identity was
    captured" and is what stops a consumer from signalling on it.

    The bracket rejects a replacement arriving DURING the observation. It
    cannot speak for one that arrived before the first read, and the window
    where that is possible is a child that exits and is reaped between the
    spawn call returning and the first probe. What covers that window is the
    probe itself: a reaped pid holds no process and an exited-not-yet-reaped
    one is a zombie, and :func:`spawned_create_time` answers None to both.
    """
    created = spawned_create_time(pid)
    pgid = spawned_pgid(pid)
    if created is not None and spawned_create_time(pid) != created:
        created = None
    return SpawnedProcess(pid=pid, pgid=pgid, create_time=created)


async def ndjson_from_cli(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdin: Any = asyncio.subprocess.DEVNULL,
    stdin_data: str | bytes | None = None,
    tail_repair: Callable[[str], dict | None] | None = None,
    on_spawn: Callable[[SpawnedProcess], None | Awaitable[None]] | None = None,
) -> AsyncIterator[dict]:
    """Yield dicts from an NDJSON-emitting CLI subprocess; tail_repair handles malformed final chunks.

    ``stdin_data`` feeds text to the child over a pipe instead of the command
    line, which is how an arbitrarily large prompt is delivered without
    hitting the OS argument-length limit. It overrides ``stdin``: the child
    always gets a pipe, the data is written by a task that runs concurrently
    with the stdout/stderr readers below (a sequential write would deadlock as
    soon as the data exceeds the pipe buffer and the child is blocked writing
    output nobody is draining), and the pipe is closed afterwards so the child
    sees EOF rather than waiting forever for more input.

    ``on_spawn`` is called once with a :class:`SpawnedProcess` immediately
    after the child exists, for a caller that must record the process identity
    of a process it did not itself spawn. It may be a coroutine function; the
    result is awaited before the first byte of output is read, so a recorder
    that writes durably has finished writing before anything can consume the
    stream. Its failure is deliberately not swallowed: a caller whose recording
    fails has no record of a child that is now running, and continuing would
    leave a live process outside whatever domain the record defines. The
    exception propagates through the teardown below, which ends the group it
    was called for.
    """
    kwargs: dict[str, Any] = dict(
        cwd=str(cwd) if cwd else None,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    if stdin_data is not None:
        kwargs["stdin"] = asyncio.subprocess.PIPE
    elif stdin is not _INHERIT_STDIN:
        kwargs["stdin"] = stdin
    # Shielded, because the OS has already started the child by the time this
    # await resumes and a cancellation landing in the window before it returns
    # would abandon that child with no handle to sweep it by — unrecorded and
    # in a group nobody knows the id of. The shield keeps the creation running
    # so the handle still arrives, and the done-callback ends its group from
    # outside this coroutine, which is the only place left that can.
    spawn = asyncio.ensure_future(asyncio.create_subprocess_exec(*cmd, **kwargs))
    try:
        proc = await asyncio.shield(spawn)
    except BaseException:
        spawn.add_done_callback(_kill_abandoned_spawn)
        raise

    if on_spawn is not None:
        # Read the identity here, not at teardown: once the child is reaped its
        # pid and group id are both recyclable, so either read then can resolve
        # to a stranger's, and the start time that would have told them apart is
        # readable only while the process is alive. See docs/internals/runtime.md.
        try:
            await maybe_await(on_spawn(observe_spawned(proc.pid)))
        except BaseException:
            await end_child_group(proc)
            raise

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

    async def _write_stdin(payload: bytes) -> None:
        if proc.stdin is None:
            return
        try:
            proc.stdin.write(payload)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            # The child exited or closed its end before consuming everything;
            # its exit status is the real signal, so don't mask it here.
            log.debug("stdin write ended early: %s", exc)
        except Exception as exc:  # noqa: BLE001
            log.debug("stdin write failed: %s", exc)
        finally:
            # Without this close the child waits for an EOF that never comes.
            with contextlib.suppress(Exception):
                proc.stdin.close()

    stdin_task: asyncio.Task | None = None
    if stdin_data is not None:
        payload = stdin_data.encode() if isinstance(stdin_data, str) else stdin_data
        stdin_task = asyncio.create_task(_write_stdin(payload))

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
        await end_child_group(proc)

        # Reap the helper tasks — contextlib.suppress(Exception) does NOT
        # catch CancelledError (BaseException), so we suppress it explicitly.
        for task in (stderr_task, stdin_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: S110, BLE001
                pass


def resolve_cli_workspace(repo: Path | None, workspace: str | None) -> Path:
    if repo is None:
        repo = Path.cwd()
    # Fail here, before any caller spawns into a nonexistent cwd — every
    # CLI-backed provider's spawn path shares this helper.
    if not repo.is_dir():
        raise ValueError(f"cwd does not exist or is not a directory: {repo}")
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
    redact_runtime_fields_in_place(data)
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
