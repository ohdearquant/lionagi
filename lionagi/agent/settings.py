# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Load and merge agent settings from global + project-local .lionagi/settings.yaml."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from lionagi.ln._proc import communicate_in_process_group

__all__ = (
    "apply_hooks_from_settings",
    "load_settings",
    "parse_external_hooks",
)

from lionagi._settings import _deep_merge as _deep_merge
from lionagi._settings import load_settings

from .spec import AgentSpec

logger = logging.getLogger(__name__)

_DEFAULT_TRUSTED_HOOK_MODULES: frozenset[str] = frozenset({"lionagi.agent.hooks"})


def apply_hooks_from_settings(
    config: AgentSpec,
    settings: dict[str, Any] | None = None,
    *,
    trusted_hook_modules: set[str] | frozenset[str] | None = None,
) -> AgentSpec:
    """Resolve hook specs from settings and register them on the AgentSpec; returns config.

    Handles two independent blocks: legacy ``hooks:`` (in-process callables,
    attached to ``config.hook_handlers`` here) and ``hooks_external:``
    (external commands, attached to ``config.external_hooks`` for
    ``create_agent`` to wire once a ``Branch`` exists). Unrelated to the
    same-named field on a plugin manifest, which is inert data only.
    """
    if settings is None:
        settings = load_settings()

    if trusted_hook_modules is None:
        trusted_hook_modules = _DEFAULT_TRUSTED_HOOK_MODULES

    hooks_config = settings.get("hooks", {})

    for phase in ("pre", "post", "on_error"):
        phase_config = hooks_config.get(phase, {})
        for tool_name, hook_specs in phase_config.items():
            if not isinstance(hook_specs, list):
                hook_specs = [hook_specs]
            for spec in hook_specs:
                handler = _resolve_hook_spec(spec, phase, tool_name, trusted_hook_modules)
                if handler is None:
                    continue
                if phase == "pre":
                    config.pre(tool_name, handler)
                elif phase == "post":
                    config.post(tool_name, handler)
                elif phase == "on_error":
                    config.on_error(tool_name, handler)

    external_config = settings.get("hooks_external", {})
    if external_config:
        config.external_hooks.extend(parse_external_hooks(external_config))

    return config


def parse_external_hooks(external_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse + validate a ``hooks_external:`` block into flat entry dicts.

    Each returned entry is ``{event, matcher, command, timeout, source}``.
    Raises ``ExternalHookConfigError`` (a ``ValueError`` subtype) at the
    first unmappable event, non-``command`` type, or invalid argv -- config
    load fails loud rather than silently dropping a declared guard.
    """
    from lionagi.hooks.external import (
        SUPPORTED_EVENTS,
        ExternalHookConfigError,
        validate_argv,
    )

    if not isinstance(external_config, dict):
        raise ExternalHookConfigError(
            f"hooks_external: must be a mapping of event -> matcher groups, got {external_config!r}"
        )

    entries: list[dict[str, Any]] = []
    for event, matcher_groups in external_config.items():
        if event not in SUPPORTED_EVENTS:
            raise ExternalHookConfigError(
                f"hooks_external: no seam for event {event!r}; LionAGI has no "
                f"hook point for it. Supported events: {sorted(SUPPORTED_EVENTS)}"
            )
        if not isinstance(matcher_groups, list):
            matcher_groups = [matcher_groups]
        for group in matcher_groups:
            if not isinstance(group, dict):
                raise ExternalHookConfigError(
                    f"hooks_external.{event}: entry must be a mapping, got {group!r}"
                )
            matcher = group.get("matcher")
            hook_specs = group.get("hooks", [])
            if not isinstance(hook_specs, list):
                hook_specs = [hook_specs]
            for spec in hook_specs:
                if not isinstance(spec, dict):
                    raise ExternalHookConfigError(
                        f"hooks_external.{event}: hook entry must be a mapping, got {spec!r}"
                    )
                hook_type = spec.get("type", "command")
                if hook_type != "command":
                    raise ExternalHookConfigError(
                        f"hooks_external.{event}: unsupported hook type {hook_type!r}; "
                        "only 'command' is implemented (reserved: http, mcp_tool, prompt)"
                    )
                command = validate_argv(spec.get("command"))
                entries.append(
                    {
                        "event": event,
                        "matcher": matcher,
                        "command": command,
                        "timeout": float(spec.get("timeout", 60.0)),
                        "source": spec.get("source"),
                    }
                )
    return entries


def _resolve_hook_spec(
    spec: dict | str,
    phase: str,
    tool_name: str,
    trusted_hook_modules: set[str] | frozenset[str],
) -> Callable | None:
    """Resolve a hook spec dict or import-path string to an async callable."""
    if isinstance(spec, str):
        return _import_hook(spec, trusted_hook_modules=trusted_hook_modules)

    if isinstance(spec, dict):
        if "python" in spec:
            return _import_hook(spec["python"], trusted_hook_modules=trusted_hook_modules)
        if "command" in spec:
            return _make_shell_hook(spec["command"], phase, tool_name)

    return None


def _import_hook(
    import_path: str,
    *,
    trusted_hook_modules: set[str] | frozenset[str],
) -> Callable | None:
    """Import a hook function from 'module.path:function_name'."""
    if ":" not in import_path:
        return None
    module_path, _, func_name = import_path.rpartition(":")
    if module_path not in trusted_hook_modules:
        raise PermissionError(
            f"Untrusted hook module {module_path!r}. Add it to trusted_hook_modules to allow."
        )
    try:
        from lionagi import ln

        module = ln.import_module(module_path)
        return getattr(module, func_name)
    except (ImportError, AttributeError):
        return None


async def _run_hook(argv: list[str], payload: bytes) -> tuple[int | None, bytes]:
    """Run one hook command to completion and return its exit code and stderr.

    Whatever ends the exchange early (a timeout, a failed exchange, a cancelled tool
    call) kills the hook's whole process group before the exception leaves, so
    neither the hook nor anything it started keeps running with side effects after
    the hook is given up on.
    """
    returncode, _, stderr_bytes = await communicate_in_process_group(argv, payload, timeout=10)
    return returncode, stderr_bytes


def _make_shell_hook(command_template: list[str], phase: str, tool_name: str) -> Callable:
    """Create an async hook running an argv command; pre-hooks raise PermissionError on non-zero exit."""
    if not isinstance(command_template, list) or not all(
        isinstance(x, str) for x in command_template
    ):
        raise ValueError(
            f"Hook command must be an argv list (e.g. ['ruff', 'format', '{{file_path}}']), "
            f"not a shell string. Got: {command_template!r}"
        )

    def _render_argv(values: dict[str, Any]) -> list[str]:
        rendered = []
        for part in command_template:
            for key, value in values.items():
                part = part.replace(f"{{{key}}}", str(value))
            rendered.append(part)
        return rendered

    if phase == "pre":

        async def shell_pre_hook(tn: str, action: str, args: dict) -> dict | None:
            argv = _render_argv(args)
            # The payload is serialized before the child exists, so a value that
            # cannot be serialized refuses the call without leaving a child behind.
            try:
                payload = json.dumps(args).encode()
            except Exception as e:
                raise PermissionError(f"Hook execution error: {e}") from e
            try:
                returncode, stderr_bytes = await _run_hook(argv, payload)
            except asyncio.TimeoutError as err:
                raise PermissionError(f"Hook timed out: {argv[0]!r}") from err
            except Exception as e:
                raise PermissionError(f"Hook execution error: {e}") from e
            if returncode != 0:
                msg = stderr_bytes.decode(errors="replace").strip() or f"Hook blocked: {argv[0]!r}"
                raise PermissionError(msg)
            return None

        return shell_pre_hook

    else:

        async def shell_post_hook(tn: str, action: str, args: dict, result: dict) -> dict | None:
            argv = _render_argv({**args, **result})
            # Serialized before the child exists, as in the pre-hook.
            try:
                payload = json.dumps(result).encode()
            except Exception as exc:
                logger.warning("hook subprocess error (swallowed)", exc_info=exc)
                return None
            try:
                await _run_hook(argv, payload)
            except asyncio.TimeoutError as exc:
                logger.warning("hook subprocess timed out (swallowed)", exc_info=exc)
            except Exception as exc:
                logger.warning("hook subprocess error (swallowed)", exc_info=exc)
            return None

        return shell_post_hook
