# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import logging
import math
import os
import re
import shutil
import signal
import socket
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from lionagi._cli_worker_context import _CliWorkerAdmission, _get_cli_worker_recorder
from lionagi.libs.path_safety import contain_and_resolve, has_traversal
from lionagi.libs.schema.as_readable import as_readable
from lionagi.ln._proc import (
    _await_child_exit as _wait_owned_child_exit,
)
from lionagi.ln._proc import (
    _group_has_members,
    aterminate_process_group,
    capture_group_witnesses,
    observe_process_group_leader,
    prepare_process_group_env,
    raw_create_time,
    signal_group_if_witnessed,
)
from lionagi.ln.concurrency.cancel import move_on_after
from lionagi.ln.concurrency.utils import maybe_await

from ._provider_errors import ProviderTeardownError, _WorkerSpawnAdmissionError
from ._secret_resolution import fill_declared_secrets_and_names
from ._subagent_admission import (
    AGENT_DEPTH_ENV,
    DEFAULT_WORKLOAD,
    admit_subagent,
    child_agent_depth,
    refuse_nested_agent,
)

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
        return raw_create_time(proc)
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
    # Runtime-only proof used by teardown after the leader has been reaped.
    # Hidden from the callback's representation and equality so the public
    # identity remains pid/pgid/start-time.
    _group_marker: str | None = field(default=None, repr=False, compare=False)


class _ChildGroupCleanup(Enum):
    CONFIRMED_EMPTY = "confirmed-empty"
    UNCONFIRMED = "unconfirmed"


_RETAINED_CLI_TASKS: set[asyncio.Future] = set()


def _forget_cli_task(task: asyncio.Future) -> None:
    _RETAINED_CLI_TASKS.discard(task)
    if not task.cancelled():
        task.exception()


def _retain_cli_task(task: asyncio.Future, admission: _CliWorkerAdmission | None) -> None:
    # The event loop alone holds weak task references. This also owns cleanup
    # for an explicitly unpersisted launch, which has no durable owner token.
    _RETAINED_CLI_TASKS.add(task)
    task.add_done_callback(_forget_cli_task)
    if admission is not None:
        admission.retain(task)


def _group_cleanup_outcome(proc: Any, spawned: SpawnedProcess | None = None) -> _ChildGroupCleanup:
    """Read absence for an owned own-session child; never authorize a signal."""
    pid = getattr(proc, "pid", None)
    if type(pid) is not int or pid <= 1 or type(getattr(proc, "returncode", None)) is not int:
        return _ChildGroupCleanup.UNCONFIRMED
    if spawned is not None and (spawned.pid != pid or spawned.pgid != pid):
        return _ChildGroupCleanup.UNCONFIRMED
    try:
        absent = not _group_has_members(pid)
    except Exception:
        return _ChildGroupCleanup.UNCONFIRMED
    return _ChildGroupCleanup.CONFIRMED_EMPTY if absent else _ChildGroupCleanup.UNCONFIRMED


def _finish_child_lease(
    lease: Any, outcome: _ChildGroupCleanup, *, launch_outcome: bool = False
) -> None:
    if outcome is _ChildGroupCleanup.CONFIRMED_EMPTY:
        lease.release()
    elif lease.slots:
        # Unlocking a child-only record is unsafe once that child exits: a
        # descendant may remain, while the next admission considers it free.
        # Keep the existing raw-fd holder locks; only holder exit drops them.
        pools = ", ".join(sorted({getattr(slot, "pool", "unknown") for slot in lease.slots}))
        label = (
            "CLI launch outcome unconfirmed"
            if launch_outcome
            else "CLI child group cleanup remains unconfirmed"
        )
        log.warning(
            "%s; its subagent slots stay held until this process exits (pools: %s)", label, pools
        )
    else:
        label = (
            "CLI launch outcome unconfirmed"
            if launch_outcome
            else "CLI child group cleanup remains unconfirmed"
        )
        log.warning("%s; no capped pools are held", label)


def _require_cli_executable(cmd: list[str], *, cwd: Path | None, env: Mapping[str, str]) -> None:
    """Refuse an unresolved executable before attempting an OS launch."""
    try:
        if not cmd or not isinstance(cmd[0], str) or not cmd[0]:
            raise ValueError("a CLI executable is required")
        program = cmd[0]
        child_cwd = None

        def child_path(value: str) -> str:
            nonlocal child_cwd
            if os.path.isabs(value):
                return value
            if child_cwd is None:
                child_cwd = os.path.realpath(os.fspath(cwd) if cwd is not None else os.getcwd())
            return os.path.join(child_cwd, value)

        if os.path.dirname(program):
            candidate = child_path(program)
            available = (
                os.path.exists(candidate)
                and os.access(candidate, os.X_OK)
                and not os.path.isdir(candidate)
            )
        else:
            path = env.get("PATH", os.defpath)
            # Empty PATH components name the child's cwd, including PATH=''.
            # Passing absolute components to which also avoids the parent's cwd.
            search = os.pathsep.join(child_path(part) for part in path.split(os.pathsep))
            available = shutil.which(program, mode=os.X_OK, path=search) is not None
        if not available:
            raise ValueError("the CLI executable is unavailable")
    except Exception as exc:
        raise _WorkerSpawnAdmissionError("CLI executable is unavailable before launch") from exc


def _worker_spawn_identity(spawned: SpawnedProcess) -> dict[str, object]:
    try:
        import psutil

        host = socket.gethostname()
        boot = psutil.boot_time()
        created = spawned.create_time
        if (
            type(spawned.pid) is not int
            or spawned.pid <= 1
            or type(spawned.pgid) is not int
            or spawned.pgid != spawned.pid
            or type(created) not in (int, float)
            or not math.isfinite(created)
            or type(boot) not in (int, float)
            or not math.isfinite(boot)
            or not isinstance(host, str)
            or not host.strip()
        ):
            raise ValueError("incomplete local worker identity")
        return {
            "pid": spawned.pid,
            "pgid": spawned.pgid,
            "pid_create_time": float(created),
            "pid_host": host,
            "pid_boot_time": float(boot),
            "process_identity_mode": "local",
        }
    except Exception as exc:
        raise _WorkerSpawnAdmissionError("local worker spawn identity could not be read") from exc


# Engine runs bind this observer only while creating their run task. Context
# propagation then associates every descendant CLI spawn with that one run,
# including providers whose public request model has no ``on_spawn`` field.
# The process handle stays runtime-only and never crosses the public callback.
_RUN_CLI_SPAWN_OBSERVER: ContextVar[
    Callable[[Any, SpawnedProcess], None | Awaitable[None]] | None
] = ContextVar("lionagi_run_cli_spawn_observer", default=None)


@contextlib.contextmanager
def _observe_run_cli_spawns(
    observer: Callable[[Any, SpawnedProcess], None | Awaitable[None]],
):
    """Bind a run-local CLI spawn observer for tasks created in this context."""

    token = _RUN_CLI_SPAWN_OBSERVER.set(observer)
    try:
        yield
    finally:
        _RUN_CLI_SPAWN_OBSERVER.reset(token)


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


def reveal_redacted(value):
    """The value inside a ``Redacted`` wrapper, or the value unchanged.

    ``redact_runtime_fields_in_place`` runs at the TOP of every model-level
    ``mode="before"`` validator, so any validator after the first one sees the
    wrapper where the caller passed a mapping. ``Redacted`` is deliberately not
    a mapping, so a reader that forgets this gets a silent miss rather than an
    error — a validator asking ``isinstance(env, Mapping)`` simply decides the
    caller supplied nothing. Code that needs to READ such a field goes through
    here; nothing revealed this way may be printed, logged, or serialized.
    """
    return value.reveal() if isinstance(value, Redacted) else value


def raise_if_env_is_not_a_string_map(value: Mapping) -> None:
    """Reject a malformed environment without quoting anything out of it.

    Nothing from the mapping is printed: each bad entry is reported by its
    position and the type that is wrong. A key can carry a credential as easily
    as a value (a token pasted where a name belongs, ``NAME=value`` written as a
    name), and no test of a key's shape tells the two apart. Raises TypeError,
    not ValueError — pydantic quotes a ValueError's rejected input verbatim into
    the error.
    """
    bad: list[str] = []
    for index, (key, val) in enumerate(value.items()):
        # `issubclass(type(x), str)`, never `isinstance`: a type whose `__class__` merely
        # answers `str` passes `isinstance` and is then rendered by pydantic, which
        # quotes the value.
        if not issubclass(type(key), str):
            bad.append(f"entry {index} (key is {type(key).__name__})")
        elif not issubclass(type(val), str):
            bad.append(f"entry {index} (value is {type(val).__name__})")
    if bad:
        raise TypeError("env must map strings to strings; these entries do not: " + ", ".join(bad))
    unencodable = unencodable_env_entry(value)
    if unencodable is not None:
        raise TypeError(_unencodable_env_message(unencodable))


def unencodable_env_entry(env: Mapping[str, str]) -> str | None:
    """The first key or value of a string environment the spawn cannot encode, named by
    its position, else None.

    On POSIX a surrogate escape (U+DC80 to U+DCFF) passes as the byte it stands for and any
    other lone surrogate fails. The codec's error holds the whole key or value, which can be a
    credential, so only the position is returned, and the caller raises its refusal after
    this handler has ended.

    The text is read as ``env_as_the_spawn_encodes_it`` reads it: ``os.fsencode`` calls a
    ``str`` subclass's own ``encode`` and answers with whatever it returns, so a ``bytes``
    answer is taken through the base type's own slice, decoded, reduced to a plain ``str``
    and encoded again here. The bytes that answer for the entry are then the bytes the spawn
    would send, not the first encoder's own answer. An encoder that raises anything at all —
    an answer that is not text or bytes, a path-like read that fails, a value whose own
    method raises — is refused by position and type with ``TypeError``; the error it raised
    is neither kept as the refusal's context nor reachable through its message or ``args``.
    """
    for index, (key, val) in enumerate(env.items()):
        for part, item in (("key", key), ("value", val)):
            unreadable = False
            try:
                encoded = os.fsencode(item)
                if isinstance(encoded, bytes):
                    encoded = bytes.__getitem__(encoded, slice(None))
                os.fsencode(str.__str__(os.fsdecode(encoded)))
            except UnicodeEncodeError:
                return f"The environment entry {index} {part}"
            except Exception:
                # Any other exception an encoder raises is refused by position and type.
                # The value's own text is not carried: the exception that was caught may
                # hold it in its message or its args, and raising inside this handler
                # would chain the refusal to it.
                unreadable = True
            if unreadable:
                # Raised after the handler, so the refusal keeps no error as its context;
                # the wording is the spawn copy's own refusal for the same entry.
                raise TypeError(
                    f"The environment entry {index} {part}, of type {type(item).__name__}, "
                    "could not be read as text, so it cannot be passed to a CLI process."
                )
    return None


def env_as_the_spawn_encodes_it(env: Mapping) -> dict[str, str]:
    """``env`` copied once, each key and value read once and kept as the text of the bytes
    the spawn passes.

    The spawn builds the process environment from a copy of the mapping
    (``prepare_process_group_env``), and on POSIX the subprocess module encodes every key and
    value of that copy with ``os.fsencode``. Doing both here, once, and handing this copy to
    the spawn means every entry the caller passes is read and checked here once, and the spawn
    is handed that read, whatever a view of the mapping or a path-like value answers on a
    later read. The secret fill and the process-group marker add or replace entries after this
    check; both are text the codec cannot refuse, so neither can fail it. On POSIX a surrogate
    escape (U+DC80 to U+DCFF) passes as the byte it stands for, so the spawn encodes the copy
    to the same bytes.

    A key or value that cannot be encoded, or whose read raises an ``Exception``, is refused by
    its position: the codec's error, or the error a path-like value raises, can hold the whole
    key or value, which can be a credential, so the refusal is raised after that handler has
    ended and carries no part of it. A read that raised ``TypeError`` is refused with
    ``TypeError``, any other with ``ValueError``. A value that is ``bytes`` is read as the
    bytes it holds, taken through the base ``bytes`` type's own slice, so a subclass's own
    ``decode``, ``__bytes__``, ``__getitem__``, ``__len__`` and ``__index__`` do not run and
    cannot change the text or fail the read. Two keys that encode to the same bytes name one
    variable: the pair is taken when their values are equal, since the process then has one
    well-defined value to read, and refused by position when they differ. An exception that
    is not an ``Exception``, or one the caller's own mapping raises while ``dict()`` copies
    it, propagates unchanged.
    """
    copied: dict[str, str] = {}
    first_index: dict[str, int] = {}
    for index, (key, val) in enumerate(dict(env).items()):
        texts: list[str] = []
        for part, item in (("key", key), ("value", val)):
            refusal = None
            refused_as: type[Exception] = ValueError
            try:
                # The spawn concatenates the encoded entries, so a bytes value is read as the
                # bytes it holds, taken through the base type's own slice: a subclass's own
                # ``decode``, ``__bytes__`` or ``__getitem__`` cannot change or fail the read.
                encoded = os.fsencode(item)
                if isinstance(encoded, bytes):
                    encoded = bytes.__getitem__(encoded, slice(None))
                # ``os.fsencode`` calls a str subclass's own ``encode`` and ``os.fsdecode``
                # hands its answer back unchecked, so the text is reduced to a plain ``str``
                # and encoded again here: the bytes checked are the bytes the spawn builds
                # from this copy, and an encoder that answers a lone surrogate is refused
                # here rather than failing the spawn's own encode with the value on it.
                text = str.__str__(os.fsdecode(encoded))
                os.fsencode(text)
                texts.append(text)
            except UnicodeEncodeError:
                refusal = _unencodable_env_message(f"The environment entry {index} {part}")
            except Exception as exc:
                if isinstance(exc, TypeError):
                    refused_as = TypeError
                refusal = (
                    f"The environment entry {index} {part}, of type {type(item).__name__}, "
                    "could not be read as text, so it cannot be passed to a CLI process."
                )
            if refusal is not None:
                raise refused_as(refusal)
        named_at = first_index.get(texts[0])
        if named_at is not None and copied[texts[0]] != texts[1]:
            raise ValueError(
                f"The environment entries {named_at} and {index} name the same "
                "variable once encoded, so they cannot be passed to a CLI process."
            )
        first_index.setdefault(texts[0], index)
        copied[texts[0]] = texts[1]
    return copied


def _unencodable_env_message(what: str) -> str:
    return (
        f"{what} holds a character the process environment cannot encode: a lone "
        "surrogate cannot be passed to a CLI process."
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


def _kill_abandoned_spawn(task: asyncio.Future) -> _ChildGroupCleanup:
    """End the group of a child nobody is left to receive.

    Used by retained late cleanup after the awaiting coroutine has unwound
    by the time this fires. The shield keeps the creation call running through
    a cancellation of its caller, so the handle still arrives here. A spawn
    task that is itself cancelled is a known, unclosed hole (logged, not
    silently passed over): the child may exist but its pid was never returned
    to this process, so only its direct-child transport gets closed, never its
    group. See docs/internals/providers.md#cli-subprocess-lifecycle.
    """
    if task.cancelled():
        log.warning(
            "the spawn task was cancelled before it produced a handle. If the OS had "
            "already created the child, nothing in this process can reach it: the pid "
            "was never returned to anyone. asyncio closes the transport on this path, "
            "which ends the direct child but not the group it leads"
        )
        return _ChildGroupCleanup.UNCONFIRMED
    if task.exception() is not None:
        return _ChildGroupCleanup.UNCONFIRMED
    # Identity was captured inside the spawn task, before its result became
    # visible to this potentially-late callback. Deriving it here would let a
    # reaped child's recycled pid describe a stranger.
    proc, spawned = task.result()
    witnesses = _witnesses_for_spawn(proc, spawned)
    if spawned._group_marker is None:
        _end_group_with_evidence(proc, witnesses)
    else:
        _end_group_with_evidence(
            proc,
            witnesses,
            expected_marker=spawned._group_marker,
        )
    return _group_cleanup_outcome(proc, spawned)


async def _cleanup_abandoned_spawn(
    task: asyncio.Future,
    lease: Any,
    admission: _CliWorkerAdmission | None,
    *,
    start_gate: asyncio.Event,
    entered: asyncio.Event,
) -> None:
    try:
        await start_gate.wait()
        entered.set()
        outcome = _kill_abandoned_spawn(task)
        if not task.cancelled() and task.exception() is None:
            proc, spawned = task.result()
            if outcome is not _ChildGroupCleanup.CONFIRMED_EMPTY:
                # The immediate witnessed kill above is the existing late-child
                # backstop. Reuse the bounded child-exit poll, not a pipe wait.
                with move_on_after(5.0):
                    await _wait_owned_child_exit(proc)
                outcome = _group_cleanup_outcome(proc, spawned)
        if outcome is _ChildGroupCleanup.CONFIRMED_EMPTY:
            _finish_child_lease(lease, outcome)
        else:
            raise ProviderTeardownError("abandoned CLI child group cleanup remains unconfirmed")
    except BaseException:
        # The original consumer already has its failure/cancellation. Keep its
        # lease and surface the separate bounded cleanup failure safely.
        _finish_child_lease(lease, _ChildGroupCleanup.UNCONFIRMED)
        log.warning("could not end the group of a child whose spawn was abandoned")
        raise
    finally:
        if admission is not None:
            admission.finish()


def _witnesses_for_spawn(proc: Any, spawned: SpawnedProcess) -> tuple[tuple[int, float], ...]:
    """Expand one spawn identity into frozen group-member witnesses."""
    pid = getattr(proc, "pid", None)
    if spawned.create_time is None or spawned.pid != pid or spawned.pgid != pid:
        return ()
    if spawned._group_marker is None:
        return capture_group_witnesses(
            proc,
            expected_leader_created=spawned.create_time,
        )
    return capture_group_witnesses(
        proc,
        expected_leader_created=spawned.create_time,
        expected_marker=spawned._group_marker,
    )


def _end_group_with_evidence(
    proc: Any,
    witnesses: tuple[tuple[int, float], ...] = (),
    *,
    expected_marker: str | None = None,
) -> str:
    """Synchronously end a group only through frozen spawn-time evidence.

    ``returncode`` is deliberately irrelevant: a child watcher may already
    have reaped the pid before the event loop publishes its return code. A
    matching start-time witness is the only group-wide authority here.
    See docs/internals/providers.md#cli-subprocess-lifecycle.

    ``expected_marker`` is accepted for call-site compatibility and is not
    read. The marker is verified where the witnesses are captured, so by the
    time it reaches here the witness set either proves the group is ours or
    nothing does; re-checking a name at signal time would add no authority.
    """
    pgid = getattr(proc, "pid", None)
    sigkill = getattr(signal, "SIGKILL", None)
    signalled = False
    try:
        if sigkill is not None:
            signalled = signal_group_if_witnessed(pgid, witnesses, sigkill)
    finally:
        # The child's own kill does not depend on the group signal succeeding.
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
    return "killed-witnessed" if signalled else "unproven"


_GROUP_EXIT_POLL_SECONDS = 0.02


async def end_child_group(
    proc: Any,
    *,
    grace: float = 5.0,
    spawned: SpawnedProcess | None = None,
) -> _ChildGroupCleanup:
    """End every member of the child's group, and survive being cancelled.

    Drains the GROUP, not just the process — a descendant ignoring SIGTERM can
    outlive a parent that doesn't. The graceful helper freezes member identities
    while the child still anchors its group, then revalidates a frozen witness
    before delayed escalation. After reaping, no fresh numeric scan may replace
    that evidence. A synchronous witness-gated kill in ``finally`` remains the
    backstop when a second cancellation interposes.
    See docs/internals/providers.md#cli-subprocess-lifecycle.
    """
    witnesses = _witnesses_for_spawn(proc, spawned) if spawned is not None else ()
    # The paired branches below look like duplication -- every callee here
    # IGNORES ``expected_marker``, since the marker is verified where the
    # witnesses are captured -- but omitting the keyword and passing it as None
    # are not the same call, and the difference is observable to anything that
    # substitutes one of these functions. Collapsing them breaks
    # test_graceful_teardown_does_not_repeat_a_numeric_sweep_after_reaping,
    # whose double declares only ``(child, *, grace, group_witnesses)`` on
    # purpose, so it pins the call shape as well as the behaviour. The
    # branching stays, and the parameter is documented as compatibility-only at
    # each definition rather than removed, which is the wider change.
    expected_marker = spawned._group_marker if spawned is not None else None
    swept = False
    try:
        if getattr(proc, "returncode", None) is None:
            if not witnesses:
                # The leader alone gets signalled below. Signalling the group
                # without a matching member identity could reach a recycled
                # PGID, so that stays refused; saying so is what keeps a
                # surviving descendant from passing as a clean teardown.
                log.warning(
                    "no member of process group %s could be confirmed as this run's, so "
                    "only the CLI child %s is signalled; other members of the group may "
                    "still be running",
                    spawned.pgid if spawned is not None else getattr(proc, "pid", None),
                    getattr(proc, "pid", None),
                )
            if expected_marker is None:
                await aterminate_process_group(
                    proc,
                    grace=grace,
                    group_witnesses=witnesses,
                )
            else:
                await aterminate_process_group(
                    proc,
                    grace=grace,
                    group_witnesses=witnesses,
                    expected_marker=expected_marker,
                )
            if witnesses:
                # The forced kill waits for the child alone. A member it killed
                # still answers the group probe until it is reaped, by init once
                # the child is gone, so absence is read after the same bounded
                # wait as below rather than at the instant the child exits.
                with move_on_after(grace):
                    while _group_has_members(proc.pid):
                        await asyncio.sleep(_GROUP_EXIT_POLL_SECONDS)
        elif witnesses:
            # The stream may have ended because the child exited while a
            # descendant kept stdout open. The leader is already reaped here,
            # so the marker-backed witness is the only safe group authority.
            if expected_marker is None:
                ended = _end_group_with_evidence(proc, witnesses)
            else:
                ended = _end_group_with_evidence(
                    proc,
                    witnesses,
                    expected_marker=expected_marker,
                )
            if ended == "killed-witnessed":
                # A member SIGKILL reached still answers the group probe until
                # it is reaped, so absence is read after a bounded wait rather
                # than at the instant of the signal.
                with move_on_after(grace):
                    while _group_has_members(proc.pid):
                        await asyncio.sleep(_GROUP_EXIT_POLL_SECONDS)
        swept = True
    finally:
        # Synchronous, so a second cancellation cannot interpose, and keyed on
        # the same membership evidence as the pass it is backing up rather than
        # on anything about the direct child.
        if not swept:
            if expected_marker is None:
                _end_group_with_evidence(proc, witnesses)
            else:
                _end_group_with_evidence(
                    proc,
                    witnesses,
                    expected_marker=expected_marker,
                )
    return _group_cleanup_outcome(proc, spawned)


# How long the stream keeps draining after the CLI child itself has exited.
# Real trailing output arrives within moments of exit; what arrives after this
# window is an orphaned descendant holding the inherited pipe open, and waiting
# on it converts a finished leg into an unbounded wall-clock burn.
_POST_EXIT_DRAIN_GRACE = 10.0


def observe_spawned(pid: int, *, expected_marker: str) -> SpawnedProcess:
    """Read pid, group, marker, and start time as one child observation.

    The group read is bracketed by a start-time read before and after,
    required unchanged, since the pid could otherwise be reassigned mid-read
    and the facts would describe a process that never existed. The unguessable
    marker is injected before spawn and closes the earlier window where a pid
    was already reassigned before the first start-time probe. Any failed or
    mismatched read yields ``create_time=None`` ("no identity captured").
    """
    created = observe_process_group_leader(
        pid,
        expected_marker=expected_marker,
    )
    pgid = spawned_pgid(pid)
    if created is not None and pgid != pid:
        created = None
    return SpawnedProcess(
        pid=pid,
        pgid=pgid,
        create_time=created,
        _group_marker=expected_marker if created is not None and pgid == pid else None,
    )


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


# Enough to carry a CLI's error banner without pasting a whole session into the
# log line; the cap is on what is logged, not on what was captured.
_ABANDONED_STDERR_LOG_CAP = 4096

# Backstop against a wedged reader draining a closed pipe to EOF, not a budget
# for the child to keep talking, so it is shorter than the exit-code path's wait.
_ABANDONED_STDERR_DRAIN_TIMEOUT = 0.5


# A credential reaches a child either from the environment we build for it or
# from its own config, so the log path strips both what we injected and what
# merely looks like a secret.
# "sig" is bounded because it is short enough to sit inside ordinary words
# (assignee, design, signal); the rest are long enough to stand alone.
_SECRET_ENV_KEY_RE = re.compile(
    r"(?i)key|token|secret|password|passwd|credential|auth|signature|bearer"
    r"|(?<![a-z])sig(?![a-z])"
)
_SECRET_SHAPE_RE = re.compile(
    r"(?i)\b(?:"
    r"Bearer\s+\S+"
    # To end of line, plus folded continuations: a scheme word ("Bearer x")
    # otherwise ends the match early, and a value wrapped onto an indented
    # continuation line leaves its tail behind.
    r"|(?:Authorization|X-Api-Key|Api-Key)\s*:[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*"
    r"|(?:sk|rk)-[a-z0-9_-]{8,}"
    r"|(?:ghp|gho|ghu|ghs|github_pat)_[a-z0-9_]{8,}"
    r"|xox[abprs]-[a-z0-9-]{8,}"
    r"|AKIA[0-9A-Z]{12,}"
    r"|eyJ[a-z0-9_-]{8,}\.[a-z0-9_-]+\.[a-z0-9_-]+"
    r")"
)
# A credential inside a connection string is invisible to both the name rule and
# the token shapes. Only the password is replaced, so the host stays diagnostic.
_URL_CREDENTIAL_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/?#@]*:)[^\s/?#@]+(@)")
# The same credential passed as a query parameter has no "@" and so is invisible
# to the rule above. Every parameter is matched and the name decides, so this
# reads the name vocabulary above rather than restating it.
_URL_QUERY_PARAM_RE = re.compile(r"([?&])([^=&\s]+)=([^&\s]+)")

# The same credential written as a plain assignment has no "?" either, and may
# be too short or too word-like to have become a candidate. Anchoring to the
# name is what lets these two rules skip the length floor the guesses need.
_ASSIGNMENT_RE = re.compile(r"([\w.\-]+)=([^\s&]+)")
# And the space-separated form, which is how a child echoes its own argv back.
# The value must not itself start with "-", or a bare flag eats the next one.
_FLAG_VALUE_RE = re.compile(r"(--[\w.\-]+)(\s+)([^\s\-][^\s]*)")

# Short values collide with ordinary words, which is the only reason either rule
# below has a floor, so both use the same one. A declared name needs none.
_MIN_REDACTABLE_VALUE_LEN = 8


def _secret_candidates(
    env: Mapping[str, str] | None, declared: Iterable[str] = ()
) -> dict[str, str]:
    """The environment values a log redactor must remove.

    Declared names come first because the operator saying a variable holds a
    secret is authority; the name pattern is only a guess, and it is what let a
    secret named for its purpose rather than its kind through.
    """
    if not env:
        return {}
    named = frozenset(declared)
    return {
        key: value
        for key, value in env.items()
        if isinstance(key, str)
        and isinstance(value, str)
        # Empty would splice "[redacted]" between every character of the log.
        and value
        and (
            key in named
            or (len(value) >= _MIN_REDACTABLE_VALUE_LEN and _SECRET_ENV_KEY_RE.search(key))
        )
    }


def _cmd_secret_values(cmd: Iterable[str] | None) -> dict[str, str]:
    """Credentials passed as arguments, which no environment mapping holds.

    A name or a flag is what makes an argument tellable from a path or a
    subcommand, so a credential passed as a bare positional stays outside this:
    nothing distinguishes it from the arguments the message needs to stay
    useful. The environment is the supported channel for one, and it is covered
    whatever the value looks like.

    The anchored text rules already cover an echoed flag; this covers the child
    printing the value alone, where nothing in the text says what it is. Same
    length floor as the environment guesses, for the same reason: the flag name
    is a guess, and a short value is not tellable from an ordinary word.

    A caller that does need a bare positional covered declares the literal value
    to remove, because it is the one that knows which argument is the credential.
    Widening this to guess at positionals is not the extension: it would strip
    the paths and subcommands the failure message exists to carry.
    """
    found: dict[str, str] = {}
    awaiting: str | None = None
    for token in cmd or ():
        if not isinstance(token, str):
            awaiting = None
            continue
        name, sep, value = token.partition("=")
        if sep and _name_reads_as_credential(name):
            if len(value) >= _MIN_REDACTABLE_VALUE_LEN:
                found[name.lstrip("-")] = value
            awaiting = None
        elif awaiting is not None and not token.startswith("-"):
            if len(token) >= _MIN_REDACTABLE_VALUE_LEN:
                found[awaiting] = token
            awaiting = None
        else:
            awaiting = token.lstrip("-") if _name_reads_as_credential(token) else None
    return found


def _bounded_env_values(
    env: Mapping[str, str] | None, already_secret: Mapping[str, str]
) -> dict[str, str]:
    """Name-matched values under the floor, replaceable only as whole tokens.

    The name is evidence the value is a credential, so length must not excuse
    it entirely. What length does decide is the replacement: a short value can
    sit inside an ordinary word, so it gets a bounded replacement while longer
    values keep the stronger substring one.
    """
    if not env:
        return {}
    return {
        key: value
        for key, value in env.items()
        if isinstance(key, str)
        and isinstance(value, str)
        and value
        and key not in already_secret
        and len(value) < _MIN_REDACTABLE_VALUE_LEN
        and _SECRET_ENV_KEY_RE.search(key)
    }


def _opaque_env_values(
    env: Mapping[str, str] | None, already_secret: Mapping[str, str]
) -> dict[str, str]:
    """Values the log will not echo even though nothing marks them secret.

    A secret can sit in a variable whose name says nothing, so length is the
    only signal left.
    """
    if not env:
        return {}
    return {
        key: value
        for key, value in env.items()
        if isinstance(key, str)
        and isinstance(value, str)
        and key not in already_secret
        and len(value) >= _MIN_REDACTABLE_VALUE_LEN
    }


def _name_reads_as_credential(name: str) -> bool:
    """Whether a parameter, variable or flag name announces that it holds a secret."""
    # Decoded before the test: `p%61ssword` is the same parameter to the server
    # that reads it, and would otherwise carry its value past every rule.
    return bool(_SECRET_ENV_KEY_RE.search(unquote(name)))


def _redact_query_value(match: re.Match[str]) -> str:
    """Replace a query parameter's value when its name reads as a credential."""
    separator, name, value = match.groups()
    if not _name_reads_as_credential(name):
        return match.group(0)
    return f"{separator}{name}=[redacted]"


def _redact_assignment_value(match: re.Match[str]) -> str:
    """Replace a bare NAME=value pair when the name reads as a credential."""
    name, value = match.groups()
    if not _name_reads_as_credential(name):
        return match.group(0)
    return f"{name}=[redacted]"


def _redact_flag_value(match: re.Match[str]) -> str:
    """Replace the value after a credential-named flag, as echoed argv shows it."""
    flag, gap, value = match.groups()
    if not _name_reads_as_credential(flag):
        return match.group(0)
    return f"{flag}{gap}[redacted]"


def _escape_control_characters(text: str) -> str:
    """Show control bytes rather than let child output forge a log record."""
    return "".join(
        character if character == " " or character.isprintable() else repr(character)[1:-1]
        for character in text
    )


def _redact_secrets_for_log(
    text: str,
    secrets: Mapping[str, str] | None,
    opaque: Mapping[str, str] | None = None,
    bounded: Mapping[str, str] | None = None,
) -> str:
    """Strip credentials out of child output before any of it reaches a log.

    Both arguments are already the sets to remove, not environments to filter:
    one selection site, so the spawn path and the log path cannot drift apart.
    A known secret becomes ``[redacted]`` because naming it adds nothing; an
    unclassified value becomes its variable's name, which keeps the message
    diagnostic without printing what the variable held.
    """
    if not text:
        return text
    replacements: dict[str, str] = {}
    for key, value in (opaque or {}).items():
        if isinstance(value, str) and value:
            replacements[value] = f"[${key}]"
    # Second, so a value in both classes is redacted rather than named.
    for value in (secrets or {}).values():
        if isinstance(value, str) and value:
            replacements[value] = "[redacted]"
    # Longest first, so a value containing another is not left half-revealed.
    for value in sorted(replacements, key=len, reverse=True):
        text = text.replace(value, replacements[value])
    for value in sorted({v for v in (bounded or {}).values() if v}, key=len, reverse=True):
        # Whole token only: a substring pass would splice into ordinary words.
        text = re.sub(rf"(?<!\w){re.escape(value)}(?!\w)", "[redacted]", text)
    text = _URL_CREDENTIAL_RE.sub(r"\1[redacted]\2", text)
    text = _URL_QUERY_PARAM_RE.sub(_redact_query_value, text)
    text = _ASSIGNMENT_RE.sub(_redact_assignment_value, text)
    text = _FLAG_VALUE_RE.sub(_redact_flag_value, text)
    # Escape last: the redaction patterns are written against the real text.
    return _escape_control_characters(_SECRET_SHAPE_RE.sub("[redacted]", text))


def _abandoned_without_output_note(
    captured: str,
    unavailable: str | None,
    drain_error: str | None,
    drain_incomplete: bool = False,
) -> str:
    """What to say about a child abandoned before it produced output; keeps ``_no_stderr_reason``'s three-way split so a broken capture cannot read as a quiet subprocess."""
    if captured:
        clipped = captured[:_ABANDONED_STDERR_LOG_CAP]
        suffix = " [truncated]" if len(captured) > _ABANDONED_STDERR_LOG_CAP else ""
        if drain_incomplete:
            suffix += " [stderr drain did not finish]"
        return f"its stderr said: {clipped}{suffix}"
    if drain_error is not None:
        return f"reading its stderr failed with {drain_error}, so no output was captured"
    if drain_incomplete:
        # An unfinished drain leaves the pipe unread, so silence here is
        # unknown rather than a quiet child.
        return "its stderr could not be drained in time, so whether it wrote anything is unknown"
    if unavailable is not None:
        return unavailable
    return "it wrote nothing to stderr either"


async def ndjson_from_cli(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdin: Any = asyncio.subprocess.DEVNULL,
    stdin_data: str | bytes | None = None,
    tail_repair: Callable[[str], dict | None] | None = None,
    on_spawn: Callable[[SpawnedProcess], None | Awaitable[None]] | None = None,
    engine: str | None = None,
    workload: str = DEFAULT_WORKLOAD,
) -> AsyncIterator[dict]:
    """Yield dicts from an NDJSON-emitting CLI subprocess; tail_repair handles
    malformed final chunks. ``stdin_data`` overrides ``stdin`` and is written
    concurrently with the stdout/stderr readers below, then closed so the
    child sees EOF. ``on_spawn`` is awaited (may be a coroutine function)
    before any output is read, and its failure is not swallowed — it
    propagates through the teardown below, which ends the child's group.
    Before the spawn the agent takes a machine-wide slot for ``engine`` and
    ``workload`` (see ``_subagent_admission``), held until the child's group
    has ended. See docs/internals/providers.md#cli-subprocess-lifecycle.
    """
    # An agent lionagi started cannot start another through lionagi, by its shell or by an
    # MCP server it was given; refused here, before any slot is waited for.
    refuse_nested_agent(engine)
    # A request copied with model_copy(update=...) skips validation, and the spawn's own
    # encode error would hold the whole key or value, so the environment is read and
    # checked here, once, and that copy is what the fill and the spawn receive.
    if env is not None:
        env = env_as_the_spawn_encodes_it(env)
    # Every CLI provider spawns through here, so a secret the child must read
    # from its own environment is filled in one place rather than four. Purely
    # additive: with nothing configured this returns ``env`` unchanged, and a
    # lookup that fails leaves the child to fail the way it already failed.
    # One config read for both: re-resolving after the fill's await would let a
    # settings edit in that window hand the child a value the redactor is not
    # told to remove.
    child_env, declared = await fill_declared_secrets_and_names(env)
    # One mapping for both the child and the redactor: with env=None the child
    # reads os.environ at exec, later than any snapshot taken here.
    spawn_env, spawn_marker = prepare_process_group_env(child_env)
    # Every agent lionagi starts carries a depth, so a lionagi it runs refuses to start
    # another (refuse_nested_agent). Set over whatever the environment held: only a
    # LIONAGI_SEAT_PROFILES profile `li agent` launched here is handed 0 (child_agent_depth).
    spawn_env[AGENT_DEPTH_ENV] = str(child_agent_depth())
    redaction_env: Mapping[str, str] = {
        **_secret_candidates(spawn_env, declared),
        **_cmd_secret_values(cmd),
    }
    opaque_env: Mapping[str, str] = _opaque_env_values(spawn_env, redaction_env)
    bounded_env: Mapping[str, str] = _bounded_env_values(spawn_env, redaction_env)
    kwargs: dict[str, Any] = dict(
        cwd=str(cwd) if cwd else None,
        env=spawn_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    # Encoded before the slot is taken and the child started, so an input that cannot be
    # encoded is refused while nothing has to be given back.
    stdin_payload: bytes | None = None
    if stdin_data is not None:
        kwargs["stdin"] = asyncio.subprocess.PIPE
        stdin_payload = stdin_data.encode() if isinstance(stdin_data, str) else stdin_data
    elif stdin is not _INHERIT_STDIN:
        kwargs["stdin"] = stdin

    # Admission is synchronous after capacity is acquired, before a task can
    # attempt an OS spawn. A waiter waking after owner close starts no child.
    lease = await admit_subagent(engine=engine, workload=workload)
    try:
        _require_cli_executable(cmd, cwd=cwd, env=spawn_env)
        recorder = _get_cli_worker_recorder()
        admission = recorder.admit() if recorder is not None else None
    except BaseException:
        lease.release()
        raise
    spawn_permitted = asyncio.Event()

    async def _spawn_and_register() -> tuple[asyncio.subprocess.Process, SpawnedProcess]:
        child = None
        observed = None
        attempted = False
        try:
            # Even an eager task factory cannot start the OS attempt before
            # the task is strongly retained by its admission and local owner.
            await spawn_permitted.wait()
            attempted = True
            child = await asyncio.create_subprocess_exec(*cmd, **kwargs)
            try:
                observed = observe_spawned(child.pid, expected_marker=spawn_marker)
                if (lease.slots or admission is not None) and observed.create_time is None:
                    observed = observe_spawned(child.pid, expected_marker=spawn_marker)
            except Exception as exc:
                if admission is None:
                    raise
                raise _WorkerSpawnAdmissionError("worker identity observation failed") from exc
            if (lease.slots or admission is not None) and observed.create_time is None:
                if admission is not None:
                    raise _WorkerSpawnAdmissionError("worker start time could not be observed")
                from ._provider_errors import SubagentAdmissionRefusedError

                raise SubagentAdmissionRefusedError(
                    "subagent admission refused: the agent's start time could not be read"
                )
            lease.record_child(observed.pid, observed.create_time)
            if admission is not None:
                await admission.record(_worker_spawn_identity(observed))
            return child, observed
        except BaseException:
            try:
                if not attempted:
                    lease.release()
                elif child is None:
                    # An exception with no handle can occur after OS launch.
                    # Without positive provenance it proves no group absence.
                    _finish_child_lease(lease, _ChildGroupCleanup.UNCONFIRMED, launch_outcome=True)
                else:
                    outcome = _ChildGroupCleanup.UNCONFIRMED
                    try:
                        outcome = await end_child_group(child, spawned=observed)
                    except BaseException:
                        log.warning("failed worker admission cleanup remains unconfirmed")
                    _finish_child_lease(lease, outcome)
            finally:
                if admission is not None:
                    admission.finish()
            raise

    spawn_coro = _spawn_and_register()
    try:
        spawn = asyncio.ensure_future(spawn_coro)
    except BaseException:
        spawn_coro.close()
        lease.release()
        if admission is not None:
            admission.finish()
        raise
    _retain_cli_task(spawn, admission)
    spawn_permitted.set()
    try:
        proc, spawned = await asyncio.shield(spawn)
    except BaseException:

        def _end_late_spawn(task: asyncio.Future) -> None:
            # A failed attempt owns its cleanup/lease/token in its own finally.
            if task.cancelled() or task.exception() is not None:
                return
            start_gate = asyncio.Event()
            entered = asyncio.Event()

            def settle_without_body() -> None:
                # Construction failure or cancellation before the first step
                # still owns the returned child and its unreleased capacity.
                outcome = _ChildGroupCleanup.UNCONFIRMED
                try:
                    outcome = _kill_abandoned_spawn(task)
                except BaseException:
                    log.warning("synchronous abandoned-child cleanup remains unconfirmed")
                try:
                    _finish_child_lease(lease, outcome)
                finally:
                    if admission is not None:
                        admission.finish()

            def cleanup_settled(done: asyncio.Future) -> None:
                try:
                    if not entered.is_set():
                        try:
                            cleanup_coro.close()
                        finally:
                            settle_without_body()
                    elif admission is not None:
                        admission.finish()
                except BaseException:
                    log.warning("could not finalize abandoned CLI cleanup ownership")

            cleanup_coro = _cleanup_abandoned_spawn(
                task, lease, admission, start_gate=start_gate, entered=entered
            )
            cleanup = None
            try:
                cleanup = asyncio.ensure_future(cleanup_coro)
                # Finalization precedes callbacks that clear owner task counts.
                cleanup.add_done_callback(cleanup_settled)
                _retain_cli_task(cleanup, admission)
            except BaseException:
                log.warning("could not construct or retain abandoned CLI cleanup")
                if cleanup is None:
                    try:
                        cleanup_coro.close()
                    except BaseException:
                        log.warning("could not close unstarted CLI cleanup")
                    try:
                        settle_without_body()
                    except BaseException:
                        log.warning("could not finalize abandoned CLI cleanup ownership")
            finally:
                # Even an eager task factory must suspend before cleanup work
                # until both strong task retention and finalization are bound.
                start_gate.set()

        if spawn.done():
            _end_late_spawn(spawn)
        else:
            spawn.add_done_callback(_end_late_spawn)
        raise
    if admission is not None:
        admission.finish()
    try:
        # Durable registration has completed. The run observer receives the
        # live handle privately; the public callback keeps its identity-only
        # contract. Their existing order still precedes all transport I/O.
        observers = (
            (_RUN_CLI_SPAWN_OBSERVER.get(), (proc, spawned)),
            (on_spawn, (spawned,)),
        )
        for observer, args in observers:
            if observer is not None:
                await maybe_await(observer(*args))

        if proc.stdout is None:
            raise RuntimeError("Failed to capture stdout from subprocess")
    except BaseException:
        outcome = _ChildGroupCleanup.UNCONFIRMED
        try:
            outcome = await end_child_group(proc, spawned=spawned)
        except BaseException:
            log.warning("CLI callback failure cleanup remains unconfirmed")
        _finish_child_lease(lease, outcome)
        raise

    decoder = codecs.getincrementaldecoder("utf-8")()
    json_decoder = json.JSONDecoder()
    buffer: str = ""

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

    def captured_stderr() -> str:
        """The child's stderr as anything outside this process may see it."""
        # Both the non-zero-exit error and the abandoned-child warning surface
        # these same bytes, so they are redacted here rather than at each.
        return _redact_secrets_for_log(
            b"".join(stderr_chunks).decode(errors="replace").strip(),
            redaction_env,
            opaque_env,
            bounded_env,
        )

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
    if stdin_payload is not None:
        stdin_task = asyncio.create_task(_write_stdin(stdin_payload))

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

    # Set before each yield, never after: GeneratorExit lands at the yield, so a
    # later assignment never runs and a child that spoke reads as one that did not.
    produced_output = False
    # Whether the exit-code path below already quoted stderr to the caller, so
    # the teardown does not repeat it into the log.
    stderr_already_surfaced = False

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
                    produced_output = True
                    yield obj
                    buffer = buffer[idx:]
                except json.JSONDecodeError:
                    break

        buffer += decoder.decode(b"", final=True)
        buffer = buffer.strip()
        if buffer:
            try:
                obj, idx = json_decoder.raw_decode(buffer)
                produced_output = True
                yield obj
            except json.JSONDecodeError:
                if tail_repair is not None:
                    try:
                        repaired = tail_repair(buffer)
                        if repaired is not None:
                            produced_output = True
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
            err = captured_stderr()
            # Emptiness decided on what was captured, before the drain note is
            # appended, so a truncated drain that captured nothing still gets
            # a message instead of the note masquerading as output.
            if not err:
                err = _no_stderr_reason(rc, stderr_unavailable, stderr_drain_error)
            if drain_truncated:
                err += " [stderr drain timed out]"
            stderr_already_surfaced = True
            raise RuntimeError(err)

    finally:
        # Neither stderr-quoting path covers this, and it is decided before any
        # await, since awaiting in a finally can change sys.exc_info().
        primary_error = sys.exc_info()[1]
        abandoned_silently = (
            primary_error is not None and not produced_output and not stderr_already_surfaced
        )

        try:
            try:
                outcome = await end_child_group(proc, spawned=spawned)
            except BaseException as teardown_error:
                # Without this line the error is lost: the consumer's own error is
                # raised in its place below.
                log.warning("CLI child group teardown raised %r", teardown_error)
                _finish_child_lease(lease, _ChildGroupCleanup.UNCONFIRMED)
                if primary_error is not None and not isinstance(primary_error, GeneratorExit):
                    raise primary_error  # noqa: B904 -- preserve the consumer error's own cause
                raise
            _finish_child_lease(lease, outcome)
            # Only a recorded launch claims its group was ended; an unrecorded one
            # keeps the warning and the held slots, so a platform without killpg
            # still completes a run that succeeded.
            if (
                outcome is not _ChildGroupCleanup.CONFIRMED_EMPTY
                and admission is not None
                and (primary_error is None or isinstance(primary_error, GeneratorExit))
            ):
                raise ProviderTeardownError("CLI child group cleanup remains unconfirmed")

            if abandoned_silently:
                # Drain first, or the warning reports "said nothing" about a child
                # that spoke; shielded so a timeout cannot cancel it mid-buffer.
                abandon_drain_incomplete = False
                abandon_cancelled: asyncio.CancelledError | None = None
                if stderr_task is not None:
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(stderr_task), timeout=_ABANDONED_STDERR_DRAIN_TIMEOUT
                        )
                    except asyncio.CancelledError as exc:
                        abandon_cancelled = exc
                        abandon_drain_incomplete = True
                    except Exception:  # noqa: BLE001
                        # Whatever it captured stays in the buffer, but the drain
                        # did not finish, so an empty buffer is unknown rather than
                        # evidence the child was quiet.
                        abandon_drain_incomplete = True
                log.warning(
                    "CLI subprocess produced no output before it was abandoned; %s",
                    _abandoned_without_output_note(
                        captured_stderr(),
                        stderr_unavailable,
                        stderr_drain_error,
                        abandon_drain_incomplete,
                    ),
                )
                if abandon_cancelled is not None:
                    raise abandon_cancelled

        finally:
            # wait() does not raise a helper's cancellation. A cancellation here
            # belongs to this consumer: join every helper before propagating it.
            reap_cancelled: asyncio.CancelledError | None = None
            for task in (stderr_task, stdin_task, exit_task, read_task):
                if task is None:
                    continue
                task.cancel()
                while not task.done():
                    try:
                        await asyncio.wait({task})
                    except asyncio.CancelledError as exc:
                        if reap_cancelled is None:
                            reap_cancelled = exc
                if not task.cancelled():
                    task.exception()
            if reap_cancelled is not None:
                raise reap_cancelled


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


def cli_fallback_bin_dirs() -> tuple[Path, ...]:
    """Dirs probed, in order, when PATH lookup fails. Detached servers (MCP
    bridges, launchd jobs) inherit their launcher's PATH, which routinely
    lacks the per-user install dirs the CLIs actually live in; PATH still wins
    when it resolves, so a user's explicit override is never shadowed.
    Computed at call time: ``Path.home()`` follows the live environment."""
    return (
        Path.home() / ".local" / "bin",
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
    )


def discover_cli(binary: str) -> tuple[bool, str | None]:
    """Return (available, resolved_path) for a CLI binary, from PATH first, then cli_fallback_bin_dirs()."""
    candidate = shutil.which(binary) or binary
    if shutil.which(candidate):
        return True, candidate
    for bin_dir in cli_fallback_bin_dirs():
        fallback = bin_dir / binary
        if fallback.is_file() and os.access(fallback, os.X_OK):
            return True, str(fallback)
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
