# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Resolve, at submit time, the MCP servers a spawned leg should be given.

A CLI-backed agent otherwise discovers MCP servers by walking up from its own
working directory, making its tool surface depend on *where it was told to
work* rather than the submission. See docs/internals/cli.md for why the
config is snapshotted here (not passed as a path) and why "nothing configured"
and "something configured but unusable" are returned as distinct states.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = (
    "MCP_CONFIG_FILENAME",
    "McpConfigError",
    "McpResolution",
    "discover_mcp_config",
    "resolve_spawn_mcp_servers",
)

MCP_CONFIG_FILENAME = ".mcp.json"


class McpConfigError(ValueError):
    """A named MCP config cannot be used. Its own type, so a caller can refuse
    the spawn on it without also catching every other ValueError raised on the
    way there."""


@dataclass(frozen=True)
class McpResolution:
    """What the submitting side resolved, and why it is what it is.

    ``servers`` is None whenever no server set could be produced; ``reason`` is
    then a short machine-readable token saying why, and is None only when the
    caller explicitly asked for no config at all.
    """

    servers: dict[str, Any] | None
    reason: str | None
    source: Path | None
    searched_from: Path
    # True only when the caller named the config file. A set found by walking up
    # from the launch directory reads identically once resolved, so consumers
    # that must tell "someone asked for these servers" from "these were lying
    # around" cannot recover the difference from ``servers`` or ``source``.
    explicit: bool = False

    @property
    def ok(self) -> bool:
        return self.servers is not None


def discover_mcp_config(start: str | Path) -> Path | None:
    """Nearest ``.mcp.json`` at or above *start*, or None.

    Mirrors how a CLI agent finds its own config, so that resolving here from
    the submitting directory produces what the child would have found had it
    been started there.
    """
    current = Path(start).expanduser().resolve()
    for directory in (current, *current.parents):
        candidate = directory / MCP_CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def _reject_json_constant(token: str) -> Any:
    """Refuse ``NaN``/``Infinity``/``-Infinity``, which json.loads accepts by default.

    Those three are a Python extension, not JSON. Accepting one here turns it
    into a Python float that the snapshot handed to the child re-emits as the
    same non-standard token, so a config nothing else could parse propagates
    into a file the child's own reader has to parse. Refusing at the read names
    the config the operator actually wrote, and does it before anything is
    spawned; a refusal at the write would name a file this code generated.
    """
    raise ValueError(
        f"{token} is not JSON: it is a Python extension the standard library "
        "accepts on read, and no other reader of this config has to"
    )


def _read_servers(path: Path) -> dict[str, Any]:
    """Parse ``{"mcpServers": {...}}`` from *path*; raises ValueError on any
    shape this cannot hand to a provider."""
    try:
        raw = path.read_text()
    except OSError as exc:
        raise McpConfigError(f"could not read {path}: {exc}") from exc
    try:
        data = json.loads(raw, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        raise McpConfigError(f"{path} is not valid JSON: {exc}") from exc
    except ValueError as exc:
        raise McpConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise McpConfigError(f"{path} must contain a JSON object, got {type(data).__name__}")
    servers = data.get("mcpServers")
    if servers is None:
        raise McpConfigError(f"{path} has no 'mcpServers' key")
    if not isinstance(servers, dict):
        raise McpConfigError(
            f"{path}: 'mcpServers' must be an object, got {type(servers).__name__}"
        )
    return servers


def resolve_spawn_mcp_servers(
    explicit: str | Path | None = None,
    *,
    launch_dir: str | Path,
    disabled: bool = False,
) -> McpResolution:
    """Resolve the server set to hand a child spawned from *launch_dir*.

    *explicit* names a config file directly and is an error when it cannot be
    used — a caller who named a file is not asking for a silent fallback.
    Without one, the nearest config at or above *launch_dir* is used. *disabled*
    is the caller saying "no MCP servers", which is a choice and not a failure.
    """
    searched_from = Path(launch_dir).expanduser().resolve()

    if disabled:
        return McpResolution(None, None, None, searched_from)

    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = (searched_from / path).resolve()
        if not path.is_file():
            raise McpConfigError(f"--mcp-config {str(explicit)!r} is not a readable file ({path})")
        return McpResolution(_read_servers(path), None, path, searched_from, explicit=True)

    found = discover_mcp_config(searched_from)
    if found is None:
        return McpResolution(None, "no_mcp_config_found", None, searched_from)
    try:
        servers = _read_servers(found)
    except McpConfigError as exc:
        return McpResolution(None, f"mcp_config_unusable:{exc}", found, searched_from)
    if not servers:
        return McpResolution(None, "mcp_config_declares_no_servers", found, searched_from)
    return McpResolution(servers, None, found, searched_from)
