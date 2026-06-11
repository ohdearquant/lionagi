# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""ADR-0027 subprocess spawning for scheduled runs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import tempfile

_log = logging.getLogger(__name__)

_TEMPLATE_RE = re.compile(r"\{\{(\w+)\}\}")

# ADR-0027 defines the closed set of action kinds.  The CLI parser accepts
# "playbook" as an alias for "play" for backward compatibility.
_VALID_ACTION_KINDS = frozenset({"agent", "flow", "fanout", "play", "flow_yaml"})
_ALIAS_ACTION_KINDS: dict[str, str] = {"playbook": "play"}

# action_model must be a safe model-spec token: alphanumerics, dots, slashes,
# colons, hyphens and underscores only.  Values starting with '-' are rejected
# unconditionally to block flag injection into the spawned li process (CWE-88).
_MODEL_RE = re.compile(r"^[a-zA-Z0-9_./:@-]+$")


def _validate_action_model(model: str) -> None:
    """Raise ValueError if *model* could inject CLI flags into the subprocess.

    A model value starting with '-' would be interpreted as a flag by the spawned
    ``li`` process.  Values containing characters outside the safe set are also
    rejected because they have no legitimate use in a model spec.

    Policy: reject loudly rather than silently filtering so callers discover bad
    data at write time rather than at fire time.
    """
    if not model:
        return
    if model.startswith("-"):
        raise ValueError(
            f"action_model {model!r} starts with '-' and would inject a CLI flag "
            "into the spawned li process. Provide a valid model identifier."
        )
    if not _MODEL_RE.match(model):
        raise ValueError(
            f"action_model {model!r} contains characters not allowed in a model "
            "identifier. Allowed: letters, digits, '_', '.', '/', ':', '@', '-'."
        )


def _validate_extra_args(extra: list) -> None:
    """Raise ValueError if any element of *extra* starts with '-'.

    Elements starting with '-' are CLI flags and would be injected verbatim into
    the argv of the spawned li process (CWE-88).  Positional tokens that do not
    start with '-' are accepted.

    Policy: reject loudly with the offending element named so callers can fix the
    schedule spec rather than silently receiving a process that behaves differently
    from what was intended.
    """
    for item in extra:
        token = str(item)
        if token.startswith("-"):
            raise ValueError(
                f"action_extra_args element {token!r} starts with '-' and would "
                "inject a CLI flag into the spawned li process. Only positional "
                "(non-flag) tokens are permitted in action_extra_args."
            )


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

    # Defensive validation: reject flag-injection vectors before touching argv.
    # These checks mirror the service-layer boundary in services/schedules.py;
    # having them here ensures the subprocess is never spawned with injected flags
    # regardless of how the schedule dict was created.
    _validate_action_model(model)
    if isinstance(extra, list):
        _validate_extra_args(extra)

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

    # extra has already been validated above; extend argv with safe positional tokens.
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
        # `uv run li` forks the real worker (and that worker may fork
        # further). Put the whole tree in its own session/process group so a
        # cancel can signal the GROUP, not just the direct child — otherwise
        # grandchildren survive scheduler shutdown as orphans.
        start_new_session=True,
    )
    # Capture the pgid NOW — once the child exits and is reaped,
    # os.getpgid(proc.pid) raises ProcessLookupError and we'd skip the group
    # kill. start_new_session=True makes pgid == proc.pid. Guard mocked pids
    # in tests: a MagicMock.pid coerces to 1, and killpg(1, …) hits init.
    # os.killpg is POSIX-only: on Windows leave _pgid None so the group-kill
    # path is skipped and cleanup falls through to proc.terminate()/kill()
    # instead of raising AttributeError.
    _pgid: int | None = (
        proc.pid if hasattr(os, "killpg") and isinstance(proc.pid, int) and proc.pid > 1 else None
    )

    try:
        _, stderr = await proc.communicate()
    except asyncio.CancelledError:
        # Cancellation (e.g. scheduler shutdown) must not leave the spawned
        # `uv run li` tree detached. SIGTERM the whole group, give it a moment
        # to exit, then SIGKILL the group, before re-raising so the caller can
        # record the cancel.
        _log.warning("spawn_and_wait cancelled; terminating child for %s", invocation_id)
        if _pgid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(_pgid, signal.SIGTERM)
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (TimeoutError, asyncio.TimeoutError):
            if _pgid is not None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(_pgid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
        raise
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)

    exit_code = proc.returncode or 0
    stderr_tail = (stderr[-2048:] if stderr else b"").decode(errors="replace")

    _log.info("Process exited with code %d", exit_code)
    return exit_code, stderr_tail
