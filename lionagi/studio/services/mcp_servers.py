# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""MCP servers as a managed Studio resource.

See ``docs/internals/studio.md`` ("MCP server registry") for the
registry/derived-file split, the write-lock discipline, and secret handling.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
import time
from copy import deepcopy
from functools import partial
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

import anyio
from fastapi import Body, HTTPException

from lionagi._paths import LIONAGI_HOME
from lionagi.libs.path_safety import validate_bare_name

from ..registry import studio_route
from ._io import read_json_file as _read_json

_log = logging.getLogger(__name__)

_REGISTRY_PATH = LIONAGI_HOME / "mcp_servers.json"
_SYNCED_MCP_JSON_PATH = LIONAGI_HOME / ".mcp.json"

_CONFIG_KEYS = ("command", "args", "env", "url", "timeout", "alwaysAllow")
_CONNECT_TIMEOUT_SECONDS = 8.0
_SECRET_MASK = "***"  # noqa: S105 — redaction marker, not a credential


class McpServerError(ValueError):
    """A server config's shape is unusable (bad transport, malformed fields)."""


class DuplicateServerError(McpServerError):
    """Raised by register_server() when the name is already registered."""


# ---------------------------------------------------------------------------
# Registry storage
# ---------------------------------------------------------------------------


# Spans load-through-save (see studio.md); never held across a network probe.
_REGISTRY_WRITE_LOCK = threading.RLock()


def _load_registry() -> dict[str, dict[str, Any]]:
    data = _read_json(_REGISTRY_PATH) if _REGISTRY_PATH.exists() else None
    if not data or not isinstance(data.get("servers"), dict):
        return {}
    return data["servers"]


def _write_private(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically, owner-only (0600 regardless
    of umask; also repairs a pre-existing 0644 file). See studio.md."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    os.chmod(path, 0o600)


def _save_registry(servers: dict[str, dict[str, Any]]) -> None:
    _write_private(
        _REGISTRY_PATH, json.dumps({"servers": servers}, indent=2, sort_keys=True) + "\n"
    )
    _sync_mcp_json(servers)


def _sync_mcp_json(servers: dict[str, dict[str, Any]]) -> None:
    """Regenerate the standard ``.mcp.json`` CLI runs can point at, containing
    only enabled servers."""
    enabled = {
        name: entry["config"] for name, entry in servers.items() if entry.get("enabled", True)
    }
    _write_private(
        _SYNCED_MCP_JSON_PATH, json.dumps({"mcpServers": enabled}, indent=2, sort_keys=True) + "\n"
    )


# ---------------------------------------------------------------------------
# Shape validation
# ---------------------------------------------------------------------------


# `_merge_config` clears these when a save declares the other transport.
# `timeout`/`alwaysAllow` apply to both, so neither is in either set.
_STDIO_ONLY_FIELDS = ("command", "args", "env")
_HTTP_ONLY_FIELDS = ("url",)


def _validate_shape(name: str, config: dict[str, Any]) -> list[str]:
    """Check a config's shape only -- never attempts a connection. Returns a
    list of human-readable error strings, empty when the shape is usable."""
    errors: list[str] = []

    try:
        validate_bare_name(name, "server name")
    except ValueError as exc:
        errors.append(str(exc))

    if not isinstance(config, dict):
        return [*errors, "config must be an object"]

    # 'args'/'env' shape-check unconditionally, regardless of transport.
    has_command = bool(config.get("command"))
    has_url = bool(config.get("url"))
    if has_command and has_url:
        errors.append("config must specify exactly one of 'command' or 'url', not both")
    elif not has_command and not has_url:
        errors.append(
            "config must specify either 'command' (stdio transport) or 'url' (http transport)"
        )

    if has_command and not isinstance(config.get("command"), str):
        errors.append("'command' must be a string")

    # `args`/`env` are checked whenever either key is present in the config,
    # not only for a stdio transport: a malformed value is malformed whether
    # or not the selected transport happens to read it, and this validator
    # is the only place that sees the config before it reaches disk.
    args = config.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        errors.append("'args' must be a list of strings")
    env = config.get("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        errors.append("'env' must be an object mapping string names to string values")

    if has_url:
        url = config.get("url")
        if not isinstance(url, str):
            errors.append("'url' must be a string")
        else:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                errors.append("'url' must be an http(s) URL")

    timeout = config.get("timeout")
    if timeout is not None and (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)):
        errors.append("'timeout' must be a number")

    return errors


def _config_from_body(body: dict[str, Any]) -> dict[str, Any]:
    return {k: body[k] for k in _CONFIG_KEYS if k in body}


# ---------------------------------------------------------------------------
# Secret handling — env values are never echoed back to a client
# ---------------------------------------------------------------------------


def _mask_config(config: dict[str, Any]) -> dict[str, Any]:
    """Strip env values from a config before it leaves the process; a client
    sees only which env keys are configured (``env_keys``)."""
    masked = {k: v for k, v in config.items() if k != "env"}
    masked["env_keys"] = sorted((config.get("env") or {}).keys())
    return masked


def _transport_of(config: dict[str, Any]) -> str:
    return "stdio" if config.get("command") else "http"


def _public_entry(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    config = entry.get("config", {})
    return {
        "name": name,
        "transport": _transport_of(config),
        **_mask_config(config),
        "enabled": bool(entry.get("enabled", True)),
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
        "last_check": entry.get("last_check"),
    }


def _scrub_secrets(text: str, config: dict[str, Any]) -> str:
    """Remove any configured env *value* from an error string before it
    reaches a client -- a failed connection attempt can echo its own
    environment back (subprocess stderr, a rejected auth token)."""
    for value in (config.get("env") or {}).values():
        if isinstance(value, str) and value:
            text = text.replace(value, _SECRET_MASK)
    return text


# ---------------------------------------------------------------------------
# Connection checking — real attempt, not a shape check in disguise
# ---------------------------------------------------------------------------


async def _attempt_connection(config: dict[str, Any]) -> dict[str, Any]:
    """Spawn/connect directly through fastmcp's Client, bypassing the shared
    MCPConnectionPool so a validation probe never lingers as a pooled,
    reusable connection. Proves a live handshake now, not lasting health."""
    try:
        from fastmcp import Client as FastMCPClient
    except ImportError:
        return {"ok": False, "error": "fastmcp is not installed (pip install lionagi[mcp])"}

    try:
        if config.get("url"):
            client = FastMCPClient(config["url"])
        else:
            from fastmcp.client.transports import StdioTransport

            env = os.environ.copy()
            env.update(config.get("env") or {})
            transport = StdioTransport(
                command=config["command"],
                args=config.get("args") or [],
                env=env,
            )
            client = FastMCPClient(transport)

        with anyio.fail_after(_CONNECT_TIMEOUT_SECONDS):
            async with client:
                await client.ping()
        return {"ok": True, "error": None}
    except TimeoutError:
        return {
            "ok": False,
            "error": f"connection attempt timed out after {_CONNECT_TIMEOUT_SECONDS:g}s",
        }
    except Exception as exc:  # noqa: BLE001 — reporting the failure IS the feature
        return {"ok": False, "error": _scrub_secrets(str(exc), config)}


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def list_servers() -> list[dict[str, Any]]:
    servers = _load_registry()
    return [_public_entry(name, entry) for name, entry in sorted(servers.items())]


def get_server(name: str) -> dict[str, Any] | None:
    entry = _load_registry().get(name)
    return None if entry is None else _public_entry(name, entry)


def register_server(name: str, config: dict[str, Any], *, enabled: bool = True) -> dict[str, Any]:
    # Registering merges onto an empty base, same rule as an existing server.
    merged = _merge_config({}, config)
    errors = _validate_shape(name, merged)
    if errors:
        raise McpServerError("; ".join(errors))

    with _REGISTRY_WRITE_LOCK:
        servers = _load_registry()
        if name in servers:
            raise DuplicateServerError(f"MCP server {name!r} already exists")

        now = time.time()
        servers[name] = {
            "config": merged,
            "enabled": enabled,
            "created_at": now,
            "updated_at": now,
            "last_check": None,
        }
        _save_registry(servers)
        return _public_entry(name, servers[name])


def _merge_config(existing: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a partial config onto the stored one instead of replacing it --
    a client never receives env values back, so an unrelated save must not
    wipe an env block it never saw. See studio.md for the full merge rules
    (``None``-removes, the ``args`` exception, and the transport-switch
    field-drop)."""
    merged = dict(existing)
    for key in ("command", "args", "url", "timeout", "alwaysAllow"):
        if key not in patch:
            continue
        value = patch[key]
        if value is None and key != "args":
            merged.pop(key, None)
        else:
            merged[key] = value

    if "env" in patch:
        incoming_env = patch["env"]
        if incoming_env is None:
            incoming_env = {}
        if isinstance(incoming_env, dict):
            merged_env = dict(existing.get("env") or {})
            for env_key, env_value in incoming_env.items():
                if env_value is None:
                    merged_env.pop(env_key, None)
                else:
                    merged_env[env_key] = env_value
            merged["env"] = merged_env
        else:
            # Not a mapping at all (e.g. a string, a list, or a number) --
            # pass it through untouched so `_validate_shape`'s own env type
            # check reports it as an ordinary shape error, instead of this
            # merge crashing on `.items()` before validation ever runs.
            merged["env"] = incoming_env

    if patch.get("url"):
        for key in _STDIO_ONLY_FIELDS:
            if key not in patch:
                merged.pop(key, None)
    if patch.get("command"):
        for key in _HTTP_ONLY_FIELDS:
            if key not in patch:
                merged.pop(key, None)

    return merged


def update_server(name: str, config: dict[str, Any]) -> dict[str, Any] | None:
    with _REGISTRY_WRITE_LOCK:
        servers = _load_registry()
        if name not in servers:
            return None

        entry = servers[name]
        existing = entry.get("config") or {}
        merged = _merge_config(existing, config)

        errors = _validate_shape(name, merged)
        if errors:
            raise McpServerError("; ".join(errors))

        if merged != existing:
            # The stored status was obtained for the configuration being
            # replaced. Keeping it would report a connection result for a
            # command or URL that was never probed, which is the same lie the
            # connection check refuses to write, arriving by a slower route.
            entry["last_check"] = None
        entry["config"] = merged
        entry["updated_at"] = time.time()
        _save_registry(servers)
        return _public_entry(name, entry)


def set_enabled(name: str, enabled: bool) -> dict[str, Any] | None:
    with _REGISTRY_WRITE_LOCK:
        servers = _load_registry()
        entry = servers.get(name)
        if entry is None:
            return None
        entry["enabled"] = enabled
        entry["updated_at"] = time.time()
        _save_registry(servers)
        return _public_entry(name, entry)


def remove_server(name: str) -> bool:
    with _REGISTRY_WRITE_LOCK:
        servers = _load_registry()
        if name not in servers:
            return False
        del servers[name]
        _save_registry(servers)
        return True


async def check_server_connection(name: str) -> dict[str, Any] | None:
    """Attempt a real connection to an already-registered server and persist
    the outcome. Reloads and re-matches against the probed config before
    saving, so a concurrent edit/replace under the same name during the
    probe can't stamp a stale result; only the final compare-save holds the
    registry lock. See studio.md for the full race analysis."""
    servers = _load_registry()
    entry = servers.get(name)
    if entry is None:
        return None

    probed_config = deepcopy(entry["config"])
    outcome = await _attempt_connection(probed_config)
    last_check = {
        "ok": outcome["ok"],
        "error": outcome["error"],
        "checked_at": time.time(),
    }

    with _REGISTRY_WRITE_LOCK:
        servers = _load_registry()
        entry = servers.get(name)
        if entry is None:
            return None
        if entry.get("config") != probed_config:
            return _public_entry(name, entry)
        entry["last_check"] = last_check
        _save_registry(servers)
        return _public_entry(name, entry)


async def validate_config(
    name: str, config: dict[str, Any], *, check_connection: bool = False
) -> dict[str, Any]:
    """Validate a config before it is saved. ``config`` is a patch (as
    ``update_server`` receives it), merged the same way so validation checks
    what would actually be persisted. Shape is always checked; connection is
    only attempted when the caller opts in, and the response says explicitly
    whether it was."""
    existing = (_load_registry().get(name) or {}).get("config") or {}
    merged = _merge_config(existing, config)
    errors = _validate_shape(name, merged)
    result: dict[str, Any] = {
        "ok": not errors,
        "errors": errors or None,
        "connection_checked": False,
        "connection_ok": None,
        "connection_error": None,
    }
    if not errors and check_connection:
        outcome = await _attempt_connection(merged)
        result["connection_checked"] = True
        result["connection_ok"] = outcome["ok"]
        result["connection_error"] = outcome["error"]
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@studio_route("/mcp/servers/", method="GET", area="mcp", name="list_mcp_servers")
async def list_mcp_servers_route() -> dict[str, Any]:
    servers = await anyio.to_thread.run_sync(list_servers)
    return {"servers": servers}


@studio_route("/mcp/servers/{name}", method="GET", area="mcp", name="get_mcp_server")
async def get_mcp_server_route(name: str) -> dict[str, Any]:
    server = await anyio.to_thread.run_sync(partial(get_server, name))
    if server is None:
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    return server


@studio_route(
    "/mcp/servers/", method="POST", area="mcp", status_code=201, name="register_mcp_server"
)
async def register_mcp_server_route(body: Annotated[dict[str, Any], Body(...)]) -> dict[str, Any]:
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="name is required")
    config = _config_from_body(body)
    enabled = bool(body.get("enabled", True))
    try:
        return await anyio.to_thread.run_sync(
            partial(register_server, name, config, enabled=enabled)
        )
    except DuplicateServerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except McpServerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@studio_route("/mcp/servers/{name}", method="PUT", area="mcp", name="update_mcp_server")
async def update_mcp_server_route(
    name: str, body: Annotated[dict[str, Any], Body(...)]
) -> dict[str, Any]:
    config = _config_from_body(body)
    try:
        updated = await anyio.to_thread.run_sync(partial(update_server, name, config))
    except McpServerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    return updated


@studio_route("/mcp/servers/{name}/enable", method="POST", area="mcp", name="enable_mcp_server")
async def enable_mcp_server_route(name: str) -> dict[str, Any]:
    updated = await anyio.to_thread.run_sync(partial(set_enabled, name, True))
    if updated is None:
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    return updated


@studio_route("/mcp/servers/{name}/disable", method="POST", area="mcp", name="disable_mcp_server")
async def disable_mcp_server_route(name: str) -> dict[str, Any]:
    updated = await anyio.to_thread.run_sync(partial(set_enabled, name, False))
    if updated is None:
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    return updated


@studio_route("/mcp/servers/{name}", method="DELETE", area="mcp", name="delete_mcp_server")
async def delete_mcp_server_route(name: str) -> dict[str, Any]:
    removed = await anyio.to_thread.run_sync(partial(remove_server, name))
    if not removed:
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    return {"ok": True}


@studio_route("/mcp/servers/{name}/check", method="POST", area="mcp", name="check_mcp_server")
async def check_mcp_server_route(name: str) -> dict[str, Any]:
    result = await check_server_connection(name)
    if result is None:
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    return result


@studio_route("/mcp/servers/{name}/validate", method="POST", area="mcp", name="validate_mcp_server")
async def validate_mcp_server_route(
    name: str, body: Annotated[dict[str, Any], Body(...)]
) -> dict[str, Any]:
    config = _config_from_body(body)
    check_connection = bool(body.get("check_connection", False))
    server_name = str(body.get("name") or name or "").strip()
    return await validate_config(server_name, config, check_connection=check_connection)
