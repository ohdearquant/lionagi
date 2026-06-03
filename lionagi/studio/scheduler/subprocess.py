# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0027 subprocess spawning for scheduled runs."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile

_log = logging.getLogger(__name__)

_TEMPLATE_RE = re.compile(r"\{\{(\w+)\}\}")


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


def build_argv(schedule: dict, trigger_context: dict) -> tuple[list[str], str | None]:
    """Build the subprocess argv for a scheduled action.

    Returns ``(argv, tmp_path)`` where ``tmp_path`` is a temporary file that
    must be deleted after the subprocess exits (only set for ``flow_yaml``).
    """
    kind = schedule["action_kind"]
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
    tmp_path: str | None = None

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
    elif kind == "flow_yaml":
        # Write the inline YAML spec to a temp file so `li o flow -f <path>`
        # can read it.  The caller is responsible for deleting tmp_path after
        # the subprocess exits.
        yaml_text = schedule.get("action_flow_yaml") or ""
        fd, tmp_path = tempfile.mkstemp(suffix=".yaml", prefix="lionagi-sched-")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(yaml_text)
        except Exception:
            os.unlink(tmp_path)
            raise
        argv += ["o", "flow", model, prompt, "-f", tmp_path]

    if project:
        argv += ["--project", project]

    if isinstance(extra, list):
        argv.extend(str(a) for a in extra)

    return argv, tmp_path


async def spawn_and_wait(
    argv: list[str],
    invocation_id: str,
    *,
    tmp_path: str | None = None,
) -> tuple[int, str]:
    """Spawn subprocess and wait for completion. Returns (exit_code, stderr_tail).

    If *tmp_path* is given it is deleted after the subprocess exits — used by
    the ``flow_yaml`` action kind which writes a temp spec file before spawning.
    """
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
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    exit_code = proc.returncode or 0
    stderr_tail = (stderr[-2048:] if stderr else b"").decode(errors="replace")

    _log.info("Process exited with code %d", exit_code)
    return exit_code, stderr_tail
