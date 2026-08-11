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

from ._secret_resolution import fill_declared_secrets

log = logging.getLogger(__name__)

# Sentinel that means "do not pass stdin to create_subprocess_exec at all"
# (inherits the parent process stdin, matching the old Gemini/Pi behaviour).
_INHERIT_STDIN = object()


def spawned_pgid(pid: int) -> int:
    """The process group of a just-spawned child.

    Falls back to the child's own pid: every spawn here uses
    ``start_new_session``, so the child leads its own group and that pid IS
    the group id whenever the read fails because the child has already
    exited.
    """
    try:
        return os.getpgid(pid)
    except OSError:
        return pid


def spawned_create_time(pid: int) -> float | None:
    """When the process at *pid* started, or None if that cannot be established.

    None means "no identity was captured," not a statement about the child.
    See docs/internals/providers.md#cli-subprocess-lifecycle.
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

    A consumer acting on this later must re-verify ``create_time`` against a
    live read and refuse to signal when they disagree or it is None — pid and
    pgid are both recyclable. The group is the initial one and is not a
    containment boundary: a descendant that calls ``setsid()`` leaves it.
    See docs/internals/providers.md#cli-subprocess-lifecycle.
    """

    pid: int
    pgid: int
    create_time: float | None


class Redacted:
    """A runtime-only value, wrapped so that nothing can print or serialize it.

    Deliberately not a mapping, so ``err.json()`` (which walks a structure)
    has nothing to walk, same as ``repr()``-based channels.
    See docs/internals/providers.md#cli-subprocess-lifecycle.
    """

    __slots__ = ("_value", "_label")

    def __init__(self, value, label: str) -> None:
        self._value = value
        self._label = label

    def reveal(self):
        return self._value

    def __repr__(self) -> str:
        if isinstance(self._value, Mapping):
            return f"<{self._label}: {len(self._value)} variable(s)>"
        return f"<{self._label}: redacted>"

    __str__ = __repr__


def raise_if_env_is_not_a_string_map(value: Mapping) -> None:
    """Reject a malformed environment without quoting anything out of it.

    Values are never printed, whatever their type; non-string keys are
    reported by position and type only, since printing one could print a
    credential the caller embedded there. Raises TypeError, not ValueError —
    pydantic quotes a ValueError's rejected input verbatim into the error.
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
    """Wrap ``env``/``on_spawn`` in a raw request mapping so nothing can print
    or serialize them. Called at the top of every model-level
    ``mode="before"`` validator, the one place that holds pydantic's WHOLE raw
    input on a failing validation. Substitution must happen in place, since
    pydantic keeps the object passed INTO the failing validator on the error —
    handing back a sanitized copy changes nothing about what the error holds.
    An immutable mapping is therefore refused (TypeError, not ValueError —
    pydantic quotes a ValueError's input verbatim), not silently skipped.
    See docs/internals/providers.md#cli-subprocess-lifecycle.
    """
    if not isinstance(data, Mapping):
        return
    present = [
        name
        for name in ("env", "on_spawn")
        if data.get(name) is not None and not isinstance(data.get(name), Redacted)
    ]
    if not present:
        return
    try:
        for name in present:
            data[name] = Redacted(data[name], name)
    except TypeError:
        raise TypeError(
            f"{type(data).__name__} is read-only, so the runtime-only field(s) "
            f"{', '.join(present)} cannot be replaced before validation. These carry a "
            "child environment and a spawn callback, and a validation error would "
            "render the mapping verbatim. Pass a mutable mapping."
        ) from None


def _kill_abandoned_spawn(task: asyncio.Future) -> None:
    """End the group of a child nobody is left to receive.

    Runs as a done-callback since the awaiting coroutine has already unwound
    by the time this fires. A cancellation landing inside the creation call
    is a known, unclosed hole (logged, not silently passed over): the child
    exists but its pid was never returned to this process, so only its
    direct-child transport gets closed, never its group.
    See docs/internals/providers.md#cli-subprocess-lifecycle.
    """
    if task.cancelled():
        log.warning(
            "the spawn task was cancelled before it produced a handle. If the OS had "
            "already created the child, nothing in this process can reach it: the pid "
            "was never returned to anyone. asyncio closes the transport on this path, "
            "which ends the direct child but not the group it leads"
        )
        return
    if task.exception() is not None:
        return
    # Not a raw kill: a spawn that completed may ALSO have been reaped by the
    # time this callback runs, and a reaped pid names whatever now holds it.
    _end_group_with_evidence(task.result())


def _end_group_with_evidence(proc: Any) -> str:
    """End a child's group wherever its identity can be established: while
    unreaped (pid can't have been reissued, no scan needed) or, once reaped,
    only if a live member still pins the group id.
    See docs/internals/providers.md#cli-subprocess-lifecycle.
    """
    pgid = getattr(proc, "pid", None)
    if getattr(proc, "returncode", None) is None:
        return "killed-unreaped" if kill_group_now(pgid) else "no-group"
    return _kill_group_if_occupied(pgid)


def _kill_group_if_occupied(pgid: Any) -> str:
    """End a process group if it can be shown to still hold someone.

    Returns ``killed``/``empty``/``unproven``/``no-group`` for the log, not
    for caller branching. ``unproven`` is the case that matters: a process
    table read that failed and showed no members is not an empty group, and
    is the only outcome where something may still be running unaddressed.
    """
    if not isinstance(pgid, int):
        return "no-group"
    members, complete = group_member_pids(pgid)
    if members:
        kill_group_now(pgid)
        return "killed"
    if not complete:
        log.warning(
            "process group %s could not be read completely and showed no members; "
            "leaving it alone rather than signalling a possibly reissued group id",
            pgid,
        )
        return "unproven"
    return "empty"


async def end_child_group(proc: Any, *, grace: float = 5.0) -> None:
    """End every member of the child's group, and survive being cancelled.

    Drains the GROUP, not just the process — a descendant ignoring SIGTERM can
    outlive a parent that doesn't, so the group is read afterwards and killed
    if still occupied. Escalation is keyed on that membership evidence, not on
    whether the direct child died, since those are different facts. The
    graceful helper (reached only on the not-yet-waited path, since it signals
    the group id unconditionally) is backed by a synchronous kill in a
    ``finally`` so a second cancellation can't interpose.
    See docs/internals/providers.md#cli-subprocess-lifecycle.
    """
    swept = False
    try:
        if getattr(proc, "returncode", None) is None:
            await aterminate_process_group(proc, grace=grace)
        _end_group_with_evidence(proc)
        swept = True
    finally:
        # Synchronous, so a second cancellation cannot interpose, and keyed on
        # the same membership evidence as the pass it is backing up rather than
        # on anything about the direct child.
        if not swept:
            _end_group_with_evidence(proc)


# How long the stream keeps draining after the CLI child itself has exited.
# Real trailing output arrives within moments of exit; what arrives after this
# window is an orphaned descendant holding the inherited pipe open, and waiting
# on it converts a finished leg into an unbounded wall-clock burn.
_POST_EXIT_DRAIN_GRACE = 10.0


def observe_spawned(pid: int) -> SpawnedProcess:
    """Read pid, group and start time as one observation of one process.

    The group read is bracketed by a start-time read before and after,
    required unchanged, since the pid could otherwise be reassigned mid-read
    and the two facts would describe a process that never existed. A failed
    bracket yields ``create_time=None`` ("no identity captured"). A pid
    reassigned before the first probe is separately covered:
    :func:`spawned_create_time` returns None for both a reaped pid and a
    zombie.
    """
    created = spawned_create_time(pid)
    pgid = spawned_pgid(pid)
    if created is not None and spawned_create_time(pid) != created:
        created = None
    return SpawnedProcess(pid=pid, pgid=pgid, create_time=created)


def _no_stderr_reason(
    rc: int,
    unavailable: str | None,
    drain_error: str | None,
) -> str:
    """Why a nonzero exit came with no stderr to quote.

    Distinguishes a child that failed silently from a capture that failed to
    read it — the caller acts on those differently, and collapsing them makes
    a broken instrument read like a quiet subprocess. The drain error
    contributes only its exception type, never its message, which can embed
    the bytes being read when it raised.
    """
    exited = f"CLI subprocess exited with code {rc}"
    if drain_error is not None:
        return f"{exited}; reading its stderr failed with {drain_error}, so no output was captured"
    if unavailable is not None:
        return f"{exited} and {unavailable}"
    return f"{exited} and wrote nothing to stderr"


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
    """Yield dicts from an NDJSON-emitting CLI subprocess; tail_repair handles
    malformed final chunks. ``stdin_data`` overrides ``stdin`` and is written
    concurrently with the stdout/stderr readers below, then closed so the
    child sees EOF. ``on_spawn`` is awaited (may be a coroutine function)
    before any output is read, and its failure is not swallowed — it
    propagates through the teardown below, which ends the child's group.
    See docs/internals/providers.md#cli-subprocess-lifecycle.
    """
    # Every CLI provider spawns through here, so a secret the child must read
    # from its own environment is filled in one place rather than four. Purely
    # additive: with nothing configured this returns ``env`` unchanged, and a
    # lookup that fails leaves the child to fail the way it already failed.
    child_env = await fill_declared_secrets(env)
    kwargs: dict[str, Any] = dict(
        cwd=str(cwd) if cwd else None,
        env=dict(child_env) if child_env is not None else None,
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
    # Why the drain records how it ended: an empty `stderr_chunks` is produced
    # by a child that said nothing, by a child whose stderr was never opened,
    # and by a drain that raised — and the caller below turns emptiness into
    # the message a human reads. Without this the three arrive as one.
    stderr_unavailable: str | None = None
    stderr_drain_error: str | None = None

    async def _drain_stderr() -> None:
        nonlocal stderr_total, stderr_unavailable, stderr_drain_error
        if proc.stderr is None:
            stderr_unavailable = "its stderr was never opened"
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
            # Type only. The exception's message can embed the bytes it was
            # reading, and this string is going into an error a caller may
            # store or send on.
            stderr_drain_error = type(exc).__name__
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

    # Guards against orphan-held pipes leaving stdout open forever; see
    # docs/internals/providers.md#cli-subprocess-lifecycle.
    async def _await_child_exit() -> int:
        # wait() alone can block on an orphan-held pipe on newer Pythons, so it
        # races a returncode poll; wait()'s result wins when it completes, since
        # it's the canonical exit code (and the only one a test double carries).
        wait_task = asyncio.create_task(proc.wait())
        try:
            while proc.returncode is None and not wait_task.done():
                await asyncio.wait({wait_task}, timeout=0.05)
            if not wait_task.done():
                await asyncio.wait({wait_task}, timeout=0.05)
            if wait_task.done():
                return wait_task.result()
            return proc.returncode
        finally:
            if not wait_task.done():
                wait_task.cancel()
                try:
                    await wait_task
                except (asyncio.CancelledError, Exception):  # noqa: S110, BLE001
                    pass

    exit_task = asyncio.create_task(_await_child_exit())
    read_task: asyncio.Task | None = None
    child_exited = False

    async def _read_next() -> bytes:
        nonlocal read_task, child_exited
        read_task = asyncio.create_task(proc.stdout.read(4096))
        if not child_exited:
            await asyncio.wait({read_task, exit_task}, return_when=asyncio.FIRST_COMPLETED)
            child_exited = exit_task.done()
        if child_exited and not read_task.done():
            # Grace is per read, not a shared deadline, so buffered output
            # survives a slow consumer; only a read producing nothing for the
            # whole grace ends the stream.
            try:
                return await asyncio.wait_for(read_task, _POST_EXIT_DRAIN_GRACE)
            except asyncio.TimeoutError:
                log.warning(
                    "CLI child (pid %s) exited but stdout stayed open — orphaned "
                    "descendants hold the pipe; ending the stream after %.0fs grace",
                    proc.pid,
                    _POST_EXIT_DRAIN_GRACE,
                )
                return b""
        return await read_task

    try:
        while True:
            chunk = await _read_next()
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

        # Same pipe-gating hazard as above: proc.wait() here would block on the
        # orphans' copy of the pipe after a grace-bounded end of stream.
        rc = await asyncio.shield(exit_task)
        if rc != 0:
            drain_truncated = False
            try:
                await asyncio.wait_for(asyncio.shield(stderr_task), timeout=2.0)
            except asyncio.TimeoutError:
                drain_truncated = True
            except asyncio.CancelledError:
                raise
            err = b"".join(stderr_chunks).decode(errors="replace").strip()
            # Emptiness decided on what was captured, before the drain note is
            # appended, so a truncated drain that captured nothing still gets
            # a message instead of the note masquerading as output.
            if not err:
                err = _no_stderr_reason(rc, stderr_unavailable, stderr_drain_error)
            if drain_truncated:
                err += " [stderr drain timed out]"
            raise RuntimeError(err)

    finally:
        await end_child_group(proc)

        # Reap the helper tasks — contextlib.suppress(Exception) does NOT
        # catch CancelledError (BaseException), so we suppress it explicitly.
        for task in (stderr_task, stdin_task, exit_task, read_task):
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
