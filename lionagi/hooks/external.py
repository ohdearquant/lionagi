# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""External-hook exec adapter: compatibility profile v1 wire envelope + executor.

Turns one ``hooks_external:`` config entry (an event, an argv command, a
matcher, and a timeout) into an async callable shaped for whichever internal
seam that event maps to: the tool pre/post hook chain at
``ActionManager.invoke`` for ``PreToolUse``/``PostToolUse``, or a
``HookBus`` handler for ``SessionStart``/``SessionEnd``/``UserPromptSubmit``/
``PostToolUseFailure``. The callable spawns the configured command as a real
subprocess, writes the JSON envelope to its stdin, and parses its stdout/exit
code back into a decision.

This is a different executor from ``lionagi.agent.settings._make_shell_hook``,
which stays wired to the legacy ``hooks: {pre,post,on_error}`` shape, never
reads stdout, and collapses every nonzero exit to one ``PermissionError``.

Also distinct from the ``hooks_external`` field on a plugin manifest
(``lionagi.plugins.manifest.Capabilities.hooks_external``): that field is
parsed as inert data only, for trust disclosure, and nothing there executes a
command. This module is the thing that actually runs one.
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
_TERMINATE_GRACE = 2.0


class ExternalHookConfigError(ValueError):
    """A ``hooks_external`` config entry is malformed; fails config load."""


class _HookTimeoutError(Exception):
    """Internal signal: the hook subprocess did not finish within its timeout."""


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
    actually spawn, using the same lookup rule ``create_subprocess_exec``
    delegates to ``execvp``: a name containing a path separator resolves
    relative to *cwd* and is never PATH-searched; a bare name is searched on
    ``PATH`` only (never implicitly relative to *cwd*).

    Raises :class:`ExternalHookConfigError` when the resolved path does not
    exist, is not a file, is not executable, or (for a bare name) is not
    found on ``PATH`` -- an unresolvable command can never be pinned or
    trusted.
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
    """``sha256`` over the resolved executable's file bytes -- the content
    half of the trust pin. A same-path substitution (the file at *path* is
    replaced after approval, e.g. a repo's ``./guard`` swapped for an
    attacker's binary, or a PATH entry reordered onto a different same-named
    binary) changes this digest even though ``resolve_hook_executable``
    returns the same path string."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_trust_record(command: list[str], cwd: str) -> dict[str, str]:
    """Build the full content-pinned trust record for one approval: the argv
    hash (identifies the approval request), plus the resolved executable's
    absolute path and content digest (identifies what will actually run).
    Raises :class:`ExternalHookConfigError` if *command* cannot be resolved
    to an executable right now.
    """
    resolved = resolve_hook_executable(command, cwd)
    return {
        "argv_hash": compute_command_hash(command),
        "resolved_path": str(resolved),
        "content_digest": compute_executable_digest(resolved),
    }


def _trust_status(command: list[str], *, source: str | None, cwd: str) -> tuple[bool, str]:
    """Loader trust rule (ADR-0048 D7, content-pinned): an entry with no
    ``source`` is project/user-authored and trusted as code; any non-empty
    ``source`` (``imported:claude``, ``imported:codex``, ...) requires a
    trust record in ``~/.lionagi/settings.yaml``'s ``trusted_hook_commands``
    whose argv hash, resolved executable path, AND content digest all match
    *command* as it resolves right now -- a prior approval of ``["./guard"]``
    does NOT carry over if the executable it resolves to, or that
    executable's bytes, have changed since approval (the flaw this closes:
    argv-only hashing let a different repository's or a later PATH-resolved
    ``./guard`` run under a stale approval).

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
    """Boolean form of :func:`_trust_status` -- see its docstring for the
    content-pinned matching rule. *cwd* defaults to the process's current
    working directory when omitted (only reached when *source* is falsy,
    where no resolution happens at all)."""
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

    Common fields (``session_id``, ``cwd``, ``hook_event_name``, ``harness``)
    are always present. Per-event fields follow the field-guarantee table:
    ``tool_name``/``tool_input`` always present for tool events;
    ``tool_response`` always present for ``PostToolUse``/``PostToolUseFailure``;
    ``prompt``/``model``/``permission_mode`` always present for
    ``UserPromptSubmit`` (``permission_mode`` defaults to ``"default"`` when no
    policy is attached).
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
            envelope["tool_response"] = tool_response
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


async def _run_hook_process(
    argv: list[str], envelope: dict[str, Any], timeout: float
) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(json.dumps(envelope).encode()),
            timeout=timeout,
        )
    except (TimeoutError, asyncio.TimeoutError) as err:
        # asyncio.wait_for raises asyncio.TimeoutError, which is a distinct
        # class from the builtin TimeoutError before Python 3.11 (they only
        # became aliases in 3.11) -- catch both so the teardown below runs on
        # every supported interpreter, not just 3.11+.
        # Kill the whole process group so a hung hook's children cannot
        # continue side effects after the timeout is declared.
        await aterminate_process_group(proc, grace=None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE)
        raise _HookTimeoutError(f"hook timed out after {timeout}s: {argv[0]!r}") from err

    if len(stdout_bytes) > _MAX_STDOUT_BYTES:
        logger.warning("hook stdout exceeded %d bytes; truncating", _MAX_STDOUT_BYTES)
        stdout_bytes = stdout_bytes[:_MAX_STDOUT_BYTES]
    return proc.returncode, stdout_bytes, stderr_bytes


def _parse_stdout_decision(
    stdout_bytes: bytes,
) -> tuple[str | None, str, dict[str, Any] | None]:
    """Parse exit-0 stdout into ``(permission_decision, reason, updated_input)``.

    Empty stdout, non-empty stdout that fails to parse as JSON, or a JSON
    body with no recognized decision field all return ``(None, "", None)``
    -- "no structured output," never a block. A top-level ``decision`` of
    ``"block"`` normalizes to ``"deny"``; ``"allow"``/``"approve"`` (or an
    explicit ``null``) normalize to ``None`` (allow); any other explicit
    value -- including ``"ask"`` or an unrecognized string -- passes through
    unchanged so the caller's decision switch fails it closed, matching the
    nested ``hookSpecificOutput.permissionDecision`` shape's handling of
    unrecognized values.
    """
    text = stdout_bytes.decode(errors="replace").strip()
    if not text:
        return None, "", None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("hook stdout was non-empty but not valid JSON; treating as no decision")
        return None, "", None
    if not isinstance(data, dict):
        return None, "", None

    hook_specific = data.get("hookSpecificOutput")
    if isinstance(hook_specific, dict) and "permissionDecision" in hook_specific:
        updated_input = hook_specific.get("updatedInput")
        return (
            hook_specific.get("permissionDecision"),
            hook_specific.get("permissionDecisionReason") or "",
            updated_input if isinstance(updated_input, dict) else None,
        )
    if "decision" in data:
        decision = data.get("decision")
        reason = data.get("reason") or ""
        if decision is None or decision in ("allow", "approve"):
            return None, reason, None
        if decision == "block":
            return "deny", reason, None
        # Any other explicit value (e.g. "ask", or an unrecognized string) is
        # handed through as-is so `_execute_hook`'s decision switch applies
        # the same fail-closed handling it uses for the nested
        # `hookSpecificOutput.permissionDecision` shape -- an explicit but
        # unrecognized top-level decision must never fall through to allow.
        return decision, reason, None
    return None, "", None


async def _execute_hook(
    *,
    argv: list[str],
    envelope: dict[str, Any],
    timeout: float,
    blocking: bool,
) -> HookVerdict:
    """Spawn *argv*, exchange *envelope*, and normalize the exit-code + stdout
    contract into a :class:`HookVerdict`.

    Exit 0 -- stdout parsed as JSON if non-empty. Exit 2 -- block; stderr is
    the reason. Any other exit, or a spawn/IO error -- hook failure (deny on
    a blocking seam, error/log-and-continue on an advisory one). A timeout is
    treated the same way after the process group is torn down.
    """
    try:
        returncode, stdout_bytes, stderr_bytes = await _run_hook_process(argv, envelope, timeout)
    except _HookTimeoutError as exc:
        return HookVerdict(outcome="deny" if blocking else "error", reason=str(exc))
    except Exception as exc:  # noqa: BLE001 -- subprocess spawn/IO errors
        return HookVerdict(
            outcome="deny" if blocking else "error",
            reason=f"hook execution error: {exc}",
        )

    if returncode == 2:
        reason = stderr_bytes.decode(errors="replace").strip() or f"hook blocked: {argv[0]!r}"
        return HookVerdict(outcome="deny", reason=reason)

    if returncode != 0:
        reason = (
            stderr_bytes.decode(errors="replace").strip()
            or f"hook failed (exit {returncode}): {argv[0]!r}"
        )
        return HookVerdict(outcome="deny" if blocking else "error", reason=reason)

    decision, reason, updated_input = _parse_stdout_decision(stdout_bytes)
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
    :class:`ToolPreHook`/:class:`ToolPostHook`-shaped callable for
    ``ActionManager``; the remaining supported events return a
    ``HookBus``-shaped ``**kwargs`` handler. ``source`` carries D6's
    provenance field (``None`` for project/user-authored entries,
    ``"imported:claude"``/``"imported:codex"`` for imported ones) -- an
    untrusted non-empty-``source`` command never executes (see
    :func:`is_command_trusted`).
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
        # Re-resolved every call, not cached from adapter construction: the
        # executable a relative/PATH-searched command resolves to can change
        # between approval and this exact invocation (D7 content pinning).
        trusted, reason = _trust_status(command, source=source, cwd=resolved_cwd)
        if not trusted:
            reason = f"{reason}; run `li hooks trust` to approve it"
            return HookVerdict(outcome="deny" if blocking else "error", reason=reason)
        return await _execute_hook(
            argv=command, envelope=envelope, timeout=timeout, blocking=blocking
        )

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
