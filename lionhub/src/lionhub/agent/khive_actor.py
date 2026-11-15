# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Give the khive MCP servers a child agent is handed an actor of their own.

khive resolves a server's actor from its ``--actor`` flag first, then from
``<cwd>/.khive/config.toml``, then from the ``KHIVE_ACTOR`` environment
variable. A child normally runs as ``agent:<profile>``: the flag names it, so
a config in the directory the child was launched from (often its parent's)
does not decide who the child is. ``KHIVE_ACTOR`` carries the same name as a
fallback. The server's packs are left as configured.

A write-capable child started by the lion MCP server is the exception. That
server writes a per-run actor config into the child's own directory, naming a
child actor derived from the submitter and the namespaces it may read, and
names the same actor in ``LIONAGI_MCP_CHILD_ACTOR``. When the config in the
child's working directory names that actor, the servers get no ``--actor``
flag, so the config decides, namespaces included. ``KHIVE_ACTOR`` carries the
same name.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

try:  # Python 3.11+
    import tomllib as _toml
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as _toml  # type: ignore[no-redef]

__all__ = (
    "KHIVE_ACTOR_ENV",
    "MINTED_CHILD_ACTOR_ENV",
    "attribute_khive_servers",
    "child_khive_actor",
    "is_khive_server",
    "minted_child_actor",
    "run_khive_actor",
)

# The actor id is a name, never a secret, so a provider that keeps a server's env
# off the command line may still pass this one key on it.
KHIVE_ACTOR_ENV = "KHIVE_ACTOR"
# Set by the lion MCP server on a child it minted a per-run actor config for.
MINTED_CHILD_ACTOR_ENV = "LIONAGI_MCP_CHILD_ACTOR"
_ACTOR_CONFIG = os.path.join(".khive", "config.toml")
_KHIVE_COMMAND = "kkernel"
_ANONYMOUS_ACTOR = "agent:anonymous"
_PROFILE_NAME = re.compile(r"[A-Za-z0-9._-]+")


def child_khive_actor(profile: str | None) -> str:
    """The khive actor id for a child agent running the named profile.

    ``agent:<profile>`` when the name is a non-empty string made only of
    ``[A-Za-z0-9._-]``; anything else (no profile, an empty name, a name with a
    path separator, a space or a non-ASCII character) is ``agent:anonymous``.
    """
    if isinstance(profile, str) and _PROFILE_NAME.fullmatch(profile):
        return f"agent:{profile}"
    return _ANONYMOUS_ACTOR


def minted_child_actor(cwd: str | os.PathLike[str] | None = None) -> str | None:
    """The per-run actor minted for this child, or None.

    Returns the value of ``LIONAGI_MCP_CHILD_ACTOR`` only when
    ``<cwd>/.khive/config.toml`` (``cwd`` defaults to the working directory)
    names the same ``[actor].id``. If the variable is unset or empty, or the
    config is missing, unreadable or names another actor, the result is None
    and the child keeps its ``agent:<profile>`` actor. The variable alone
    never changes attribution.
    """
    actor = os.environ.get(MINTED_CHILD_ACTOR_ENV, "")
    if not actor:
        return None
    path = os.path.join(os.fspath(cwd) if cwd is not None else os.getcwd(), _ACTOR_CONFIG)
    try:
        with open(path, "rb") as fh:
            parsed = _toml.load(fh)
    except (OSError, ValueError):
        return None
    table = parsed.get("actor")
    named = table.get("id") if isinstance(table, dict) else None
    return actor if named == actor else None


def run_khive_actor(profile: str | None) -> str:
    """The khive actor this process's child runs as.

    The minted per-run actor when one is verified (``minted_child_actor``),
    otherwise ``child_khive_actor(profile)``.
    """
    return minted_child_actor() or child_khive_actor(profile)


def is_khive_server(spec: Any) -> bool:
    """Whether a server entry launches the khive kernel (command basename ``kkernel``)."""
    if not isinstance(spec, Mapping):
        return False
    command = spec.get("command")
    return isinstance(command, str) and os.path.basename(command) == _KHIVE_COMMAND


def _actor_args(value: Any, actor: str, *, flag: bool = True) -> list[Any]:
    """Copy a server's args without any actor or namespace option, then add ``--actor``.

    ``--actor`` goes right after a leading ``mcp`` subcommand, otherwise at the
    end. With ``flag=False`` nothing is added, so the server's directory config
    decides its actor.
    """
    original = value if isinstance(value, (list, tuple)) else ()
    args = []
    skip_value = False
    for arg in original:
        if skip_value:
            skip_value = False
            if not isinstance(arg, str) or not arg.startswith("-"):
                continue
        if isinstance(arg, str):
            if arg in ("--actor", "--namespace"):
                skip_value = True
                continue
            if arg.startswith(("--actor=", "--namespace=")):
                continue
        args.append(arg)
    if flag:
        offset = 1 if original and original[0] == "mcp" else len(args)
        args[offset:offset] = ["--actor", actor]
    return args


def _attributed(spec: Mapping[str, Any], actor: str, *, flag: bool) -> dict[str, Any]:
    """Copy a khive server with ``actor`` in its environment and, with ``flag``, its arguments."""
    existing = spec.get("env")
    env: dict[str, Any] = dict(existing) if isinstance(existing, Mapping) else {}
    env[KHIVE_ACTOR_ENV] = actor
    server = dict(spec)
    server["args"] = _actor_args(spec.get("args"), actor, flag=flag)
    server["env"] = env
    return server


def attribute_khive_servers(servers: Mapping[str, Any] | None, actor: str) -> dict[str, Any] | None:
    """Copy ``servers`` with each khive server's actor set to ``actor``.

    Returns None for None. Otherwise returns a new mapping in which every server
    whose ``command`` basename is ``kkernel`` gets new ``args`` and ``env``. Any
    actor or namespace option is removed from the args, and ``KHIVE_ACTOR``
    carries ``actor``. ``--actor <actor>`` is added, which overrides a directory
    config, unless ``actor`` is the verified minted actor of this child
    (``minted_child_actor``): then the directory config that names it decides,
    with its namespaces. Other entries are passed through.
    """
    if servers is None:
        return None
    flag = actor != minted_child_actor()
    return {
        name: _attributed(spec, actor, flag=flag) if is_khive_server(spec) else spec
        for name, spec in servers.items()
    }
