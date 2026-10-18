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

from lionagi._paths import is_account_home
from lionagi.mcp._errors import SubmitInputError

__all__ = (
    "MCP_CONFIG_FILENAME",
    "McpConfigError",
    "McpProfilePolicy",
    "McpResolution",
    "SNAPSHOT_SEARCHED_FROM_KEY",
    "SNAPSHOT_SOURCE_KEY",
    "apply_profile_mcp_policy",
    "discover_mcp_config",
    "explicit_mcp_config_path",
    "resolve_spawn_mcp_servers",
)

MCP_CONFIG_FILENAME = ".mcp.json"

# Carried by a snapshot of a config that was found rather than named, naming the
# file it was taken from. A submitted run is pointed at its snapshot with
# --mcp-config, the flag a caller names a file with, so without this key the set
# a search found would reach the run as one the caller asked for.
SNAPSHOT_SOURCE_KEY = "lionagiSnapshotOf"
# Beside it, the directory that search began in. When the submission names a cwd
# other than that directory, the run reading the snapshot starts there, so its own
# directory cannot stand in for this one.
SNAPSHOT_SEARCHED_FROM_KEY = "lionagiSnapshotSearchedFrom"


class McpConfigError(SubmitInputError):
    """A named MCP config cannot be used. Its own type, so a caller can refuse
    the spawn on it without also catching every other ValueError raised on the
    way there."""


@dataclass(frozen=True)
class McpProfilePolicy:
    """Normalized MCP authority declared by an agent profile.

    ``enabled=False`` is an exact refusal. ``servers`` is an exact allowlist
    over whichever catalog the higher-precedence layer resolved. ``None`` for
    either field means the profile is silent on that dimension.
    """

    enabled: bool | None = None
    servers: tuple[str, ...] | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {}
        if self.enabled is not None:
            value["enabled"] = self.enabled
        if self.servers is not None:
            value["servers"] = list(self.servers)
        return value


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
    # A profile policy is also an authored authority decision, but it remains
    # distinct from ``explicit`` because a submit-time config must outrank it.
    profile_applied: bool = False

    @property
    def ok(self) -> bool:
        return self.servers is not None

    @property
    def declared_empty(self) -> bool:
        """Whether "no servers" is the caller's answer rather than a failure to find one.

        True for ``--no-mcp-config`` and for a named config that lists no
        servers. A search that found nothing, or found a config declaring none,
        is not a declaration: the leg is told it keeps whatever its own CLI
        discovers, so it must not have that suppressed.
        """
        if self.servers:
            return False
        if self.servers is not None:
            return True
        return self.reason is None

    @property
    def exact(self) -> bool:
        """Whether ``servers`` is the complete set for this spawn.

        A declared empty set is exact. So is any set a profile policy produced:
        an allowlist selects from the catalog, and handing its survivors over
        additively lets the child merge the excluded names back in from the
        provider's own configuration -- the second discovery the allowlist
        exists to prevent. A non-empty set that came from a named or discovered
        config is not exact; that handoff stays additive.
        """
        return self.profile_applied or self.declared_empty


def discover_mcp_config(start: str | Path) -> Path | None:
    """Nearest ``.mcp.json`` at or above *start*, or None.

    Mirrors how a CLI agent finds its own config, so that resolving here from
    the submitting directory produces what the child would have found had it
    been started there.
    """
    current = Path(start).expanduser().resolve()
    for directory in (current, *current.parents):
        candidate = directory / MCP_CONFIG_FILENAME
        if candidate.is_file() and not is_account_home(directory):
            return candidate
    return None


def apply_profile_mcp_policy(
    resolution: McpResolution,
    policy: McpProfilePolicy | None,
) -> McpResolution:
    """Apply a role policy below an explicit call and above ambient discovery.

    An allowlist is an exact decision even when discovery found nothing: it
    becomes ``{}``, not ``None``, so a child cannot perform its own second
    discovery and regain servers outside the role's authority.
    """
    if policy is None:
        return resolution
    if policy.enabled is False:
        return McpResolution(
            {},
            "profile_mcp_disabled",
            None,
            resolution.searched_from,
            profile_applied=True,
        )
    if policy.servers is None:
        return resolution

    available = resolution.servers or {}
    selected = {name: available[name] for name in policy.servers if name in available}
    reason = resolution.reason if resolution.servers is None else None
    return McpResolution(
        selected,
        reason or "profile_mcp_allowlist",
        resolution.source,
        resolution.searched_from,
        profile_applied=True,
    )


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
    return _read_config(path)["mcpServers"]


def _read_config(path: Path) -> dict[str, Any]:
    """The config object at *path*, its ``mcpServers`` checked as
    :func:`_read_servers` documents."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise McpConfigError(f"could not read {path}: {exc}") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise McpConfigError(f"{path} is not valid UTF-8: {exc}") from exc
    try:
        data = json.loads(text, parse_constant=_reject_json_constant)
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
    return data


def explicit_mcp_config_path(explicit: str | Path, *, launch_dir: str | Path) -> Path:
    """The file a named config is read from: a relative name is taken from *launch_dir*."""
    path = Path(explicit).expanduser()
    if path.is_absolute():
        return path
    return (Path(launch_dir).expanduser().resolve() / path).resolve()


def _snapshot_path(data: dict[str, Any], key: str, path: Path) -> Path:
    """A path a snapshot records, which its writer always makes absolute."""
    value = data[key]
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise McpConfigError(f"{path}: {key!r} must be an absolute path, got {value!r}")
    return Path(value)


def resolve_spawn_mcp_servers(
    explicit: str | Path | None = None,
    *,
    launch_dir: str | Path,
    disabled: bool = False,
    profile_policy: McpProfilePolicy | None = None,
) -> McpResolution:
    """Resolve the server set to hand a child spawned from *launch_dir*.

    *explicit* names a config file directly and is an error when it cannot be
    used — a caller who named a file is not asking for a silent fallback.
    Without one, the nearest config at or above *launch_dir* is used. *disabled*
    is the caller saying "no MCP servers", which is a choice and not a failure.
    *profile_policy* is a role's own MCP authority, applied to a set this
    submission resolves; a named *explicit* config outranks it.
    """
    searched_from = Path(launch_dir).expanduser().resolve()

    if disabled:
        return McpResolution(None, None, None, searched_from)

    if explicit is not None:
        path = explicit_mcp_config_path(explicit, launch_dir=searched_from)
        if not path.is_file():
            raise McpConfigError(f"--mcp-config {str(explicit)!r} is not a readable file ({path})")
        data = _read_config(path)
        if SNAPSHOT_SOURCE_KEY not in data:
            return McpResolution(data["mcpServers"], None, path, searched_from, explicit=True)
        # A snapshot written before the search directory was recorded falls back to this one.
        return McpResolution(
            data["mcpServers"],
            None,
            _snapshot_path(data, SNAPSHOT_SOURCE_KEY, path),
            _snapshot_path(data, SNAPSHOT_SEARCHED_FROM_KEY, path)
            if SNAPSHOT_SEARCHED_FROM_KEY in data
            else searched_from,
        )

    # A disabled role must not read ambient configuration at all. Besides
    # avoiding needless secret-bearing I/O, this makes the profile the source
    # of authority rather than a filter applied after a broad grant was read.
    if profile_policy is not None and profile_policy.enabled is False:
        return apply_profile_mcp_policy(
            McpResolution(None, None, None, searched_from),
            profile_policy,
        )

    found = discover_mcp_config(searched_from)
    if found is None:
        return apply_profile_mcp_policy(
            McpResolution(None, "no_mcp_config_found", None, searched_from),
            profile_policy,
        )
    try:
        servers = _read_servers(found)
    except McpConfigError as exc:
        return apply_profile_mcp_policy(
            McpResolution(None, f"mcp_config_unusable:{exc}", found, searched_from),
            profile_policy,
        )
    if not servers:
        return apply_profile_mcp_policy(
            McpResolution(None, "mcp_config_declares_no_servers", found, searched_from),
            profile_policy,
        )
    return apply_profile_mcp_policy(
        McpResolution(servers, None, found, searched_from),
        profile_policy,
    )
