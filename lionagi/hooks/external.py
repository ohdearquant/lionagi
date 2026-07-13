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
import re
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
    "external_hook_adapter",
    "is_command_trusted",
    "match_hook",
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
    """``sha256(json.dumps(argv))`` -- the trust-record key for one hook command."""
    return hashlib.sha256(json.dumps(command).encode()).hexdigest()


def is_command_trusted(command: list[str], *, source: str | None) -> bool:
    """Loader trust rule: an entry with no ``source`` is project/user-authored
    and trusted as code; any non-empty ``source`` (``imported:claude``,
    ``imported:codex``, ...) requires the argv hash to already be recorded in
    ``~/.lionagi/settings.yaml``'s ``trusted_hook_commands``.
    """
    if not source:
        return True
    from lionagi.plugins._user_settings import read_user_settings

    trusted = read_user_settings().get("trusted_hook_commands", [])
    if not isinstance(trusted, list):
        return False
    return compute_command_hash(command) in trusted


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
        if not is_command_trusted(command, source=source):
            reason = (
                f"untrusted hook command {command!r} (source={source!r}); "
                "run `li hooks trust` to approve it"
            )
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
