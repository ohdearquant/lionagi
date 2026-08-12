# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Reusable hook library + provider-neutral event assembly.

Hooks are defined ONCE, by name, in ``LIONAGI_HOME/hooks/library.json``
("check and block dangerous commands", "memory injection", ...). Consumers —
agent profiles, the Studio Operator — never restate commands; they carry an
*assembly*: a list of attachments ``{hook, event, matcher?}`` binding a named
library hook to a provider-neutral event. At launch time the assembly is
materialized into whatever the executing provider understands (the Claude CLI
settings ``hooks`` block today; the codex config and lionagi's own in-process
hook seams ride the same neutral vocabulary later), so the same assembly works
identically whichever CLI runs the agent.

Neutral events::

    pre_tool       before a tool call        -> PreToolUse
    post_tool      after a tool call         -> PostToolUse
    prompt_submit  user prompt submitted     -> UserPromptSubmit
    post_response  assistant turn finished   -> Stop
    session_start  session opened/resumed    -> SessionStart
    session_end    session closed            -> SessionEnd
"""

from __future__ import annotations

import json
import logging
from typing import Any

from lionagi._paths import LIONAGI_HOME

from ..registry import studio_route

_log = logging.getLogger(__name__)

_LIBRARY_PATH = LIONAGI_HOME / "hooks" / "library.json"

# Provider-neutral event vocabulary -> Claude CLI settings event names.
NEUTRAL_EVENTS: dict[str, str] = {
    "pre_tool": "PreToolUse",
    "post_tool": "PostToolUse",
    "prompt_submit": "UserPromptSubmit",
    "post_response": "Stop",
    "session_start": "SessionStart",
    "session_end": "SessionEnd",
}


class HookLibraryError(ValueError):
    """A hook definition or assembly is unusable."""


# ── Library storage ──────────────────────────────────────────────────────────


def validate_hook_def(name: str, spec: Any) -> dict[str, Any]:
    if not isinstance(name, str) or not name.strip():
        raise HookLibraryError("hook name must be a non-empty string")
    if not isinstance(spec, dict):
        raise HookLibraryError(f"hook {name!r}: definition must be an object")
    unknown = set(spec) - {"description", "command", "timeout"}
    if unknown:
        raise HookLibraryError(f"hook {name!r}: unknown keys {sorted(unknown)}")
    command = spec.get("command")
    if not isinstance(command, str) or not command.strip():
        raise HookLibraryError(f"hook {name!r}: needs a non-empty string 'command'")
    description = spec.get("description", "")
    if not isinstance(description, str):
        raise HookLibraryError(f"hook {name!r}: description must be a string")
    timeout = spec.get("timeout")
    if timeout is not None and not isinstance(timeout, int | float):
        raise HookLibraryError(f"hook {name!r}: timeout must be a number")
    out: dict[str, Any] = {"description": description, "command": command}
    if timeout is not None:
        out["timeout"] = timeout
    return out


def read_library() -> dict[str, dict[str, Any]]:
    """Named hook definitions; empty when the file is absent.

    A broken file raises — a hand-edited library must not silently read as
    empty while every assembly referencing it starts failing resolution.
    """
    try:
        raw = _LIBRARY_PATH.read_text()
    except FileNotFoundError:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HookLibraryError(f"stored hook library is invalid JSON: {exc}") from exc
    hooks = payload.get("hooks") if isinstance(payload, dict) else None
    if not isinstance(hooks, dict):
        raise HookLibraryError("stored hook library must be {'hooks': {...}}")
    return {name: validate_hook_def(name, spec) for name, spec in hooks.items()}


def _write_library(hooks: dict[str, dict[str, Any]]) -> None:
    _LIBRARY_PATH.parent.mkdir(parents=True, exist_ok=True)
    _LIBRARY_PATH.write_text(json.dumps({"hooks": hooks}, indent=2) + "\n")


def upsert_hook(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    normalized = validate_hook_def(name, spec)
    hooks = read_library()
    hooks[name] = normalized
    _write_library(hooks)
    return normalized


def delete_hook(name: str) -> bool:
    hooks = read_library()
    if name not in hooks:
        return False
    del hooks[name]
    _write_library(hooks)
    return True


# ── Assemblies ───────────────────────────────────────────────────────────────


def validate_attachments(
    attachments: Any, *, library: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Validate ``[{hook, event, matcher?}]``; resolve names when a library is given."""
    if not isinstance(attachments, list):
        raise HookLibraryError("attachments must be a list")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(attachments):
        if not isinstance(item, dict):
            raise HookLibraryError(f"attachments[{i}]: must be an object")
        unknown = set(item) - {"hook", "event", "matcher"}
        if unknown:
            raise HookLibraryError(f"attachments[{i}]: unknown keys {sorted(unknown)}")
        hook = item.get("hook")
        if not isinstance(hook, str) or not hook.strip():
            raise HookLibraryError(f"attachments[{i}]: needs a non-empty string 'hook'")
        event = item.get("event")
        if event not in NEUTRAL_EVENTS:
            raise HookLibraryError(
                f"attachments[{i}]: unknown event {event!r}; known events: {sorted(NEUTRAL_EVENTS)}"
            )
        matcher = item.get("matcher")
        if matcher is not None and not isinstance(matcher, str):
            raise HookLibraryError(f"attachments[{i}]: matcher must be a string")
        if library is not None and hook not in library:
            raise HookLibraryError(f"attachments[{i}]: hook {hook!r} is not in the library")
        entry: dict[str, Any] = {"hook": hook, "event": event}
        if matcher:
            entry["matcher"] = matcher
        out.append(entry)
    return out


def materialize_claude_hooks(attachments: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve an assembly against the library into a Claude settings ``hooks`` block.

    An attachment whose hook has since been deleted from the library is
    skipped with a log line rather than failing the launch — the save-time
    validation is where a dangling name is an error.
    """
    library = read_library()
    by_event: dict[str, list[dict[str, Any]]] = {}
    for att in attachments:
        spec = library.get(att["hook"])
        if spec is None:
            _log.warning(
                "hook %r referenced by an assembly is not in the library; skipping",
                att["hook"],
            )
            continue
        cli_event = NEUTRAL_EVENTS[att["event"]]
        hook_spec: dict[str, Any] = {"type": "command", "command": spec["command"]}
        if spec.get("timeout") is not None:
            hook_spec["timeout"] = spec["timeout"]
        group: dict[str, Any] = {"hooks": [hook_spec]}
        if att.get("matcher"):
            group["matcher"] = att["matcher"]
        by_event.setdefault(cli_event, []).append(group)
    return by_event


# ── Routes ───────────────────────────────────────────────────────────────────


@studio_route("/hooks/library", method="GET", area="hooks", name="list_hook_library")
async def list_hook_library_route() -> dict[str, Any]:
    try:
        hooks = read_library()
    except HookLibraryError as exc:
        return {"path": str(_LIBRARY_PATH), "hooks": {}, "error": str(exc)}
    return {
        "path": str(_LIBRARY_PATH),
        "hooks": hooks,
        "events": sorted(NEUTRAL_EVENTS),
    }


@studio_route("/hooks/library/{name}", method="PUT", area="hooks", name="put_hook_def")
async def put_hook_def_route(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    from fastapi import HTTPException

    try:
        normalized = upsert_hook(name, spec)
    except HookLibraryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"name": name, **normalized}


@studio_route("/hooks/library/{name}", method="DELETE", area="hooks", name="delete_hook_def")
async def delete_hook_def_route(name: str) -> dict[str, Any]:
    from fastapi import HTTPException

    try:
        removed = delete_hook(name)
    except HookLibraryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail=f"hook {name!r} not found")
    return {"ok": True}
