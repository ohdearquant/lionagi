# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lionagi.ln.concurrency import Lock

# Suppress MCP server logging by default
logging.getLogger("mcp").setLevel(logging.WARNING)
logging.getLogger("fastmcp").setLevel(logging.WARNING)
logging.getLogger("mcp.server").setLevel(logging.WARNING)
logging.getLogger("mcp.server.lowlevel").setLevel(logging.WARNING)
logging.getLogger("mcp.server.lowlevel.server").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Environment variable keys that should never be passed to MCP servers
_SENSITIVE_ENV_PATTERNS = frozenset(
    {
        "API_KEY",
        "API_SECRET",
        "API_TOKEN",
        "ACCESS_TOKEN",
        "AUTH_TOKEN",
        "AWS_SECRET",
        "AWS_SESSION_TOKEN",
        "CREDENTIAL",
        "DATABASE_URL",
        "DB_PASSWORD",
        "PASSWORD",
        "PRIVATE_KEY",
        "REFRESH_TOKEN",
        "SECRET_KEY",
        "SERVICE_TOKEN",
    }
)


__all__ = (
    "MCPSecurityConfig",
    "MCPConnectionPool",
    "create_mcp_tool",
)


@dataclass(frozen=True)
class MCPSecurityConfig:
    """Fail-closed security config for MCP connection pool."""

    allow_commands: bool = False
    command_allowlist: frozenset[str] | None = None
    allow_urls: bool = False
    url_allowlist: frozenset[str] | None = None
    env_denylist_patterns: frozenset[str] = field(default_factory=lambda: _SENSITIVE_ENV_PATTERNS)
    filter_sensitive_env: bool = True
    max_connections_per_server: int = 5


def _filter_env(env: dict[str, str], config: MCPSecurityConfig) -> dict[str, str]:
    """Remove env vars matching deny-listed substrings (case-insensitive)."""
    if not config.filter_sensitive_env:
        return env

    filtered = {}
    deny = config.env_denylist_patterns
    for key, value in env.items():
        key_upper = key.upper()
        if any(pattern in key_upper for pattern in deny):
            logger.debug(f"Filtered sensitive env var: {key}")
            continue
        filtered[key] = value
    return filtered


def _validate_command(command: str, config: MCPSecurityConfig) -> None:
    """Fail-closed: deny unless allow_commands=True and passes allowlist."""
    if not config.allow_commands:
        raise PermissionError(
            f"MCP command transport is disabled (allow_commands=False). "
            f"Set MCPSecurityConfig(allow_commands=True) to permit command-based MCP servers. "
            f"Blocked command: '{command}'"
        )

    if config.command_allowlist is None:
        # allow_commands=True and no allowlist: any bare or path command is permitted.
        return

    if "/" in command or "\\" in command:
        bare = os.path.basename(command)
        if bare in config.command_allowlist:
            raise ValueError(
                f"Command contains path separator: '{command}'. "
                f"Use bare command name '{bare}' instead."
            )
        raise ValueError(
            f"Command '{command}' not in allowlist. Allowed: {sorted(config.command_allowlist)}"
        )

    if command not in config.command_allowlist:
        raise ValueError(
            f"Command '{command}' not in allowlist. Allowed: {sorted(config.command_allowlist)}"
        )


def _validate_url(url: str, config: MCPSecurityConfig) -> None:
    """Fail-closed: deny unless allow_urls=True and scheme is https/wss."""
    if not config.allow_urls:
        raise PermissionError(
            f"MCP URL transport is disabled (allow_urls=False). "
            f"Set MCPSecurityConfig(allow_urls=True) to permit URL-based MCP servers. "
            f"Blocked URL: '{url}'"
        )

    parsed = urlparse(url)
    if parsed.scheme not in ("https", "wss"):
        raise ValueError(
            f"MCP URL transport requires https or wss scheme. Got '{parsed.scheme}' in URL: '{url}'"
        )

    if config.url_allowlist is not None:
        host = parsed.hostname or ""
        if host not in config.url_allowlist:
            raise ValueError(
                f"MCP URL host '{host}' not in allowlist. Allowed: {sorted(config.url_allowlist)}"
            )


class MCPConnectionPool:
    """Connection pool for MCP clients with fail-closed security."""

    _clients: dict[str, Any] = {}
    _configs: dict[str, dict] = {}
    _lock: Lock | None = None
    _lock_guard: threading.Lock = threading.Lock()
    _security: MCPSecurityConfig | None = None
    # Per-server policy keyed by content signature so reconnects
    # re-apply the same authorization instead of falling back to fail-closed.
    _server_security: dict[str, MCPSecurityConfig] = {}

    @staticmethod
    def _policy_key(server_config: dict[str, Any]) -> str:
        """Content-based key for per-server policy registry."""
        if "server" in server_config:
            return f"server:{server_config['server']}"
        material = {k: v for k, v in server_config.items() if not k.startswith("_")}
        blob = json.dumps(material, sort_keys=True, default=str)
        digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        return f"inline:{digest}"

    @classmethod
    def remember_security(
        cls, server_config: dict[str, Any], security: MCPSecurityConfig | None
    ) -> None:
        """Record the policy a server was authorized under. No-op if None."""
        if security is not None:
            cls._server_security[cls._policy_key(server_config)] = security

    @classmethod
    def _get_lock(cls) -> Lock:
        # Lazy creation avoids binding to an event loop at import time (3.10-3.11).
        if cls._lock is None:
            with cls._lock_guard:
                if cls._lock is None:
                    cls._lock = Lock()
        return cls._lock

    @classmethod
    def set_security_config(cls, config: MCPSecurityConfig) -> None:
        """Set security config for new connections. Existing ones unaffected."""
        cls._security = config

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.cleanup()

    @classmethod
    def load_config(cls, path: str = ".mcp.json") -> None:
        """Load MCP server configurations from a .mcp.json file."""
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"MCP config file not found: {path}")

        try:
            with open(config_path) as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise json.JSONDecodeError(
                f"Invalid JSON in MCP config file: {e.msg}", e.doc, e.pos
            ) from e

        if not isinstance(data, dict):
            raise ValueError("MCP config must be a JSON object")

        servers = data.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError("mcpServers must be a dictionary")

        cls._configs.update(servers)

    @classmethod
    async def get_client(
        cls,
        server_config: dict[str, Any],
        security: MCPSecurityConfig | None = None,
    ) -> Any:
        """Get or create a pooled MCP client."""
        # Explicit policy authorizes this server for future reconnects;
        # absent one, recover the policy the server was loaded under.
        if security is not None:
            cls.remember_security(server_config, security)
        else:
            security = cls._server_security.get(cls._policy_key(server_config))

        if "server" in server_config:
            server_name = server_config["server"]
            if server_name not in cls._configs:
                cls.load_config()
            if server_name not in cls._configs:
                raise ValueError(f"Unknown MCP server: {server_name}")

            config = cls._configs[server_name]
            cache_key = f"server:{server_name}"
        else:
            config = server_config
            cache_key = f"inline:{config.get('command')}:{id(config)}"

        async with cls._get_lock():
            if cache_key in cls._clients:
                client = cls._clients[cache_key]
                if hasattr(client, "is_connected") and client.is_connected():
                    return client
                else:
                    del cls._clients[cache_key]

            client = await cls._create_client(config, security=security)
            cls._clients[cache_key] = client
            return client

    @classmethod
    async def _create_client(
        cls,
        config: dict[str, Any],
        security: MCPSecurityConfig | None = None,
    ) -> Any:
        """Create a new MCP client from config (fail-closed)."""
        if not isinstance(config, dict):
            raise ValueError("Config must be a dictionary")

        if not any(k in config for k in ["url", "command"]):
            raise ValueError("Config must have either 'url' or 'command' key")

        # Precedence: explicit > process-global > fail-closed default.
        if security is not None:
            effective_security = security
        elif cls._security is not None:
            effective_security = cls._security
        else:
            effective_security = MCPSecurityConfig()

        # Validate BEFORE any import or transport construction.
        if "url" in config:
            _validate_url(config["url"], effective_security)
        elif "command" in config:
            _validate_command(config["command"], effective_security)

        try:
            from fastmcp import Client as FastMCPClient
        except ImportError:
            raise ImportError("FastMCP not installed. Run: pip install fastmcp") from None

        if "url" in config:
            client = FastMCPClient(config["url"])
        elif "command" in config:
            command = config["command"]
            args = config.get("args", [])
            if not isinstance(args, list):
                raise ValueError("Config 'args' must be a list")

            env = os.environ.copy()
            env.update(config.get("env", {}))

            env = _filter_env(env, effective_security)

            if not (
                config.get("debug", False) or os.environ.get("MCP_DEBUG", "").lower() == "true"
            ):
                env.setdefault("LOG_LEVEL", "ERROR")
                env.setdefault("PYTHONWARNINGS", "ignore")
                env.setdefault("FASTMCP_QUIET", "true")
                env.setdefault("MCP_QUIET", "true")

            from fastmcp.client.transports import StdioTransport

            transport = StdioTransport(
                command=command,
                args=args,
                env=env,
            )
            client = FastMCPClient(transport)
        else:
            raise ValueError("Config must have 'url' or 'command'")

        await client.__aenter__()
        return client

    @classmethod
    async def cleanup(cls):
        async with cls._get_lock():
            for cache_key, client in cls._clients.items():
                try:
                    await client.__aexit__(None, None, None)
                except Exception as e:
                    logging.debug(f"Error cleaning up MCP client {cache_key}: {e}")
            cls._clients.clear()


def create_mcp_tool(mcp_config: dict[str, Any], tool_name: str) -> Any:
    """Create an async callable wrapping MCP tool execution."""

    async def mcp_callable(**kwargs):
        actual_tool_name = mcp_config.get("_original_tool_name", tool_name)

        config_for_client = {k: v for k, v in mcp_config.items() if not k.startswith("_")}

        client = await MCPConnectionPool.get_client(config_for_client)

        result = await client.call_tool(actual_tool_name, kwargs)

        if hasattr(result, "content"):
            content = result.content
            if isinstance(content, list) and len(content) == 1:
                item = content[0]
                if hasattr(item, "text"):
                    return item.text
                elif isinstance(item, dict) and item.get("type") == "text":
                    return item.get("text", "")
            return content
        elif isinstance(result, list) and len(result) == 1:
            item = result[0]
            if isinstance(item, dict) and item.get("type") == "text":
                return item.get("text", "")

        return result

    mcp_callable.__name__ = tool_name
    mcp_callable.__doc__ = f"MCP tool: {tool_name}"

    return mcp_callable
