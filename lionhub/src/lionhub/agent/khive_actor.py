# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Give the khive MCP servers a child agent is handed an actor of their own.

khive resolves its actor as: the ``--actor`` flag, then ``[actor] id`` in
``<cwd>/.khive/config.toml``, then the ``KHIVE_ACTOR`` environment variable,
then anonymous. Setting ``KHIVE_ACTOR`` in a server's environment is therefore
a fallback only: it never overrides an actor config file found in the child's
directory. A child agent also must not get the ``comm`` pack. ``KHIVE_PACKS``
replaces the server's pack set and khive has no way to subtract one pack, so a
list that is present loses ``comm`` and a server with no list is given khive's
built-in set without it. A server whose pack set differs from the running khive
daemon's has its requests refused by the daemon and runs them in its own process,
against its own registry, so a child's ``comm`` request finds no such verb.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

__all__ = ("KHIVE_ARGV_ENV_KEYS", "attribute_khive_servers", "child_khive_actor")

_KHIVE_COMMAND = "kkernel"
_ACTOR_ENV = "KHIVE_ACTOR"
_PACKS_ENV = "KHIVE_PACKS"
_COMM_PACK = "comm"
# khive's built-in pack set (``RuntimeConfig::built_in_packs``) without ``comm``.
_CHILD_PACKS = "kg,gtd,memory,brain,schedule,knowledge,session,tool,exec,git,code,workspace,blob"
_ANONYMOUS_ACTOR = "agent:anonymous"
# The env keys this module writes: an actor id and a pack list, never secrets, so a provider
# that keeps a server's env off the command line may still pass these two on it.
KHIVE_ARGV_ENV_KEYS = frozenset({_ACTOR_ENV, _PACKS_ENV})
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


def _is_khive_server(spec: Any) -> bool:
    """Whether a server entry launches the khive kernel (command basename ``kkernel``)."""
    if not isinstance(spec, Mapping):
        return False
    command = spec.get("command")
    return isinstance(command, str) and os.path.basename(command) == _KHIVE_COMMAND


def _without_comm(packs: str) -> str:
    """``packs`` minus ``comm``, comma-joined in order; khive's own built-in set without ``comm`` when
    nothing is left.

    khive splits ``KHIVE_PACKS`` on commas and whitespace and ignores an empty value, falling back to
    a set that includes ``comm``, so an empty result is never written.
    """
    entries = [entry for entry in re.split(r"[,\s]+", packs) if entry and entry != _COMM_PACK]
    return ",".join(entries) if entries else _CHILD_PACKS


def _args_without_comm(args: Any) -> Any:
    """``args`` with every ``--pack comm`` / ``--pack=comm`` removed; a ``--pack`` flag beats
    ``KHIVE_PACKS``."""
    if not isinstance(args, list):
        return args
    kept: list[Any] = []
    skip = False
    for index, arg in enumerate(args):
        if skip:
            skip = False
            continue
        if arg == "--pack" and index + 1 < len(args) and args[index + 1] == _COMM_PACK:
            skip = True
            continue
        if arg == f"--pack={_COMM_PACK}":
            continue
        kept.append(arg)
    return kept


def _attributed(spec: Mapping[str, Any], actor: str) -> dict[str, Any]:
    """A shallow copy of a khive server entry carrying ``actor`` and no ``comm`` pack."""
    existing = spec.get("env")
    env: dict[str, Any] = dict(existing) if isinstance(existing, Mapping) else {}
    env[_ACTOR_ENV] = actor
    packs = env.get(_PACKS_ENV)
    env[_PACKS_ENV] = _without_comm(packs) if isinstance(packs, str) else _CHILD_PACKS
    server = dict(spec)
    server["env"] = env
    if "args" in server:
        server["args"] = _args_without_comm(server["args"])
    return server


def attribute_khive_servers(servers: Mapping[str, Any] | None, actor: str) -> dict[str, Any] | None:
    """Copy ``servers`` with each khive server attributed to ``actor``.

    Returns None for None. Otherwise returns a new mapping in which every server
    whose ``command`` basename is ``kkernel`` is a shallow copy with a new
    ``env``: ``KHIVE_ACTOR`` is set to ``actor`` (replacing any value there), and
    ``KHIVE_PACKS`` loses ``comm`` when present or is set to khive's built-in set
    without ``comm`` when absent. Every other server, and every other key, is
    passed through unchanged. The input is never mutated.
    """
    if servers is None:
        return None
    return {
        name: _attributed(spec, actor) if _is_khive_server(spec) else spec
        for name, spec in servers.items()
    }
