# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Give the khive MCP servers a child agent is handed an actor of their own.

The server's ``--actor`` flag names the child actor. Its ``KHIVE_ACTOR``
environment variable carries the same name. The server's packs are left as
configured.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

__all__ = (
    "KHIVE_ACTOR_ENV",
    "attribute_khive_servers",
    "child_khive_actor",
    "is_khive_server",
)

# The actor id is a name, never a secret, so a provider that keeps a server's env
# off the command line may still pass this one key on it.
KHIVE_ACTOR_ENV = "KHIVE_ACTOR"
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


def is_khive_server(spec: Any) -> bool:
    """Whether a server entry launches the khive kernel (command basename ``kkernel``)."""
    if not isinstance(spec, Mapping):
        return False
    command = spec.get("command")
    return isinstance(command, str) and os.path.basename(command) == _KHIVE_COMMAND


def _actor_args(value: Any, actor: str) -> list[Any]:
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
    offset = 1 if original and original[0] == "mcp" else len(args)
    args[offset:offset] = ["--actor", actor]
    return args


def _attributed(spec: Mapping[str, Any], actor: str) -> dict[str, Any]:
    """Copy a khive server with ``actor`` in its arguments and environment."""
    existing = spec.get("env")
    env: dict[str, Any] = dict(existing) if isinstance(existing, Mapping) else {}
    env[KHIVE_ACTOR_ENV] = actor
    server = dict(spec)
    server["args"] = _actor_args(spec.get("args"), actor)
    server["env"] = env
    return server


def attribute_khive_servers(servers: Mapping[str, Any] | None, actor: str) -> dict[str, Any] | None:
    """Copy ``servers`` with each khive server's actor set to ``actor``.

    Returns None for None. Otherwise returns a new mapping in which every server
    whose ``command`` basename is ``kkernel`` gets new ``args`` and ``env``.
    The new ``--actor`` replaces any actor or namespace option, and
    ``KHIVE_ACTOR`` carries the same value. Other entries are passed through.
    """
    if servers is None:
        return None
    return {
        name: _attributed(spec, actor) if is_khive_server(spec) else spec
        for name, spec in servers.items()
    }
