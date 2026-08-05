# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The agent roster: which profile names exist here, and what one of them runs.

Both verbs call ``load_agent_profile``/``build_agent_profile_catalog`` from
:mod:`lionagi.cli._providers` — the same functions ``li agent -a NAME`` uses
— rather than reading the profile files a second time, so a verb's answer
never drifts from what a submitted run actually does.

Resolution reads the working directory live (git root, then up, then
``~/.lionagi/``), and a submitted run's directory is the ``cwd`` argument, not
this server's — so both verbs take and resolve under the same ``cwd``.
Precedence is whole-file: the first file found wins outright (no field
merging), so the winner is ``source`` and every displaced file is ``shadowed``.
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

    Process-wide chdir, safe only because everything between the two calls is
    synchronous — an op that never awaits can't interleave with another.
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

    A root with no ``agents/`` subdirectory is still listed, marked absent, so
    a caller can tell why an expected name is missing rather than getting a
    silently shorter list.
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
    """Every file *name* itself resolves to, in resolution order — the first one runs.

    Built from the same per-directory candidates, walked in the same order and
    narrowed by the same spelling rule as ``load_agent_profile``, so the file
    named here is the file that was actually read.

    ``match``: ``exact`` is spelled as the caller asked; ``separator_fallback``
    means the other separator spelling answered instead (still what a run
    would read, but worth knowing before editing that file). ``ambiguous``
    means the resolver refuses rather than ranks — a directory declaring the
    name under both separators, with neither spelled as asked, raises instead
    of picking a winner; those files are listed unordered and the walk stops.
    """
    from lionagi._paths import find_lionagi_dirs
    from lionagi.cli._providers import _profile_path_candidates, _resolve_plugin_profile_path

    found: list[dict[str, str]] = []
    ambiguous: list[dict[str, str]] = []
    refused = False
    if "/" not in name:
        for d in find_lionagi_dirs():
            # Every candidate in the root, not just the winning one — grouped
            # by spelling, since '-'/'_' only substitute when the asked
            # spelling is absent, so both can independently resolve.
            declared: dict[str, list[Path]] = {}
            for path in _profile_path_candidates(d / "agents", name):
                if path.is_file():
                    declared.setdefault(path.stem, []).append(path)
            if name in declared:
                found.extend(
                    {"path": str(path), "scope": _scope(d), "match": "exact"}
                    for path in declared[name]
                )
                continue
            if len(declared) > 1:
                ambiguous.extend(
                    {"path": str(path), "scope": _scope(d)}
                    for paths in declared.values()
                    for path in paths
                )
                if not found:
                    refused = True
                    break
                continue
            found.extend(
                {"path": str(path), "scope": _scope(d), "match": "separator_fallback"}
                for paths in declared.values()
                for path in paths
            )
    if not refused:
        plugin_path = _resolve_plugin_profile_path(name)
        if plugin_path is not None:
            found.append({"path": str(plugin_path), "scope": "plugin", "match": "exact"})
    return {
        "source": found[0] if found else None,
        "shadowed": found[1:],
        "ambiguous": ambiguous,
    }


_PLACEMENT_FIELDS = ("source", "shadowed", "ambiguous")


def _record_placement(name: str) -> dict[str, Any]:
    """The placement a reply carries when it was not asked for anything narrower.

    ``match`` and ``ambiguous`` are reachable only by naming them in
    ``fields``, so an unreshaped reply keeps the keys it has always had.
    """
    placement = _placement(name)
    source = placement["source"]
    return {
        "source": None if source is None else {k: v for k, v in source.items() if k != "match"},
        "shadowed": [
            {k: v for k, v in entry.items() if k != "match"} for entry in placement["shadowed"]
        ],
    }


def _resolved_fields() -> tuple[str, ...]:
    """The keys a resolved block carries, asked of the function that builds one
    (not a second hardcoded list, which could drift from it)."""
    from lionagi.cli._providers import AgentProfile, profile_config

    return tuple(profile_config(AgentProfile(name="")))


def _parse_fields(fields: list[str]) -> tuple[set[str], tuple[str, ...] | None]:
    """Split a requested field list into per-profile keys and resolved sub-keys.

    An unrecognised field is refused by name rather than silently dropped —
    otherwise a typo reads as "profile doesn't declare this", a wrong answer.
    """
    resolved_fields = _resolved_fields()
    keys: set[str] = set()
    sub: list[str] = []
    whole_resolved = False
    for field in fields:
        if field in _PLACEMENT_FIELDS:
            keys.add(field)
        elif field == "resolved":
            whole_resolved = True
        elif field.startswith("resolved.") and field[len("resolved.") :] in resolved_fields:
            sub.append(field[len("resolved.") :])
        elif field != "name":
            known = ", ".join(
                (
                    "name",
                    *_PLACEMENT_FIELDS,
                    "resolved",
                    *(f"resolved.{f}" for f in resolved_fields),
                )
            )
            raise ValueError(f"unknown profile field {field!r}; known fields are: {known}")
    if whole_resolved:
        return keys, resolved_fields
    return keys, tuple(sub) if sub else None


def _profile_entry(
    name: str,
    config: dict[str, Any],
    selected: set[str],
    resolved_fields: tuple[str, ...] | None,
    *,
    projected: bool,
) -> dict[str, Any]:
    """One row of the list, either whole or narrowed to the fields asked for."""
    if not projected:
        return {"name": name, **_record_placement(name), "resolved": config}
    entry: dict[str, Any] = {"name": name}
    if selected:
        entry.update({k: v for k, v in _placement(name).items() if k in selected})
    if resolved_fields is not None:
        entry["resolved"] = {k: config[k] for k in resolved_fields}
    return entry


def profile_list(
    *,
    cwd: str | None = None,
    names: list[str] | None = None,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Every profile name ``agent.submit`` would accept here, and what each resolves to.

    ``names``/``fields`` narrow the reply, not the work — the roster resolves
    the same way regardless. ``name`` is always present on a projected profile
    even when ``fields`` doesn't ask for it, since rows that can't be told
    apart are unusable rather than merely smaller.
    """
    from lionagi.cli._providers import build_agent_profile_catalog

    selected, resolved_fields = (set(), None) if fields is None else _parse_fields(fields)
    with _resolving_under(cwd):
        catalog = build_agent_profile_catalog()
        if names is not None:
            wanted = set(names)
            catalog = {name: config for name, config in catalog.items() if name in wanted}
        profiles = [
            _profile_entry(name, config, selected, resolved_fields, projected=fields is not None)
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
            **_record_placement(name),
            "resolved": profile_config(profile),
            # Frontmatter keys with no runtime meaning, by name only — not a
            # second route to their contents.
            "declared_extra_keys": sorted(profile.extra),
        }
