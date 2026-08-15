# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Operator hook assembly: library attachments -> per-turn ``--settings`` file.

The Operator's engine turn deliberately runs the provider CLI with
``setting_sources: ""`` so nothing user- or project-level is inherited into
the privileged control-plane conversation (MCP servers included). That also
means the CLI loads none of its own hooks. This service is the explicit
injection path: the Operator carries one hook *assembly*
(``LIONAGI_HOME/operator_hooks.json``, editable over the API below) —
attachments binding named hooks from the shared hook library
(services/hooks_library.py) to provider-neutral events — and the engine
materializes just that assembly into a settings file handed to the CLI via
``--settings``. Hooks run, inheritance stays off.

Stored shape::

    {
      "enabled": true,
      "attachments": [
        {"hook": "guard-dangerous-commands", "event": "pre_tool", "matcher": "Bash"},
        {"hook": "memory-injection", "event": "prompt_submit"}
      ]
    }
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from lionagi._paths import LIONAGI_HOME

from ..registry import studio_route
from .hooks_library import (
    HookLibraryError,
    materialize_claude_hooks,
    read_library,
    validate_attachments,
)

_log = logging.getLogger(__name__)

_CONFIG_PATH = LIONAGI_HOME / "operator_hooks.json"
_SETTINGS_PATH = LIONAGI_HOME / "operator_hooks.settings.json"


class OperatorHooksError(ValueError):
    """The Operator hook assembly's shape is unusable."""


def validate_config(config: Any, *, resolve: bool = False) -> dict[str, Any]:
    """Validate and normalize a stored assembly; raises OperatorHooksError.

    ``resolve=True`` additionally requires every attachment's hook to exist
    in the library — used at write time so a dangling name fails the save,
    while reads of an older config still parse.
    """
    if not isinstance(config, dict):
        raise OperatorHooksError(f"config must be an object, got {type(config).__name__}")
    unknown = set(config) - {"enabled", "attachments"}
    if unknown:
        raise OperatorHooksError(f"unknown top-level keys: {sorted(unknown)}")
    enabled = config.get("enabled", True)
    if not isinstance(enabled, bool):
        raise OperatorHooksError("'enabled' must be a boolean")
    try:
        library = read_library() if resolve else None
        attachments = validate_attachments(config.get("attachments", []), library=library)
    except HookLibraryError as exc:
        raise OperatorHooksError(str(exc)) from exc
    return {"enabled": enabled, "attachments": attachments}


def read_config() -> dict[str, Any]:
    """The stored assembly, empty-but-valid when the file is absent."""
    try:
        raw = _CONFIG_PATH.read_text()
    except FileNotFoundError:
        return {"enabled": True, "attachments": []}
    try:
        return validate_config(json.loads(raw))
    except (json.JSONDecodeError, OperatorHooksError) as exc:
        # A hand-edited broken file must not silently disable hooks the
        # human believes are on — surface it at every reader instead.
        raise OperatorHooksError(f"stored hooks config is invalid: {exc}") from exc


def write_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = validate_config(config, resolve=True)
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(normalized, indent=2) + "\n")
    return normalized


def materialize_settings_file() -> Path | None:
    """Write the CLI settings file for the current assembly; None when inert.

    Returns None when hooks are disabled, empty, or the stored config is
    broken — the engine turn then simply runs hook-less, and the API surface
    is where the breakage is reported (a turn must not fail because a hook
    config rotted).
    """
    try:
        config = read_config()
    except OperatorHooksError:
        _log.exception("operator hooks config unusable; running the turn without hooks")
        return None
    if not config["enabled"] or not config["attachments"]:
        return None
    try:
        hooks_block = materialize_claude_hooks(config["attachments"])
    except HookLibraryError:
        _log.exception("hook library unusable; running the turn without hooks")
        return None
    if not hooks_block:
        return None
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(json.dumps({"hooks": hooks_block}, indent=2) + "\n")
    return _SETTINGS_PATH


@studio_route("/operator/hooks", method="GET", area="operator", name="get_operator_hooks")
async def get_operator_hooks_route() -> dict[str, Any]:
    try:
        config = read_config()
    except OperatorHooksError as exc:
        return {"path": str(_CONFIG_PATH), "error": str(exc)}
    return {"path": str(_CONFIG_PATH), **config}


@studio_route("/operator/hooks", method="PUT", area="operator", name="put_operator_hooks")
async def put_operator_hooks_route(config: dict[str, Any]) -> dict[str, Any]:
    from fastapi import HTTPException

    try:
        normalized = write_config(config)
    except OperatorHooksError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"path": str(_CONFIG_PATH), **normalized}
