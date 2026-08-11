# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""External-hook exec adapter: compatibility profile v1 wire envelope + executor.

Turns one ``hooks_external:`` config entry (event, argv command, matcher,
timeout) into an async callable for whichever internal seam the event maps
to: the tool pre/post hook chain for ``PreToolUse``/``PostToolUse``, or a
``HookBus`` handler for the rest. Spawns the configured command as a real
subprocess, writes the JSON envelope to stdin, parses stdout/exit code back
into a decision.

Distinct from ``lionagi.agent.settings._make_shell_hook`` (legacy
``hooks: {pre,post,on_error}`` shape, never reads stdout) and from the
``hooks_external`` field on a plugin manifest (parsed as inert disclosure
data only — this module is what actually runs a command).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lionagi.ln._proc import aterminate_process_group
from lionagi.protocols.action.tool_hooks import ToolPostDecision, ToolPreDecision

logger = logging.getLogger(__name__)

__all__ = (
    "BLOCKING_EVENTS",
    "PROFILE_VERSION",
    "SUPPORTED_EVENTS",
    "ExternalHookConfigError",
    "HookVerdict",
    "build_envelope",
    "compute_command_hash",
    "compute_executable_digest",
    "compute_trust_record",
    "external_hook_adapter",
    "is_command_trusted",
    "match_hook",
    "resolve_hook_executable",
    "validate_argv",
)

# Compatibility profile version this adapter implements (see the ADR's Notes
# section: the loader and import command reference the profile version they
# implement; a later harness-driven revision gets a new version here).
PROFILE_VERSION = "v1"

# External event name -> the internal seam it is wired to (fixed mapping;
# any other name fails config load rather than silently no-op'ing).
SUPPORTED_EVENTS = frozenset(
    {
        "SessionStart",
        "SessionEnd",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PostToolUseFailure",
    }
)

# Events with a blocking-capable seam: a deny verdict must fail the action
# closed (raise), not just log and continue.
BLOCKING_EVENTS = frozenset({"UserPromptSubmit", "PreToolUse"})

_MAX_STDOUT_BYTES = 1_048_576  # 1 MiB cap on hook stdout read-back
_MAX_STDERR_BYTES = 1_048_576  # same cap on stderr -- neither pipe buffers unbounded
_READ_CHUNK_BYTES = 65_536
_TERMINATE_GRACE = 2.0


class ExternalHookConfigError(ValueError):
    """A ``hooks_external`` config entry is malformed; fails config load."""


class _HookTimeoutError(Exception):
    """Internal signal: the hook subprocess did not finish within its timeout."""


def _json_safe(value: Any) -> Any:
    """ADR-0048 D1's ``tool_response`` field guarantee: the value as-is where
    JSON-serializable, else its ``str()`` form. Applied at envelope
    construction (see :func:`build_envelope`) so an arbitrary, non-serializable
    tool result becomes a string up front instead of raising out of
    ``json.dumps`` after a hook subprocess has already been spawned."""
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


def validate_argv(command: Any) -> list[str]:
    """Enforce the argv rule: a non-empty list of non-empty, non-whitespace strings.

    A string-form command, an empty list, or a list containing a blank entry
    all raise -- this is a config error, never a heuristic ``shlex.split``.
    """
    if not isinstance(command, list) or not command:
        raise ExternalHookConfigError(
            f"hooks_external: command must be a non-empty argv list, got {command!r}"
        )
    for part in command:
        if not isinstance(part, str) or not part.strip():
            raise ExternalHookConfigError(
                "hooks_external: command must be a non-empty argv list of "
                f"non-empty, non-whitespace strings, got {command!r}"
            )
    return command


def compute_command_hash(command: list[str]) -> str:
    """``sha256(json.dumps(argv))`` -- identifies the *approval request*, not
    the executable that will run. Kept as an argv-identity key inside the
    fuller :func:`compute_trust_record`; never sufficient on its own to admit
    execution (see D7 note on content pinning below)."""
    return hashlib.sha256(json.dumps(command).encode()).hexdigest()


def resolve_hook_executable(command: list[str], cwd: str) -> Path:
    """Resolve ``command[0]`` to the absolute executable path that will
    actually spawn, using the same lookup rule ``execvp`` uses: a name with a
    path separator resolves relative to *cwd*, never PATH-searched; a bare
    name is PATH-searched only.

    Raises :class:`ExternalHookConfigError` when the resolved path does not
    exist, is not a file, is not executable, or isn't found on ``PATH``.
    """
    name = command[0]
    if os.sep in name or (os.altsep and os.altsep in name):
        candidate = (Path(cwd) / name).resolve()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ExternalHookConfigError(
                f"hooks_external: executable not found or not executable: {candidate}"
            )
        return candidate
    found = shutil.which(name)
    if found is None:
        raise ExternalHookConfigError(f"hooks_external: executable {name!r} not found on PATH")
    return Path(found).resolve()


def compute_executable_digest(path: Path) -> str:
    """``sha256`` over the resolved executable's file bytes — the content
    half of the trust pin, so a same-path substitution after approval
    changes this digest even though the resolved path string doesn't."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_trust_record(command: list[str], cwd: str) -> dict[str, str]:
    """The trust record for one approval: argv hash (identifies the request)
    plus the resolved executable's path and content digest (identifies what
    will actually run). Raises if *command* cannot be resolved right now.
    """
    resolved = resolve_hook_executable(command, cwd)
    return {
        "argv_hash": compute_command_hash(command),
        "resolved_path": str(resolved),
        "content_digest": compute_executable_digest(resolved),
    }


@dataclass(frozen=True, slots=True)
class _BoundExecutable:
    """A private, single-process-owned copy of the resolved executable's
    bytes; ``path`` (inside ``private_dir``) is what actually gets exec'd,
    never the configured/approved path. See
    docs/internals/mcp.md#hooks-private-copy-trust-pinning.

    ``private_dir`` is removed once the hook process exits, or immediately
    if the copy's digest doesn't match the trust record."""

    path: Path
    private_dir: str


_POSIX_NOFOLLOW_OPEN = hasattr(os, "O_NOFOLLOW")


def _open_executable_fd(path: Path) -> int:
    """Open *path* for execution without following a symlink at its final
    component, and verify what was opened is a regular file.

    ``os.O_NOFOLLOW`` is POSIX-only; on Windows this falls back to a
    non-atomic ``path.is_symlink()`` pre-check, weaker but the best
    available. Raises :class:`ExternalHookConfigError` (never a bare
    ``OSError``) on failure. The returned fd is the caller's to close.
    """
    if _POSIX_NOFOLLOW_OPEN:
        open_flags = os.O_RDONLY | os.O_NOFOLLOW
    else:
        if path.is_symlink():
            raise ExternalHookConfigError(
                f"hooks_external: cannot open resolved executable {path}: refusing to follow a symlink"
            )
        open_flags = os.O_RDONLY
    try:
        fd = os.open(str(path), open_flags)
    except OSError as exc:
        raise ExternalHookConfigError(
            f"hooks_external: cannot open resolved executable {path}: {exc}"
        ) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ExternalHookConfigError(
                f"hooks_external: resolved executable {path} is not a regular file"
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def _hash_fd(fd: int) -> str:
    """``sha256`` over an open fd's full content, read from offset 0 —
    reads the already-open descriptor rather than a fresh path lookup
    that could resolve to a different file by the time it runs."""
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1 << 20)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(fd, 0, os.SEEK_SET)
    return digest.hexdigest()


def _materialize_private_copy(fd: int, basename: str) -> _BoundExecutable:
    """Copy the bytes at *fd* (from offset 0, never a fresh path lookup)
    into a fresh, single-process-owned private directory, run BEFORE any
    trust digest is computed. See
    docs/internals/mcp.md#hooks-private-copy-trust-pinning for why copy-then-hash
    (not hash-then-copy) is what closes the TOCTOU window.

    *basename* matches the original so a shebang script's argv[0] stays
    readable. Directory is ``mkdtemp``'d (mode 0700); copy is ``O_EXCL``."""
    private_dir = tempfile.mkdtemp(prefix="lionagi-hook-")
    private_path = Path(private_dir) / basename
    os.lseek(fd, 0, os.SEEK_SET)
    copy_fd = os.open(str(private_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    try:
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            os.write(copy_fd, chunk)
        os.fsync(copy_fd)
    finally:
        os.close(copy_fd)
    return _BoundExecutable(path=private_path, private_dir=private_dir)


def _hash_private_copy(path: Path) -> str:
    """``sha256`` over the private copy at *path*, opened fresh read-only
    from the private directory — never the source path or fd again, so a
    source overwrite at any point can't change what gets compared/exec'd."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        return _hash_fd(fd)
    finally:
        os.close(fd)


def _prepare_trusted_execution(
    command: list[str], *, source: str, cwd: str
) -> tuple[_BoundExecutable | None, str]:
    """Resolve, open, privately copy, and THEN content-verify *command*
    for a source-having (imported) entry — never hash the mutable source
    itself, only the frozen private copy. See
    docs/internals/mcp.md#hooks-private-copy-trust-pinning.

    Returns ``(bound, "")`` on a match, or ``(None, reason)`` on failure
    (the private copy, if made, is removed first); the caller must never
    fall back to spawning the raw argv in the failure case.
    """
    try:
        resolved_path = resolve_hook_executable(command, cwd)
    except ExternalHookConfigError as exc:
        return None, f"untrusted hook command {command!r} (source={source!r}): {exc}"
    try:
        fd = _open_executable_fd(resolved_path)
    except ExternalHookConfigError as exc:
        return None, f"untrusted hook command {command!r} (source={source!r}): {exc}"

    try:
        bound = _materialize_private_copy(fd, resolved_path.name)
    finally:
        os.close(fd)

    try:
        from lionagi.plugins._user_settings import read_user_settings

        trusted = read_user_settings().get("trusted_hook_commands", [])
        if not isinstance(trusted, list):
            trusted = []
        argv_hash = compute_command_hash(command)
        content_digest = _hash_private_copy(bound.path)
        argv_matches = [
            record
            for record in trusted
            if isinstance(record, dict) and record.get("argv_hash") == argv_hash
        ]
        for record in argv_matches:
            if (
                record.get("resolved_path") == str(resolved_path)
                and record.get("content_digest") == content_digest
            ):
                return bound, ""
    except BaseException:
        shutil.rmtree(bound.private_dir, ignore_errors=True)
        raise

    shutil.rmtree(bound.private_dir, ignore_errors=True)
    if argv_matches:
        return None, (
            f"hook command {command!r} (source={source!r}) resolves to a different "
            f"executable than was approved (now: {str(resolved_path)!r}); the "
            "approved path or its contents changed since `li hooks trust` -- fails "
            "closed, this is not the executable that was reviewed"
        )
    return None, f"untrusted hook command {command!r} (source={source!r})"


def _trust_status(command: list[str], *, source: str | None, cwd: str) -> tuple[bool, str]:
    """Loader trust rule (ADR-0048 D7, content-pinned): a ``source``-less entry
    is project/user-authored and trusted as code; any non-empty ``source``
    (``imported:claude``, ``imported:codex``, ...) requires a matching record
    in ``trusted_hook_commands`` whose argv hash, resolved executable path,
    AND content digest all match *command* as it resolves right now — a
    stale approval does not carry over if the resolved executable or its
    bytes changed (closes argv-only hashing letting a different repo's or a
    later PATH-resolved same-argv command run under a stale approval).

    Returns ``(trusted, reason)``; *reason* is empty when trusted.
    """
    if not source:
        return True, ""
    from lionagi.plugins._user_settings import read_user_settings

    trusted = read_user_settings().get("trusted_hook_commands", [])
    if not isinstance(trusted, list):
        trusted = []
    try:
        current = compute_trust_record(command, cwd)
    except ExternalHookConfigError as exc:
        return False, f"untrusted hook command {command!r} (source={source!r}): {exc}"

    argv_matches = [
        record
        for record in trusted
        if isinstance(record, dict) and record.get("argv_hash") == current["argv_hash"]
    ]
    for record in argv_matches:
        if (
            record.get("resolved_path") == current["resolved_path"]
            and record.get("content_digest") == current["content_digest"]
        ):
            return True, ""
    if argv_matches:
        return False, (
            f"hook command {command!r} (source={source!r}) resolves to a different "
            f"executable than was approved (now: {current['resolved_path']!r}); the "
            "approved path or its contents changed since `li hooks trust` -- fails "
            "closed, this is not the executable that was reviewed"
        )
    return False, f"untrusted hook command {command!r} (source={source!r})"


def is_command_trusted(command: list[str], *, source: str | None, cwd: str | None = None) -> bool:
    """Boolean form of :func:`_trust_status`; *cwd* defaults to the current
    working directory when omitted."""
    trusted, _ = _trust_status(
        command, source=source, cwd=cwd if cwd is not None else str(Path.cwd())
    )
    return trusted


def match_hook(matcher: str | None, subject: str) -> bool:
    """Harness matcher semantics: omitted/``""``/``"*"`` matches all;
    alphanumeric/``_``/``-``/space/``,``/``|`` strings are exact-or-list
    matches; anything else is an unanchored regex."""
    if not matcher or matcher == "*":
        return True
    if re.fullmatch(r"[\w \-,|]+", matcher):
        names = [n.strip() for n in re.split(r"[,|]", matcher) if n.strip()]
        return subject in names
    return re.search(matcher, subject) is not None


def build_envelope(
    *,
    hook_event_name: str,
    session_id: str,
    cwd: str,
    harness: str = "lionagi",
    tool_name: str | None = None,
    tool_input: Any = None,
    tool_response: Any = None,
    prompt: str | None = None,
    model: str | None = None,
    permission_mode: str | None = None,
) -> dict[str, Any]:
    """Build the stdin envelope for *hook_event_name*.

    Common fields are always present; per-event fields follow the
    field-guarantee table (ADR-0048 D1): tool events get
    ``tool_name``/``tool_input``, plus ``tool_response`` (via
    :func:`_json_safe`, so a non-serializable result becomes a string here
    rather than failing ``json.dumps`` after the hook has spawned) for
    ``PostToolUse``/``PostToolUseFailure``; ``UserPromptSubmit`` gets
    ``prompt``/``model``/``permission_mode`` (defaulting to ``"default"``).
    """
    envelope: dict[str, Any] = {
        "session_id": session_id,
        "cwd": cwd,
        "hook_event_name": hook_event_name,
        "harness": harness,
    }
    if hook_event_name in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        envelope["tool_name"] = tool_name
        envelope["tool_input"] = tool_input
        if hook_event_name in ("PostToolUse", "PostToolUseFailure"):
            envelope["tool_response"] = _json_safe(tool_response)
    elif hook_event_name == "UserPromptSubmit":
        envelope["prompt"] = prompt
        envelope["model"] = model
        envelope["permission_mode"] = permission_mode or "default"
    return envelope


@dataclass(frozen=True, slots=True)
class HookVerdict:
    """Normalized outcome of one hook process's exit code + stdout.

    ``outcome`` is ``"allow"`` (continue, possibly with ``updated_input``),
    ``"deny"`` (fails the action closed on a blocking seam, else an advisory
    note), or ``"error"`` (hook itself misbehaved -- logged, never a block on
    an advisory seam; treated as deny on a blocking seam, since a guard that
    cannot run must not silently admit the action it was meant to gate).
    """

    outcome: str
    reason: str = ""
    updated_input: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class _CappedRead:
    """The retained bytes from one pipe drain, plus whether the pipe
    exceeded its cap — ``truncated`` must survive to the decision point so a
    complete short response is never mistaken for a truncated longer one."""

    data: bytes
    truncated: bool


async def _read_capped(stream: Any, cap: int, label: str) -> _CappedRead:
    """Read *stream* to EOF, keeping at most the first *cap* bytes.

    Bytes beyond the cap are still drained off the pipe (so the child never
    blocks on a full pipe) but discarded immediately, not buffered — a
    verbose hook can't force unbounded allocation. *label* names the stream
    in the truncation log.
    """
    if stream is None:
        return _CappedRead(data=b"", truncated=False)
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        chunk = await stream.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total <= cap:
            chunks.append(chunk)
        else:
            if not truncated:
                keep = cap - (total - len(chunk))
                if keep > 0:
                    chunks.append(chunk[:keep])
            truncated = True
    if truncated:
        logger.warning("hook %s exceeded %d bytes; truncating", label, cap)
    return _CappedRead(data=b"".join(chunks), truncated=truncated)


async def _write_stdin(proc: Any, data: bytes) -> None:
    stdin = proc.stdin
    if stdin is None:
        return
    stdin.write(data)
    await stdin.drain()
    stdin.close()
    with contextlib.suppress(Exception):
        await stdin.wait_closed()


async def _drain(proc: Any, envelope_bytes: bytes) -> tuple[_CappedRead, _CappedRead]:
    """Write the envelope and read both pipes concurrently — a hook that
    writes output before consuming all of stdin can't deadlock either
    direction. Reaps the process once both streams hit EOF."""
    _, stdout, stderr = await asyncio.gather(
        _write_stdin(proc, envelope_bytes),
        _read_capped(proc.stdout, _MAX_STDOUT_BYTES, "stdout"),
        _read_capped(proc.stderr, _MAX_STDERR_BYTES, "stderr"),
    )
    await proc.wait()
    return stdout, stderr


async def _spawn(argv: list[str], bound: _BoundExecutable | None, cwd: str) -> Any:
    """Spawn *argv* in *cwd* — the same directory the command was resolved
    against for approval, never whichever directory the calling process
    happens to be in.

    When *bound* is set (an imported command that went through
    :func:`_prepare_trusted_execution`), execution targets ``bound.path``,
    the private hash-verified copy, never the configured/approved path.
    *bound* is ``None`` for a project/user-authored command; it still spawns
    in *cwd*, via the raw argv.
    """
    common = {
        "stdin": asyncio.subprocess.PIPE,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "start_new_session": True,
        "cwd": cwd,
    }
    if bound is None:
        return await asyncio.create_subprocess_exec(*argv, **common)
    return await asyncio.create_subprocess_exec(*argv, executable=str(bound.path), **common)


async def _run_hook_process(
    argv: list[str],
    bound: _BoundExecutable | None,
    cwd: str,
    envelope_bytes: bytes,
    timeout: float,
) -> tuple[int, _CappedRead, _CappedRead]:
    proc = await _spawn(argv, bound, cwd)
    try:
        stdout, stderr = await asyncio.wait_for(
            _drain(proc, envelope_bytes),
            timeout=timeout,
        )
    except (TimeoutError, asyncio.TimeoutError) as err:
        # Catches both since they're distinct classes before Python 3.11.
        # Kill the whole process group so a hung hook's children can't
        # continue side effects after the timeout is declared.
        await aterminate_process_group(proc, grace=None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE)
        raise _HookTimeoutError(f"hook timed out after {timeout}s: {argv[0]!r}") from err

    return proc.returncode, stdout, stderr


def _parse_stdout_decision(
    stdout_bytes: bytes,
) -> tuple[str | None, str, dict[str, Any] | None, bool]:
    """Parse exit-0 stdout into ``(permission_decision, reason,
    updated_input, malformed)``. See
    docs/internals/mcp.md#hooks-stdout-decision-parsing for the malformed
    vs. empty-stdout distinction and the top-level ``decision`` null
    convention.
    """
    text = stdout_bytes.decode(errors="replace").strip()
    if not text:
        return None, "", None, False
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("hook stdout was non-empty but not valid JSON; failing closed")
        return None, "hook stdout was non-empty but not valid JSON", None, True
    if not isinstance(data, dict):
        return (
            None,
            f"hook stdout parsed to a non-object JSON value ({type(data).__name__})",
            None,
            True,
        )

    hook_specific = data.get("hookSpecificOutput")
    if isinstance(hook_specific, dict) and "permissionDecision" in hook_specific:
        decision = hook_specific.get("permissionDecision")
        reason = hook_specific.get("permissionDecisionReason") or ""
        if decision is None:
            return (
                None,
                reason or "hookSpecificOutput.permissionDecision was explicitly null",
                None,
                True,
            )
        updated_input = hook_specific.get("updatedInput")
        return (
            decision,
            reason,
            updated_input if isinstance(updated_input, dict) else None,
            False,
        )
    if "decision" in data:
        decision = data.get("decision")
        reason = data.get("reason") or ""
        if decision is None or decision in ("allow", "approve"):
            return None, reason, None, False
        if decision == "block":
            return "deny", reason, None, False
        # Any other value (e.g. "ask") passes through as-is so the caller's
        # decision switch fails it closed, same as the nested shape.
        return decision, reason, None, False
    return None, "hook stdout was valid JSON but had no recognized decision field", None, True


async def _execute_hook(
    *,
    argv: list[str],
    envelope: dict[str, Any],
    timeout: float,
    blocking: bool,
    bound: _BoundExecutable | None = None,
    cwd: str = ".",
) -> HookVerdict:
    """Spawn *argv*, exchange *envelope*, and normalize the exit-code + stdout
    contract into a :class:`HookVerdict`.

    *envelope* is serialized to JSON before any subprocess is spawned, so a
    serialization failure never orphans a spawned hook process.

    Exit 0 — stdout parsed as JSON if non-empty, UNLESS truncated (a
    truncated response is never parsed, since the retained prefix could
    coincidentally read as a complete but wrong decision) — treated as a
    hook failure like any other. Exit 2 — block, stderr is the reason. Any
    other exit, spawn/IO error, or timeout — hook failure (deny on a
    blocking seam, error/log-and-continue on an advisory one).
    """
    try:
        envelope_bytes = json.dumps(envelope).encode()
    except (TypeError, ValueError) as exc:
        return HookVerdict(
            outcome="deny" if blocking else "error",
            reason=f"hook envelope is not JSON-serializable: {exc}",
        )
    try:
        returncode, stdout, stderr = await _run_hook_process(
            argv, bound, cwd, envelope_bytes, timeout
        )
    except _HookTimeoutError as exc:
        return HookVerdict(outcome="deny" if blocking else "error", reason=str(exc))
    except Exception as exc:  # noqa: BLE001 -- subprocess spawn/IO errors
        return HookVerdict(
            outcome="deny" if blocking else "error",
            reason=f"hook execution error: {exc}",
        )

    if returncode == 2:
        reason = stderr.data.decode(errors="replace").strip() or f"hook blocked: {argv[0]!r}"
        return HookVerdict(outcome="deny", reason=reason)

    if returncode != 0:
        reason = (
            stderr.data.decode(errors="replace").strip()
            or f"hook failed (exit {returncode}): {argv[0]!r}"
        )
        return HookVerdict(outcome="deny" if blocking else "error", reason=reason)

    if stdout.truncated:
        return HookVerdict(
            outcome="deny" if blocking else "error",
            reason=(
                f"hook stdout exceeded {_MAX_STDOUT_BYTES} bytes and was "
                "truncated; a truncated response is never parsed as a decision"
            ),
        )

    decision, reason, updated_input, malformed = _parse_stdout_decision(stdout.data)
    if malformed:
        # A response that couldn't be understood as a decision is a
        # hook-response failure, not a policy choice — never the same as a
        # genuinely empty stdout's "no opinion".
        return HookVerdict(
            outcome="deny" if blocking else "error",
            reason=reason or "hook stdout did not contain a recognized decision; failing closed",
        )
    if decision is None or decision == "allow":
        return HookVerdict(outcome="allow", reason=reason, updated_input=updated_input)
    if decision == "deny":
        return HookVerdict(outcome="deny", reason=reason or "denied")
    if decision == "ask":
        return HookVerdict(
            outcome="deny",
            reason=(
                "hook requested interactive approval ('ask'); no interactive "
                "approval surface exists in this runtime -- failing closed"
            ),
        )
    return HookVerdict(outcome="deny", reason=f"unrecognized decision {decision!r}; failing closed")


def external_hook_adapter(
    *,
    event: str,
    command: list[str],
    timeout: float = 60.0,
    matcher: str | None = None,
    source: str | None = None,
    cwd: str | None = None,
    session_id: str | None = None,
    harness: str = "lionagi",
) -> Callable[..., Awaitable[Any]]:
    """Turn one ``hooks_external`` entry into an async callable for its seam.

    ``event`` selects the shape: ``PreToolUse``/``PostToolUse`` return a
    tool-hook-shaped callable for ``ActionManager``; the remaining events
    return a ``HookBus``-shaped ``**kwargs`` handler. ``source`` carries the
    provenance field (``None`` for project/user-authored entries,
    ``"imported:claude"``/``"imported:codex"`` for imported ones) — an
    untrusted non-empty-``source`` command never executes.
    """
    if event not in SUPPORTED_EVENTS:
        raise ExternalHookConfigError(
            f"hooks_external: no seam for event {event!r}; LionAGI has no hook "
            f"point for it. Supported events: {sorted(SUPPORTED_EVENTS)}"
        )
    command = validate_argv(command)
    resolved_cwd = cwd or str(Path.cwd())
    fallback_session_id = session_id or ""
    blocking = event in BLOCKING_EVENTS

    async def _guarded_execute(envelope: dict[str, Any]) -> HookVerdict:
        # Re-resolved every call, not cached: for a source-having (imported)
        # entry, resolution, the trust-record match, and the private copy
        # that gets exec'd all come from this ONE
        # `_prepare_trusted_execution` call, never a fresh re-resolution of
        # `command[0]` that a swap between "approved" and "exec'd" could win
        # a race against (see `_BoundExecutable`, D7 content pinning). A
        # source-less entry has no approval to bind against; it spawns
        # directly in `resolved_cwd`.
        bound: _BoundExecutable | None = None
        if source:
            bound, reason = _prepare_trusted_execution(command, source=source, cwd=resolved_cwd)
            if bound is None:
                reason = f"{reason}; run `li hooks trust` to approve it"
                return HookVerdict(outcome="deny" if blocking else "error", reason=reason)
        try:
            return await _execute_hook(
                argv=command,
                envelope=envelope,
                timeout=timeout,
                blocking=blocking,
                bound=bound,
                cwd=resolved_cwd,
            )
        finally:
            if bound is not None:
                with contextlib.suppress(OSError):
                    shutil.rmtree(bound.private_dir)

    if event == "PreToolUse":

        async def pre_hook(tool_name: str, arguments: dict[str, Any]) -> ToolPreDecision | None:
            if not match_hook(matcher, tool_name):
                return None
            envelope = build_envelope(
                hook_event_name=event,
                session_id=fallback_session_id,
                cwd=resolved_cwd,
                harness=harness,
                tool_name=tool_name,
                tool_input=arguments,
            )
            verdict = await _guarded_execute(envelope)
            if verdict.outcome == "allow":
                return ToolPreDecision(decision="allow", updated_input=verdict.updated_input)
            return ToolPreDecision(decision="deny", reason=verdict.reason)

        return pre_hook

    if event == "PostToolUse":

        async def post_hook(
            tool_name: str,
            arguments: dict[str, Any],
            result: Any,
            error: BaseException | None,
        ) -> ToolPostDecision | None:
            if not match_hook(matcher, tool_name):
                return None
            tool_response = result if error is None else {"error": str(error)}
            envelope = build_envelope(
                hook_event_name=event,
                session_id=fallback_session_id,
                cwd=resolved_cwd,
                harness=harness,
                tool_name=tool_name,
                tool_input=arguments,
                tool_response=tool_response,
            )
            verdict = await _guarded_execute(envelope)
            # PostToolUse cannot un-run the call; a deny/error becomes an
            # advisory note (matches "block on an advisory event" -- the
            # reason is surfaced, not enforced).
            if verdict.outcome in ("deny", "error") and verdict.reason:
                return ToolPostDecision(reason=verdict.reason)
            return None

        return post_hook

    # Remaining supported events route through HookBus (advisory unless
    # USER_PROMPT_SUBMIT, which is blocking).
    async def bus_hook(**kwargs: Any) -> None:
        tool_response = kwargs.get("tool_response")
        if event == "PostToolUseFailure" and tool_response is None and "error" in kwargs:
            tool_response = {"error": str(kwargs.get("error"))}
        envelope = build_envelope(
            hook_event_name=event,
            session_id=str(kwargs.get("session_id") or fallback_session_id),
            cwd=resolved_cwd,
            harness=harness,
            tool_name=kwargs.get("tool_name"),
            tool_input=kwargs.get("tool_input"),
            tool_response=tool_response,
            prompt=kwargs.get("prompt"),
            model=kwargs.get("model"),
            permission_mode=kwargs.get("permission_mode"),
        )
        verdict = await _guarded_execute(envelope)
        if verdict.outcome == "deny":
            if blocking:
                raise PermissionError(verdict.reason or "denied by external hook")
            logger.info("external hook %r reported block: %s", command, verdict.reason)
        elif verdict.outcome == "error":
            logger.warning("external hook %r failed: %s", command, verdict.reason)

    return bus_hook
