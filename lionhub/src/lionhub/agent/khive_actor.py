# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Give the khive MCP servers a child agent is handed an actor of their own.

khive resolves its actor as: the ``--actor`` flag, then ``[actor] id`` in
``<cwd>/.khive/config.toml``, then the ``KHIVE_ACTOR`` environment variable,
then anonymous. Child servers receive the actor flag so their working-directory
config cannot override their attribution; the environment variable remains a
fallback. The server's packs are left as configured, so a child keeps every
verb its server loads, ``comm`` included, under its own actor.
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


def _without_actor_flags(args: list[Any]) -> list[Any]:
    """``args`` without any ``--actor`` flag: ``--actor=<id>``, ``--actor <id>``, or a bare one."""
    kept: list[Any] = []
    index = 0
    while index < len(args):
        value = args[index]
        if value == "--actor":
            following = args[index + 1] if index + 1 < len(args) else None
            has_value = isinstance(following, str) and not following.startswith("-")
            index += 2 if has_value else 1
            continue
        if isinstance(value, str) and value.startswith("--actor="):
            index += 1
            continue
        kept.append(value)
        index += 1
    return kept


def _attributed(spec: Mapping[str, Any], actor: str) -> dict[str, Any]:
    """A shallow copy of a khive server entry whose arguments and env name ``actor``."""
    args = spec.get("args")
    if isinstance(args, (list, tuple)):
        args = [*_without_actor_flags(list(args)), "--actor", actor]
    existing = spec.get("env")
    env: dict[str, Any] = dict(existing) if isinstance(existing, Mapping) else {}
    env[KHIVE_ACTOR_ENV] = actor
    server = dict(spec)
    if args is not None:
        server["args"] = args
    server["env"] = env
    return server


def attribute_khive_servers(servers: Mapping[str, Any] | None, actor: str) -> dict[str, Any] | None:
    """Copy ``servers`` with each khive server's actor flag and environment set.

    Returns None for None. Otherwise returns a new mapping in which every server
    whose ``command`` basename is ``kkernel`` is a shallow copy with ``--actor``
    set to ``actor`` and a new ``env`` whose ``KHIVE_ACTOR`` is ``actor``, replacing
    any values there. Every other server, and every other key, is passed through
    unchanged. The input is never mutated.
    """
    if servers is None:
        return None
    return {
        name: _attributed(spec, actor) if is_khive_server(spec) else spec
        for name, spec in servers.items()
    }
