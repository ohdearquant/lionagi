# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeAlias
from urllib.parse import urlparse

from lionagi.ln._hash import compute_hash
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
    "is_synthetic_mcp_wrapper_schema",
    "validate_mcp_tool_admission",
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


# --- Generic-executor admission rule -----------------------------------
#
# Registration-time admission control for MCP tool descriptors. This is
# independent of MCPSecurityConfig (transport authorization) and of
# PermissionPolicy (invocation-time, keyed by tool name): it rejects a
# caller-shaped generic command/process/script executor before it ever
# reaches the tool registry, regardless of transport settings.

AdmissionReason: TypeAlias = Literal[
    "unbounded-command-input",
    "unbounded-process-input",
    "unbounded-script-payload",
    "executor-description-with-broad-input",
    "executor-identity-with-insufficient-schema",
]

_STRONG_EXECUTOR_NAMES = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "shell",
        "cmd",
        "powershell",
        "pwsh",
        "terminal",
        "exec",
        "exec_command",
        "execute_command",
        "run_command",
        "run_shell",
        "shell_exec",
        "command_exec",
        "spawn_process",
        "run_process",
    }
)

_EXECUTOR_DESCRIPTION_PHRASES = (
    "arbitrary command",
    "arbitrary commands",
    "arbitrary shell",
    "execute command",
    "execute commands",
    "executes command",
    "executes commands",
    "execute a command",
    "execute os command",
    "execute os commands",
    "executes os commands",
    "execute an os command",
    "execute system command",
    "execute system commands",
    "executes system commands",
    "execute a system command",
    "execute shell command",
    "execute shell commands",
    "executes shell commands",
    "execute a shell command",
    "execute terminal command",
    "execute terminal commands",
    "executes terminal commands",
    "execute a terminal command",
    "run command",
    "run commands",
    "runs command",
    "runs commands",
    "run a command",
    "run os command",
    "run os commands",
    "runs os commands",
    "run an os command",
    "run system command",
    "run system commands",
    "runs system commands",
    "run a system command",
    "run shell command",
    "run shell commands",
    "runs shell commands",
    "run a shell command",
    "run terminal command",
    "run terminal commands",
    "runs terminal commands",
    "run a terminal command",
    "run shell",
    "run a shell",
    "execute script",
    "execute scripts",
    "executes scripts",
    "execute a script",
    "spawn process",
    "spawn processes",
    "spawns processes",
    "spawn a process",
    "shell command executor",
    "shell command runner",
    "command line executor",
    "command line runner",
)

_COMMAND_KEYS = frozenset({"command", "cmd", "command_line", "shell_command"})
_PROGRAM_KEYS = frozenset({"program", "executable", "binary"})
_ARGUMENT_KEYS = frozenset({"args", "argv"})
_PAYLOAD_KEYS = frozenset({"script", "code", "input", "text"})
_SELECTOR_KEYS = frozenset({"shell", "interpreter"})
_AUXILIARY_KEYS = frozenset(
    {
        "cwd",
        "working_directory",
        "working_dir",
        "env",
        "environment",
        "stdin",
        "timeout",
        "timeout_seconds",
        "shell",
        "interpreter",
        "user",
    }
)
_CATEGORIZED_KEYS = _COMMAND_KEYS | _PROGRAM_KEYS | _ARGUMENT_KEYS | _PAYLOAD_KEYS | _SELECTOR_KEYS

_CAMEL_BOUNDARY_LOWER_UPPER = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_CAMEL_BOUNDARY_UPPER_RUN = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")
_NON_ALNUM_RUN = re.compile(r"[^a-zA-Z0-9]+")
_REPEATED_UNDERSCORE = re.compile(r"_+")
_NON_ALPHANUM_RUN = re.compile(r"[^a-z0-9]+")


def _normalize_mcp_identifier(name: object) -> str:
    """Case-fold a tool/property name to `_`-joined tokens, splitting camelCase first."""
    if not isinstance(name, str):
        return ""
    split = _CAMEL_BOUNDARY_UPPER_RUN.sub("_", _CAMEL_BOUNDARY_LOWER_UPPER.sub("_", name))
    folded = split.casefold()
    replaced = _NON_ALNUM_RUN.sub("_", folded)
    return _REPEATED_UNDERSCORE.sub("_", replaced).strip("_")


def _normalize_mcp_description(description: object) -> str:
    """Case-fold a description to single-space-joined tokens for phrase matching."""
    if not isinstance(description, str):
        return ""
    folded = description.casefold()
    return _NON_ALPHANUM_RUN.sub(" ", folded).strip()


def _has_strong_executor_name(tool_name: object) -> bool:
    return _normalize_mcp_identifier(tool_name) in _STRONG_EXECUTOR_NAMES


def _has_executor_description_signal(description: object) -> bool:
    normalized = _normalize_mcp_description(description)
    if not normalized:
        return False
    padded = f" {normalized} "
    return any(f" {phrase} " in padded for phrase in _EXECUTOR_DESCRIPTION_PHRASES)


_IDENTIFIER_LIKE_KEY_PATTERN = re.compile(
    r"^(?:[a-z0-9]+_)*(?:id|ids|path|paths|uri|url|uuid|slug)$"
)


def _is_identifier_like_key(norm_key: str) -> bool:
    """True for dynamic-but-benign resource identifiers (`service_id`,
    `resource_path`, `request_id`, ...). These are excluded from the
    strong-name "must be affirmatively bounded" fallback: a fixed-operation
    tool with a free-form identifier field is not executor-shaped, even
    though the identifier itself is an unbounded string."""
    return bool(_IDENTIFIER_LIKE_KEY_PATTERN.match(norm_key))


def _schema_is_insufficient(input_schema: object) -> bool:
    if input_schema is None or not isinstance(input_schema, Mapping):
        return True
    top_type = input_schema.get("type")
    # `type` may be a Draft 2020-12 type array (e.g. `["object", "null"]`);
    # such a schema is an object schema whenever "object" is one of its
    # allowed types, and its properties must still be inspected. Only a
    # top-level type that excludes "object" entirely makes the schema
    # insufficient.
    if top_type is not None and not _schema_type_includes(top_type, "object"):
        return True
    if "properties" in input_schema and not isinstance(input_schema["properties"], Mapping):
        return True
    if "properties" not in input_schema and any(
        k in input_schema for k in ("$ref", "oneOf", "anyOf", "allOf")
    ):
        return True
    properties = input_schema.get("properties")
    props = properties if isinstance(properties, Mapping) else {}
    if not props:
        return input_schema.get("additionalProperties") is not False
    return False


def _property_is_bounded(prop_schema: object) -> bool:
    if not isinstance(prop_schema, Mapping):
        return False
    if "enum" in prop_schema or "const" in prop_schema:
        return True
    if _schema_type_includes(prop_schema.get("type"), "array"):
        items = prop_schema.get("items")
        if isinstance(items, Mapping) and ("enum" in items or "const" in items):
            return True
    return False


def _schema_type_includes(type_value: object, target: str) -> bool:
    """True when a JSON Schema `type` (string or Draft 2020-12 type array) allows `target`."""
    if isinstance(type_value, list):
        return target in type_value
    return type_value == target


def _property_is_free_form(prop_schema: object, is_categorized_key: bool) -> bool:
    # JSON Schema boolean `true` matches any value, so it is at least as
    # permissive as an untyped free-form string; `false` matches nothing.
    if prop_schema is True:
        return True
    if prop_schema is False or not isinstance(prop_schema, Mapping):
        return False
    if _property_is_bounded(prop_schema):
        return False
    prop_type = prop_schema.get("type")
    if _schema_type_includes(prop_type, "string"):
        return True
    if _schema_type_includes(prop_type, "array"):
        items = prop_schema.get("items")
        return isinstance(items, Mapping) and _schema_type_includes(items.get("type"), "string")
    if prop_type is None and is_categorized_key:
        return True
    return False


_MAX_SCHEMA_WALK_DEPTH = 12


class _SchemaWalkResult:
    """Accumulates classifier evidence discovered while traversing a
    (possibly nested/composed) JSON Schema input descriptor."""

    __slots__ = (
        "free_form_command_keys",
        "free_form_program_keys",
        "free_form_argument_keys",
        "free_form_payload_keys",
        "non_auxiliary_free_form_keys",
        "non_identifier_free_form_keys",
        "selector_key_present",
        "unresolvable",
    )

    def __init__(self) -> None:
        self.free_form_command_keys: set[str] = set()
        self.free_form_program_keys: set[str] = set()
        self.free_form_argument_keys: set[str] = set()
        self.free_form_payload_keys: set[str] = set()
        self.non_auxiliary_free_form_keys: set[str] = set()
        self.non_identifier_free_form_keys: set[str] = set()
        self.selector_key_present = False
        # True when a channel could not be proven bounded: an external/
        # unresolvable `$ref`, a `$ref` cycle, a depth-cap trip, or a
        # malformed `properties`/composition shape. Fed into fail-closed
        # handling for descriptor-bearing tools whose name or description
        # signals an executor; otherwise it is just insufficient evidence.
        self.unresolvable = False


def _resolve_local_ref(ref: str, root_schema: Mapping) -> Mapping | None:
    """Resolve a same-document `$ref` (e.g. `#/$defs/Foo`) against
    `root_schema`. Returns None if the pointer cannot be resolved locally."""
    node: Any = root_schema
    for raw_part in ref[2:].split("/"):
        if raw_part == "":
            continue
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, Mapping) else None


def _pattern_matches_categorized_key(pattern: object) -> str | None:
    """If a `patternProperties` regex would match one of the recognized
    command/process/argument/payload/selector key names, return that key."""
    if not isinstance(pattern, str):
        return None
    try:
        compiled = re.compile(pattern)
    except re.error:
        return None
    for key in _CATEGORIZED_KEYS:
        if compiled.search(key):
            return key
    return None


def _record_free_form_key(norm_key: str, result: _SchemaWalkResult) -> None:
    if norm_key in _COMMAND_KEYS:
        result.free_form_command_keys.add(norm_key)
    elif norm_key in _PROGRAM_KEYS:
        result.free_form_program_keys.add(norm_key)
    elif norm_key in _ARGUMENT_KEYS:
        result.free_form_argument_keys.add(norm_key)
    elif norm_key in _PAYLOAD_KEYS:
        result.free_form_payload_keys.add(norm_key)
    if norm_key not in _AUXILIARY_KEYS:
        result.non_auxiliary_free_form_keys.add(norm_key)
        if not _is_identifier_like_key(norm_key):
            result.non_identifier_free_form_keys.add(norm_key)


def _consider_property(
    raw_key: object,
    prop_schema: object,
    root_schema: Mapping,
    depth: int,
    seen_refs: frozenset[str],
    is_strong_name: bool,
    is_executor_description: bool,
    result: _SchemaWalkResult,
) -> None:
    norm_key = _normalize_mcp_identifier(raw_key)
    if norm_key in _SELECTOR_KEYS:
        result.selector_key_present = True

    if isinstance(prop_schema, Mapping):
        has_nested_shape = any(
            k in prop_schema
            for k in (
                "properties",
                "$ref",
                "allOf",
                "anyOf",
                "oneOf",
                "additionalProperties",
                "patternProperties",
            )
        )
        prop_type = prop_schema.get("type")
        type_excludes_object = prop_type is not None and not _schema_type_includes(
            prop_type, "object"
        )
        if has_nested_shape and not type_excludes_object:
            # A container (e.g. a nested `options` object) is not itself a
            # command/process/script value; walk its own reachable
            # properties instead of classifying the container's key.
            _walk_schema(
                prop_schema,
                root_schema,
                depth + 1,
                seen_refs,
                is_strong_name,
                is_executor_description,
                result,
            )
            return

    if not _property_is_free_form(prop_schema, norm_key in _CATEGORIZED_KEYS):
        return

    _record_free_form_key(norm_key, result)


def _walk_schema(
    schema: object,
    root_schema: Mapping,
    depth: int,
    seen_refs: frozenset[str],
    is_strong_name: bool,
    is_executor_description: bool,
    result: _SchemaWalkResult,
) -> None:
    """Bounded, cycle-safe traversal collecting classifier evidence from
    `properties` (including nested objects), `allOf`/`anyOf`/`oneOf`
    branches, local `$ref` resolution, `patternProperties`, and
    `additionalProperties`."""
    if depth > _MAX_SCHEMA_WALK_DEPTH:
        result.unresolvable = True
        return
    if not isinstance(schema, Mapping):
        return

    ref = schema.get("$ref")
    if ref is not None:
        if not isinstance(ref, str) or not ref.startswith("#/") or ref in seen_refs:
            # External/non-local or cyclic reference: cannot be proven
            # bounded from this document alone.
            result.unresolvable = True
            return
        resolved = _resolve_local_ref(ref, root_schema)
        if resolved is None:
            result.unresolvable = True
            return
        _walk_schema(
            resolved,
            root_schema,
            depth + 1,
            seen_refs | {ref},
            is_strong_name,
            is_executor_description,
            result,
        )
        return

    for comp_key in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(comp_key)
        if branches is None:
            continue
        if not isinstance(branches, list):
            result.unresolvable = True
            continue
        for branch in branches:
            _walk_schema(
                branch,
                root_schema,
                depth + 1,
                seen_refs,
                is_strong_name,
                is_executor_description,
                result,
            )

    properties = schema.get("properties")
    if properties is not None:
        if not isinstance(properties, Mapping):
            result.unresolvable = True
        else:
            for raw_key, prop_schema in properties.items():
                _consider_property(
                    raw_key,
                    prop_schema,
                    root_schema,
                    depth,
                    seen_refs,
                    is_strong_name,
                    is_executor_description,
                    result,
                )

    pattern_properties = schema.get("patternProperties")
    if isinstance(pattern_properties, Mapping):
        for pattern, pattern_schema in pattern_properties.items():
            matched_key = _pattern_matches_categorized_key(pattern)
            if matched_key is not None:
                _consider_property(
                    matched_key,
                    pattern_schema,
                    root_schema,
                    depth,
                    seen_refs,
                    is_strong_name,
                    is_executor_description,
                    result,
                )

    additional_properties = schema.get("additionalProperties")
    if additional_properties is not None and additional_properties is not False:
        if _property_is_free_form(additional_properties, True):
            # No fixed key name is available for a free-form map channel; it
            # only counts as executor-shaped evidence when corroborated by
            # the tool's own name or description (the same corroboration
            # `unbounded-script-payload` already requires of payload keys).
            if is_strong_name or is_executor_description:
                _record_free_form_key("<additionalProperties>", result)


def _classify_generic_executor(
    tool_name: str,
    input_schema: object | None,
    description: str | None,
) -> AdmissionReason | None:
    is_strong_name = _has_strong_executor_name(tool_name)
    is_executor_description = _has_executor_description_signal(description)
    schema_insufficient = _schema_is_insufficient(input_schema)

    result = _SchemaWalkResult()
    if isinstance(input_schema, Mapping):
        top_type = input_schema.get("type")
        if top_type is None or _schema_type_includes(top_type, "object"):
            _walk_schema(
                input_schema,
                input_schema,
                0,
                frozenset(),
                is_strong_name,
                is_executor_description,
                result,
            )

    has_free_form_command = bool(result.free_form_command_keys)
    s_process = bool(result.free_form_program_keys) and bool(result.free_form_argument_keys)
    s_payload = bool(result.free_form_payload_keys) and (
        result.selector_key_present or is_strong_name or is_executor_description
    )
    s_broad = bool(result.non_auxiliary_free_form_keys)

    # An unbounded command-shaped field is dangerous on its own; unrelated
    # extra properties (benign or not) do not make it safe, and no name or
    # description corroboration is required to deny it.
    if has_free_form_command:
        return "unbounded-command-input"
    if s_process:
        return "unbounded-process-input"
    if s_payload:
        return "unbounded-script-payload"
    if is_executor_description and (s_broad or result.unresolvable):
        return "executor-description-with-broad-input"
    # A strong executor identity must be affirmatively demonstrated safe
    # (empty/no schema, or every property bounded via enum/const, or only
    # auxiliary/identifier-like free-form fields); an unresolvable channel
    # or a remaining executor-shaped free-form property leaves the identity
    # uncorroborated.
    if is_strong_name and (
        schema_insufficient or result.unresolvable or result.non_identifier_free_form_keys
    ):
        return "executor-identity-with-insufficient-schema"
    return None


# `create_mcp_tool()` wraps every MCP tool in `async def mcp_callable(**kwargs)`.
# When a `Tool` is built without an explicit `tool_schema` (e.g. a caller
# constructs `Tool(mcp_config={"exec": {...}})` directly rather than going
# through server discovery), `function_to_schema()` reflects that wrapper
# into this exact deterministic shape. It carries no information from the
# remote server -- it is a fixed artifact of the wrapper's own signature and
# docstring -- and must not be treated as remote descriptor metadata by the
# admission rule.
_SYNTHETIC_MCP_WRAPPER_PARAMETERS = {
    "type": "object",
    "properties": {"kwargs": {"type": "string", "description": None}},
    "required": ["kwargs"],
}


def is_synthetic_mcp_wrapper_schema(
    mcp_tool_name: str,
    advertised_name: object,
    input_schema: object,
    description: object,
) -> bool:
    """True when a prebuilt `Tool`'s schema is the auto-generated `**kwargs` wrapper.

    `mcp_tool_name` is the key under which the tool was registered in
    `Tool.mcp_config` -- the identity `create_mcp_tool()` used to name and
    document the wrapper callable.
    """
    return (
        advertised_name == mcp_tool_name
        and description == f"MCP tool: {mcp_tool_name}"
        and input_schema == _SYNTHETIC_MCP_WRAPPER_PARAMETERS
    )


def validate_mcp_tool_admission(
    tool_name: str,
    input_schema: object | None,
    description: str | None,
) -> None:
    """Raise PermissionError when an MCP descriptor exposes a generic executor.

    Pure and synchronous: does not read MCPSecurityConfig, environment
    variables, files, pool state, or acquire a client. Registration-time
    admission control only; it does not change transport authorization or
    invocation-time permissions.
    """
    reason = _classify_generic_executor(tool_name, input_schema, description)
    if reason is None:
        return
    raise PermissionError(
        f"MCP tool {tool_name!r} was not registered: generic executor surface "
        f"detected [{reason}]. Expose a structured, bounded operation instead; "
        "this admission rule has no configuration opt-out."
    )


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
        return f"inline:{compute_hash(blob)}"

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
    def load_config(cls, path: str = ".mcp.json") -> list[str]:
        """Load MCP server configurations from a .mcp.json file.

        Returns the server names declared in THIS file. The pool accumulates
        configs across loads (``_configs`` is process-global), so callers
        that mean "the servers from the file I just loaded" must use this
        return value rather than enumerating ``_configs``.
        """
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
        return list(servers.keys())

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
