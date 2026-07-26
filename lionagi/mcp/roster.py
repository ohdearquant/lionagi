# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The agent roster: which profile names exist here, and what one of them runs.

``agent.submit`` takes ``agent=<name>`` and nothing on this surface said which
names it would accept or what one of them would do. Finding out meant leaving the
tool surface, listing ``~/.lionagi/agents/`` by hand, and guessing whether a
project directory shadowed it. These two verbs answer both questions here.

What makes the answer worth trusting is that it is not computed here. Both verbs
call ``load_agent_profile`` and ``build_agent_profile_catalog`` from
:mod:`lionagi.cli._providers` — the same functions the spawned ``li agent`` calls
when it turns ``-a NAME`` into a model, an effort and a set of flags. A second
reader of the same files would drift from the first, and the drift would be
invisible in the worst way: the verb would report one model, the run would use
another, and the caller would have no reason to doubt the verb.

Two properties of that resolver shape what these verbs have to accept and report.

Resolution reads the working directory live — git root, then the working
directory and each parent, then ``~/.lionagi/`` — and a submitted run's working
directory is the ``cwd`` argument, not this server's. So both verbs take the same
``cwd`` and resolve under it; without it they would answer accurately about the
server's roster and misleadingly about the run's.

Precedence is whole-file. The first file found wins outright: a project profile
does not merge its fields over a global one of the same name, it replaces it. So
the winning file is reported as ``source`` and every displaced file as
``shadowed``, rather than being merged into a single view that no code produces.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

__all__ = ("profile_list", "profile_show")


@contextmanager
def _resolving_under(cwd: str | None):
    """Run the resolver with the working directory a submitted run would have.

    The change is process-wide, which is safe here only because everything
    between the two calls is synchronous: dispatch awaits one op at a time, and
    an op that never awaits cannot interleave with another one.
    """
    if cwd is None:
        yield
        return
    previous = os.getcwd()
    os.chdir(Path(cwd).expanduser())
    try:
        yield
    finally:
        os.chdir(previous)


def _scope(lionagi_dir: Path) -> str:
    return "global" if lionagi_dir == Path.home() / ".lionagi" else "project"


def _roots() -> list[dict[str, Any]]:
    """The agent directories the resolver would search, in the order it searches them.

    A root with no ``agents/`` subdirectory is still listed, marked absent: that
    is why a name a caller expected to find is missing, and dropping it silently
    would leave the caller with an empty list and no explanation.
    """
    from lionagi._paths import find_lionagi_dirs

    return [
        {
            "path": str(d / "agents"),
            "scope": _scope(d),
            "exists": (d / "agents").is_dir(),
        }
        for d in find_lionagi_dirs()
    ]


def _placement(name: str) -> dict[str, Any]:
    """Every file declaring *name*, in resolution order — the first one is what runs.

    Built from the same per-directory resolver ``load_agent_profile`` uses, and
    walked in the same order, so the file named here is the file that was read.
    """
    from lionagi._paths import find_lionagi_dirs
    from lionagi.cli._providers import _resolve_plugin_profile_path, _resolve_profile_path

    found: list[dict[str, str]] = []
    if "/" not in name:
        for d in find_lionagi_dirs():
            path = _resolve_profile_path(d / "agents", name)
            if path is not None:
                found.append({"path": str(path), "scope": _scope(d)})
    plugin_path = _resolve_plugin_profile_path(name)
    if plugin_path is not None:
        found.append({"path": str(plugin_path), "scope": "plugin"})
    return {"source": found[0] if found else None, "shadowed": found[1:]}


def profile_list(*, cwd: str | None = None) -> dict[str, Any]:
    """Every profile name ``agent.submit`` would accept here, and what each resolves to."""
    from lionagi.cli._providers import build_agent_profile_catalog

    with _resolving_under(cwd):
        catalog = build_agent_profile_catalog()
        profiles = [
            {"name": name, **_placement(name), "resolved": config}
            for name, config in sorted(catalog.items())
        ]
        return {
            "cwd": os.getcwd(),
            "roots": _roots(),
            "profiles": profiles,
            "count": len(profiles),
        }


def profile_show(name: str, *, cwd: str | None = None) -> dict[str, Any]:
    """What one named profile resolves to, and which file it came from.

    A name nothing declares raises ``FileNotFoundError`` from the loader itself,
    carrying the loader's own list of what is available.
    """
    from lionagi.cli._providers import load_agent_profile, profile_config

    with _resolving_under(cwd):
        profile = load_agent_profile(name)
        return {
            "cwd": os.getcwd(),
            "name": name,
            **_placement(name),
            "resolved": profile_config(profile),
            # Frontmatter keys the loader recognised no runtime meaning for,
            # by name only: enough to see that a profile declares something
            # extra, without turning this into a second route to its contents.
            "declared_extra_keys": sorted(profile.extra),
        }
