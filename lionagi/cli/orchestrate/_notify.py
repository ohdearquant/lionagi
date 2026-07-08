# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Outbound completion signal: a generic shell hook fired once a flow/play
invocation reaches its terminal status.

Resolved from `.lionagi/settings.yaml` (`notify.on_terminal`, project
overrides global) or an explicit `--notify` override. lionagi ships no
messaging integration here — the hook is just a shell command template with
three substitution variables (`{payload}`, `{status}`, `{invocation_id}`),
run with a short timeout. Failures are logged and never propagate: they must
never affect the run's own terminal status or exit code.
"""

from __future__ import annotations

import asyncio
import json
import logging

from lionagi.agent.settings import load_settings
from lionagi.ln._proc import aterminate_process_group

from .._logging import warn

__all__ = ("fire_terminal_notify",)

logger = logging.getLogger(__name__)

_HOOK_TIMEOUT = 10.0


def _render_template(template: str, *, status: str, invocation_id: str, payload_json: str) -> str:
    # {payload} is substituted last so its JSON body is never re-scanned by
    # the earlier, narrower substitutions.
    rendered = template.replace("{status}", status).replace("{invocation_id}", invocation_id)
    return rendered.replace("{payload}", payload_json)


async def _await_proc_dead(proc: asyncio.subprocess.Process, grace: float = 2.0) -> None:
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except Exception:  # noqa: BLE001 — best-effort reap, never let this raise
        logger.debug(
            "timed out waiting for notify hook process %s to exit", proc.pid, exc_info=True
        )


async def fire_terminal_notify(
    *,
    invocation_id: str,
    kind: str,
    playbook: str | None,
    status: str,
    save_dir: str | None,
    cwd: str,
    exit_class: str,
    started_at: float,
    ended_at: float,
    override_command: str | None = None,
    project_dir: str | None = None,
) -> None:
    """Fire the configured terminal-notify hook exactly once, best-effort.

    `override_command` (the CLI `--notify` flag) wins over the settings
    value. No template configured on either side is a silent no-op.
    """
    command = override_command
    if not command:
        settings = load_settings(project_dir=project_dir)
        notify_cfg = settings.get("notify") if isinstance(settings, dict) else None
        command = notify_cfg.get("on_terminal") if isinstance(notify_cfg, dict) else None
    if not command:
        return

    payload = {
        "invocation_id": invocation_id,
        "kind": kind,
        "playbook": playbook,
        "status": status,
        "save_dir": save_dir,
        "cwd": cwd,
        "exit_class": exit_class,
        "started_at": started_at,
        "ended_at": ended_at,
    }
    rendered = _render_template(
        command,
        status=status,
        invocation_id=invocation_id,
        payload_json=json.dumps(payload),
    )

    proc = None
    try:
        proc = await asyncio.create_subprocess_shell(
            rendered,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=_HOOK_TIMEOUT)
    except asyncio.TimeoutError:
        if proc is not None:
            await aterminate_process_group(proc, grace=None)
            await _await_proc_dead(proc)
        warn(f"notify.on_terminal hook timed out after {_HOOK_TIMEOUT}s")
        return
    except Exception as exc:  # noqa: BLE001 — a hook failure must never affect the run
        warn(f"notify.on_terminal hook failed to run: {exc}")
        return

    if proc.returncode != 0:
        detail = stderr_bytes.decode(errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        warn(f"notify.on_terminal hook exited {proc.returncode}{suffix}")
