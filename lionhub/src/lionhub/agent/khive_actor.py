# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Give the khive MCP servers a child agent is handed an actor of their own.

khive resolves its default namespace as: the ``--actor`` flag (``--namespace``
is its older alias, and ``--actor`` wins over it), then ``[actor] id`` in
``<cwd>/.khive/config.toml``, then anonymous. The ``KHIVE_ACTOR`` environment
variable sets attribution only, never the namespace. A child's ``kkernel mcp``
server is given ``--actor agent:<profile>``, so the child's attribution and its
default namespace are both its own: reads and writes that name no namespace use
``agent:<profile>``, not the namespace of its working directory or ``local``.
Any ``--actor`` or ``--namespace`` already in the server's arguments is
replaced. The environment variable is set too, and is all an entry gets when it
has no argument list or does not run the ``mcp`` subcommand, whose parser is the
one that accepts the flag. The server's packs are left as configured, so a child
keeps every verb its server loads, ``comm`` included, under its own actor.
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


_NAMESPACE_FLAGS = ("--actor", "--namespace")


def _without_actor_flags(args: list[Any]) -> list[Any]:
    """``args`` without any ``--actor`` or ``--namespace`` flag, in ``--flag=<id>``, ``--flag <id>`` or bare form."""
    kept: list[Any] = []
    index = 0
    while index < len(args):
        value = args[index]
        if value in _NAMESPACE_FLAGS:
            following = args[index + 1] if index + 1 < len(args) else None
            has_value = isinstance(following, str) and not following.startswith("-")
            index += 2 if has_value else 1
            continue
        if isinstance(value, str) and value.startswith(
            tuple(f"{flag}=" for flag in _NAMESPACE_FLAGS)
        ):
            index += 1
            continue
        kept.append(value)
        index += 1
    return kept


def _attributed(spec: Mapping[str, Any], actor: str) -> dict[str, Any]:
    """A shallow copy of a khive server entry whose arguments and env name ``actor``."""
    args = spec.get("args")
    if isinstance(args, (list, tuple)) and "mcp" in args:
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
    whose ``command`` basename is ``kkernel`` is a shallow copy with a new ``env``
    whose ``KHIVE_ACTOR`` is ``actor``, replacing any value there; when its
    arguments run the ``mcp`` subcommand they also end in ``--actor`` ``actor``,
    with any earlier ``--actor`` or ``--namespace`` removed, which makes ``actor``
    the server's default namespace as well as its attribution. Every other server, and every other key, is passed through
    unchanged. The input is never mutated.
    """
    if servers is None:
        return None
    return {
        name: _attributed(spec, actor) if is_khive_server(spec) else spec
        for name, spec in servers.items()
    }
