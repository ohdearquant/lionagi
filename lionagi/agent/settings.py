# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Load and merge agent settings from global + project-local .lionagi/settings.yaml."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lionagi.ln._proc import aterminate_process_group

__all__ = (
    "apply_hooks_from_settings",
    "load_settings",
)

import yaml

from lionagi.libs.nested import deep_merge as _deep_merge_impl

from .spec import AgentSpec

logger = logging.getLogger(__name__)

_DEFAULT_TRUSTED_HOOK_MODULES: frozenset[str] = frozenset({"lionagi.agent.hooks"})


def load_settings(
    project_dir: str | Path | None = None,
    *,
    include_project: bool = True,
) -> dict[str, Any]:
    """Load and merge ~/.lionagi/settings.yaml with project-local override; auto-detects cwd."""
    merged: dict[str, Any] = {}

    global_path = Path.home() / ".lionagi" / "settings.yaml"
    if global_path.is_file():
        with open(global_path) as f:
            global_settings = yaml.safe_load(f) or {}
        _deep_merge(merged, global_settings)

    if not include_project:
        return merged

    if project_dir:
        local_path = Path(project_dir) / ".lionagi" / "settings.yaml"
        if local_path.is_file():
            with open(local_path) as f:
                local_settings = yaml.safe_load(f) or {}
            _deep_merge(merged, local_settings)
    else:
        cwd = Path.cwd()
        for parent in [cwd, *cwd.parents]:
            candidate = parent / ".lionagi" / "settings.yaml"
            if candidate.is_file():
                with open(candidate) as f:
                    local_settings = yaml.safe_load(f) or {}
                _deep_merge(merged, local_settings)
                break

    return merged


def apply_hooks_from_settings(
    config: AgentSpec,
    settings: dict[str, Any] | None = None,
    *,
    trusted_hook_modules: set[str] | frozenset[str] | None = None,
) -> AgentSpec:
    """Resolve hook specs from settings and register them on the AgentSpec; returns config."""
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

    return config


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
        module = importlib.import_module(module_path)
        return getattr(module, func_name)
    except (ImportError, AttributeError):
        return None


async def _wait_proc(proc: asyncio.subprocess.Process, grace: float = 2.0) -> None:  # type: ignore[name-defined]
    """Await process exit with a bounded grace period; suppress errors."""
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except Exception:  # noqa: BLE001
        logger.debug("Timed out waiting for process %s to exit", proc.pid, exc_info=True)


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
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                _, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(json.dumps(args).encode()),
                    timeout=10,
                )
            except asyncio.TimeoutError as err:
                # Kill the whole process group so lingering children can't
                # continue side effects after the hook times out.
                if proc is not None:
                    await aterminate_process_group(proc, grace=None)
                    await _wait_proc(proc)
                raise PermissionError(f"Hook timed out: {argv[0]!r}") from err
            except Exception as e:
                raise PermissionError(f"Hook execution error: {e}") from e
            if proc.returncode != 0:
                msg = stderr_bytes.decode(errors="replace").strip() or f"Hook blocked: {argv[0]!r}"
                raise PermissionError(msg)
            return None

        return shell_pre_hook

    else:

        async def shell_post_hook(tn: str, action: str, args: dict, result: dict) -> dict | None:
            argv = _render_argv({**args, **result})
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                await asyncio.wait_for(
                    proc.communicate(json.dumps(result).encode()),
                    timeout=10,
                )
            except asyncio.TimeoutError as exc:
                # Kill process group on post-hook timeout so no delayed side effects
                # occur after the hook is silently dropped.
                if proc is not None:
                    await aterminate_process_group(proc, grace=None)
                    await _wait_proc(proc)
                logger.warning("hook subprocess timed out (swallowed)", exc_info=exc)
            except Exception as exc:
                logger.warning("hook subprocess error (swallowed)", exc_info=exc)
            return None

        return shell_post_hook


def _deep_merge(base: dict, override: dict) -> dict:
    return _deep_merge_impl(base, override, mutate=True)
