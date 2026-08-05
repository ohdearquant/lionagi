# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""MCP servers as a managed Studio resource.

Studio keeps one authoritative registry (``LIONAGI_HOME/mcp_servers.json``,
full configs including secret values, enable/disable state, last connection
status) and derives a plain ``.mcp.json`` from it on every write
(``LIONAGI_HOME/.mcp.json``), containing only the *enabled* servers in
exactly the ``{"mcpServers": {...}}`` shape ``lionagi/cli/_mcp_resolve.py``
already parses. A run submitted with ``--mcp-config
~/.lionagi/.mcp.json`` sees exactly the servers Studio manages, and never a
disabled one -- disabled servers are simply absent from the derived file
rather than marked with a flag a reader might not honour.

The registry is a separate store from the derived file (not a passthrough
onto some project's ``.mcp.json``) because Studio is not scoped to one
project directory -- it manages many named projects from a single process,
and per-project cwd discovery would pick whichever project happened to be
the server's launch directory, which is not a meaningful answer here.
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


# Every mutation is a read-modify-write over the whole registry file, so two
# of them interleaving loses one wholesale: the second writes back a dict it
# read before the first landed. The routes run these in worker threads, so the
# boundary that has to hold is a thread lock, and it has to span the load and
# the save rather than either alone. Reads outside it are fine -- os.replace
# makes each save atomic, so a reader sees one whole registry or the other.
#
# It is reentrant because a mutation may call another one, and it is never
# held across a network probe: a connection attempt can run for seconds, and
# blocking every save behind it would trade a lost write for a frozen UI.
_REGISTRY_WRITE_LOCK = threading.RLock()


def _load_registry() -> dict[str, dict[str, Any]]:
    data = _read_json(_REGISTRY_PATH) if _REGISTRY_PATH.exists() else None
    if not data or not isinstance(data.get("servers"), dict):
        return {}
    return data["servers"]


def _write_private(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically, owner-only.

    Both the registry and its derived ``.mcp.json`` hold secret env values,
    so a reader other than the owner must never see them -- not even
    briefly. The temp file is created via ``mkstemp``, which is guaranteed
    ``0600`` regardless of umask, and ``os.replace`` is atomic so a crash
    mid-write never leaves a half-written registry. The final ``chmod`` is
    what repairs a file created before this fix: a plain ``write_text``
    writes into the existing inode and leaves its old ``0644`` mode alone,
    so an explicit chmod on every save is what actually closes that off.
    """
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
    """Regenerate the standard ``.mcp.json`` CLI runs can point at.

    Only enabled servers are included -- a disabled server is exactly as
    absent from this file as one that was never registered, so a reader that
    knows nothing about Studio's "enabled" concept still gets the right set.
    """
    enabled = {
        name: entry["config"] for name, entry in servers.items() if entry.get("enabled", True)
    }
    _write_private(
        _SYNCED_MCP_JSON_PATH, json.dumps({"mcpServers": enabled}, indent=2, sort_keys=True) + "\n"
    )


# ---------------------------------------------------------------------------
# Shape validation
# ---------------------------------------------------------------------------


# Which fields belong to which transport. The shape check below reads a
# config's stdio fields only when it has a command and its http fields only
# when it has a url, so these are the sets a transport switch has to clear --
# keep them in step with the two branches of _validate_shape. `timeout` and
# `alwaysAllow` are deliberately in neither: they apply to both transports.
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

    has_command = bool(config.get("command"))
    has_url = bool(config.get("url"))
    if has_command and has_url:
        errors.append("config must specify exactly one of 'command' or 'url', not both")
    elif not has_command and not has_url:
        errors.append(
            "config must specify either 'command' (stdio transport) or 'url' (http transport)"
        )

    if has_command:
        if not isinstance(config.get("command"), str):
            errors.append("'command' must be a string")
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
    """Strip env values from a config before it leaves the process.

    A client sees which env keys are configured (``env_keys``), never their
    values, in list/get responses or validation results. Values are only
    ever read back off disk inside this module (e.g. to attempt a
    connection), never serialized into an HTTP response.
    """
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
    """Remove any configured env *value* from an error string before it is
    returned to a client. A failed connection attempt can echo back its own
    environment (subprocess stderr, an auth error naming the token it
    rejected) -- this is the point secrets could otherwise leak through a
    path that isn't the config itself."""
    for value in (config.get("env") or {}).values():
        if isinstance(value, str) and value:
            text = text.replace(value, _SECRET_MASK)
    return text


# ---------------------------------------------------------------------------
# Connection checking — real attempt, not a shape check in disguise
# ---------------------------------------------------------------------------


async def _attempt_connection(config: dict[str, Any]) -> dict[str, Any]:
    """Actually try to reach the server: spawns the stdio process or opens
    the http connection directly through fastmcp's Client, bypassing the
    shared MCPConnectionPool (lionagi.service.connections.mcp_wrapper) so a
    validation probe never lingers as a pooled, reusable connection shared
    with unrelated branches. Honest about what it proves: a live handshake
    right now, not that every tool the server exposes will keep working.
    """
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
    errors = _validate_shape(name, config)
    if errors:
        raise McpServerError("; ".join(errors))

    with _REGISTRY_WRITE_LOCK:
        servers = _load_registry()
        if name in servers:
            raise DuplicateServerError(f"MCP server {name!r} already exists")

        now = time.time()
        servers[name] = {
            "config": config,
            "enabled": enabled,
            "created_at": now,
            "updated_at": now,
            "last_check": None,
        }
        _save_registry(servers)
        return _public_entry(name, servers[name])


def _merge_config(existing: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Merge a partial config onto the stored one instead of replacing it.

    A client never receives env *values* back (see ``_mask_config``), so a
    save that only changed e.g. ``args`` and echoed nothing else back must
    not wipe the env block it never saw. ``env`` merges key-by-key; a `None`
    value for a key removes it (the client's explicit way to drop a secret
    without knowing its value). Any other field replaces wholesale when
    present, and a `None`/empty value removes it.

    A transport switch drops every field belonging to the transport being
    left, not just the one that names it. The shape check only requires
    exactly one of ``command``/``url``, so an http entry that kept the old
    ``args`` and ``env`` would still validate -- and the derived ``.mcp.json``
    would then hand every reader a set of stdio arguments and secrets that
    the chosen transport never uses.
    """
    merged = dict(existing)
    for key in ("command", "args", "url", "timeout", "alwaysAllow"):
        if key not in patch:
            continue
        value = patch[key]
        if value in (None, ""):
            merged.pop(key, None)
        else:
            merged[key] = value

    if "env" in patch:
        incoming_env = patch["env"] or {}
        merged_env = dict(existing.get("env") or {})
        for env_key, env_value in incoming_env.items():
            if env_value is None:
                merged_env.pop(env_key, None)
            else:
                merged_env[env_key] = env_value
        merged["env"] = merged_env

    if patch.get("url"):
        for key in _STDIO_ONLY_FIELDS:
            merged.pop(key, None)
    if patch.get("command"):
        for key in _HTTP_ONLY_FIELDS:
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
    the outcome, so list/get can honestly report "whether the last
    connection attempt succeeded" instead of a shape guess.

    The registry is reloaded after the probe rather than reusing the
    pre-await snapshot: ``_attempt_connection`` can run for seconds, and
    writing back a stale full ``servers`` dict would silently revert any
    save that landed while the probe was in flight.

    The reloaded entry is matched against the configuration that was actually
    probed before the outcome is kept. A name is mutable and reusable: an
    operator can edit a server, or delete it and register a different one
    under the same name, while the probe is still running. Matching on name
    alone would stamp "connected" onto a server this attempt never reached.
    A result that no longer describes the current configuration is discarded
    rather than persisted, so the stored status is always one that was
    obtained for the configuration it sits on.

    Only the final reload-compare-save runs under the registry write lock. The
    probe itself must not hold it: it can take seconds, and a save blocked
    behind a network attempt is a worse failure than the one the lock prevents.
    """
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
    """Validate a config before it is saved. Shape is always checked;
    connection is only attempted when the caller opts in, and the response
    says explicitly whether it was -- a shape check that silently claimed to
    prove a server works would be worse than no check at all."""
    errors = _validate_shape(name, config)
    result: dict[str, Any] = {
        "ok": not errors,
        "errors": errors or None,
        "connection_checked": False,
        "connection_ok": None,
        "connection_error": None,
    }
    if not errors and check_connection:
        outcome = await _attempt_connection(config)
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
