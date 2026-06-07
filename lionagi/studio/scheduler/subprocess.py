# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0027 subprocess spawning for scheduled runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re

_log = logging.getLogger(__name__)

_TEMPLATE_RE = re.compile(r"\{\{(\w+)\}\}")

# ADR-0027 defines the closed set of action kinds.  The CLI parser accepts
# "playbook" as an alias for "play" for backward compatibility.
_VALID_ACTION_KINDS = frozenset({"agent", "flow", "fanout", "play"})
_ALIAS_ACTION_KINDS: dict[str, str] = {"playbook": "play"}


def _render_template(template: str, context: dict) -> str:
    """Replace {{var}} placeholders with values from trigger context."""

    def _replace(m: re.Match) -> str:
        key = m.group(1)
        # Look in github events first
        events = context.get("github_events", [])
        if events and isinstance(events, list) and isinstance(events[0], dict):
            val = events[0].get(key)
            if val is not None:
                return str(val)
        return context.get(key, m.group(0))

    return _TEMPLATE_RE.sub(_replace, template)


def build_argv(schedule: dict, trigger_context: dict) -> list[str]:
    kind = schedule["action_kind"]
    # Normalize legacy alias and validate against the closed set (LIONAGI-AUDIT-003).
    kind = _ALIAS_ACTION_KINDS.get(kind, kind)
    if kind not in _VALID_ACTION_KINDS:
        raise ValueError(
            f"Unknown action_kind {kind!r}. Valid kinds: {sorted(_VALID_ACTION_KINDS)}"
        )
    model = schedule.get("action_model") or ""
    prompt = schedule.get("action_prompt") or ""
    agent = schedule.get("action_agent")
    playbook = schedule.get("action_playbook")
    project = schedule.get("action_project")
    extra = schedule.get("action_extra_args") or []

    # Render template variables from trigger context
    if prompt:
        prompt = _render_template(prompt, trigger_context)

    argv = ["uv", "run", "li"]

    if kind == "agent":
        argv += ["agent", model, prompt]
        if agent:
            argv += ["--agent", agent]
    elif kind == "flow":
        argv += ["o", "flow", model, prompt]
    elif kind == "fanout":
        argv += ["o", "fanout", model, prompt]
    elif kind == "play":
        argv += ["play"]
        if playbook:
            argv.append(playbook)

    if project:
        argv += ["--project", project]

    if isinstance(extra, list):
        argv.extend(str(a) for a in extra)

    return argv


async def spawn_and_wait(argv: list[str], invocation_id: str) -> tuple[int, str]:
    """Spawn subprocess and wait for completion. Returns (exit_code, stderr_tail)."""
    env = {**os.environ, "LIONAGI_INVOCATION_ID": invocation_id}

    _log.info("Spawning: %s", " ".join(argv))
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    try:
        _, stderr = await proc.communicate()
    except asyncio.CancelledError:
        # Cancellation (e.g. scheduler shutdown) must not leave the spawned
        # `uv run li` child detached. Terminate it, give it a moment to exit,
        # then kill, before re-raising so the caller can record the cancel.
        _log.warning("spawn_and_wait cancelled; terminating child for %s", invocation_id)
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (TimeoutError, asyncio.TimeoutError):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
        raise

    exit_code = proc.returncode or 0
    stderr_tail = (stderr[-2048:] if stderr else b"").decode(errors="replace")

    _log.info("Process exited with code %d", exit_code)
    return exit_code, stderr_tail
