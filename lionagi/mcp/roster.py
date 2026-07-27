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
    """Every file *name* itself resolves to, in resolution order — the first one runs.

    Built from the same per-directory candidates ``load_agent_profile`` uses,
    walked in the same order and narrowed by the same spelling rule, so the file
    named here is the file that was read and everything after it is a file this
    name really does displace.

    ``match`` says how the source was reached, because the two ways are not
    equally solid. ``exact`` means a file spelled the way the caller spelled it.
    ``separator_fallback`` means no such file exists and the other separator
    spelling answered instead — still what a run would read, but reached by a
    substitution the caller did not ask for, which is worth knowing before
    editing the file it names.

    ``ambiguous`` is the case that has no source at all. A directory declaring a
    name under both separator spellings, with neither spelled as asked, is one
    the resolver refuses rather than ranks: it raises instead of picking a
    winner. Those files are listed here, unordered, and the walk stops there —
    ranking them would name a file as runnable that a submitted run rejects, and
    listing what lies past the refusal would describe files no request reaches.
    """
    from lionagi._paths import find_lionagi_dirs
    from lionagi.cli._providers import _profile_path_candidates, _resolve_plugin_profile_path

    found: list[dict[str, str]] = []
    ambiguous: list[dict[str, str]] = []
    refused = False
    if "/" not in name:
        for d in find_lionagi_dirs():
            # Every candidate in the root, not just the winning one: the two
            # layouts share a directory, so a single root can hold two
            # declarations of the same name and the loser is displaced just as
            # surely as one in a root further down.
            #
            # Within one spelling, though. '-' and '_' stand in for each other
            # only where the spelling asked for is absent, so a directory
            # holding both holds two profiles that each still resolve under
            # their own name. Grouping by spelling and keeping the one the
            # resolver would read leaves the other out of this answer entirely,
            # rather than reporting a profile a caller can run as displaced.
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
                # Reached only when nothing earlier resolved: this is where the
                # resolver raises, so there is nothing further for a request to
                # find. With a source already in hand the walk never gets here
                # at all, and these files are simply out of reach either way.
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


def _resolved_fields() -> tuple[str, ...]:
    """The keys a resolved block carries, asked of the function that builds one.

    Naming them here as a second list would let the two drift, and the drift
    would surface as a projection that silently refuses a field the unprojected
    reply still returns.
    """
    from lionagi.cli._providers import AgentProfile, profile_config

    return tuple(profile_config(AgentProfile(name="")))


def _parse_fields(fields: list[str]) -> tuple[set[str], tuple[str, ...] | None]:
    """Split a requested field list into per-profile keys and resolved sub-keys.

    An unrecognised field is refused by name rather than dropped: a caller who
    misspells one would otherwise read the missing key as a profile that does
    not declare it, which is a different and wrong answer.
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
    """One row of the list, either whole or narrowed to the fields asked for.

    A row asked for neither placement nor configuration skips the placement walk
    entirely — the fields a caller left out cost nothing to leave out.
    """
    if not projected:
        return {"name": name, **_placement(name), "resolved": config}
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

    ``names`` and ``fields`` narrow the reply rather than the work: the roster is
    resolved the same way either way, and what changes is how much of it comes
    back. Both matter because the reply is the cost — a caller that wanted one
    profile has already paid for the whole roster by the time it could filter
    one out of it. Given neither, the reply is exactly what it has always been.

    ``name`` is always present on a projected profile even when ``fields`` does
    not ask for it. It is the only key that says which profile a row is about,
    and rows that cannot be told apart are not a smaller answer but an unusable
    one.
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
            **_placement(name),
            "resolved": profile_config(profile),
            # Frontmatter keys the loader recognised no runtime meaning for,
            # by name only: enough to see that a profile declares something
            # extra, without turning this into a second route to its contents.
            "declared_extra_keys": sorted(profile.extra),
        }
