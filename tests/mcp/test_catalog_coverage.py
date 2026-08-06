# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Every command the CLI offers is either registered or named absent.

The privilege fence already guards the other direction: a verb cannot become
reachable without someone writing its path into a reviewed list. Nothing guarded
this one. A command could be added to the CLI and the catalog would simply not
mention it, and silence reads the same as considered-and-declined -- which is the
one thing the absent entries exist to distinguish. Twenty-three commands had
accumulated that way before this test existed.

The CLI surface is measured here rather than listed, because a list of command
paths in a test is a second copy of the parser tree and would go stale in the
same way the catalog did.
"""

from __future__ import annotations

import argparse

import pytest

from lionagi.cli.main import _COMMAND_REGISTRY
from lionagi.mcp.verbs import ABSENT, FENCED_PATHS, VERBS


def _leaves(parser: argparse.ArgumentParser, prefix: str) -> dict[str, str]:
    """Every typed leaf spelling under *parser*, mapped to its canonical one.

    Aliases are measured, not skipped. Dropping them would leave a command
    reachable only under an alias invisible to this test, which is the shape of
    silence it exists to catch. argparse keeps no canonical flag, so the first
    name registered for a given parser object is taken as canonical — that is the
    `name` argument of `add_parser`, with `aliases=` following it.
    """
    subactions = [
        action
        for action in parser._actions  # noqa: SLF001 — the parser tree has no public reader
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
    ]
    if not subactions:
        return {prefix: prefix}
    found: dict[str, str] = {}
    for action in subactions:
        canonical_of: dict[int, str] = {}
        for name, sub in action.choices.items():
            canonical_name = canonical_of.setdefault(id(sub), name)
            for spelling, canonical in _leaves(sub, f"{prefix} {name}").items():
                # Rewrite this level's segment back to the canonical spelling,
                # leaving deeper levels' own canonicalisation intact.
                found[spelling] = canonical.replace(
                    f"{prefix} {name}", f"{prefix} {canonical_name}", 1
                )
    return found


def _cli_leaves() -> tuple[dict[str, str], dict[str, str]]:
    """Every typed leaf spelling in the CLI, mapped to its canonical path.

    Top-level aliases are included for the same reason as nested ones, and are
    canonicalised to the registry's own `spec.name`.
    """
    leaves: dict[str, str] = {}
    unbuildable: dict[str, str] = {}
    for spec in _COMMAND_REGISTRY:
        root = argparse.ArgumentParser(prog="li")
        subparsers = root.add_subparsers(dest="command")
        try:
            getattr(spec.loader(), spec.parser_factory)(subparsers)
        except Exception as exc:  # noqa: BLE001 — recorded, and failed on by the caller
            unbuildable[spec.name] = f"{type(exc).__name__}: {exc}"
            continue
        for name, sub in subparsers.choices.items():
            if name not in (spec.name, *spec.aliases):
                continue
            for spelling, canonical in _leaves(sub, name).items():
                leaves[spelling] = canonical.replace(name, spec.name, 1)
    return leaves, unbuildable


def _measured_surface() -> dict[str, str]:
    """The leaf map, or a failure naming what went unmeasured.

    A coverage gate that cannot see part of the surface must fail rather than
    report on the part it can see. The earlier version printed the unbuildable
    commands and passed, so a whole command tree could go unmeasured while this
    file claimed the catalog covered everything. If an optional extra is missing,
    that is the environment to fix — not a silence to inherit.
    """
    leaves, unbuildable = _cli_leaves()
    assert unbuildable == {}, (
        "these top-level commands' parsers would not build, so their subcommands "
        f"went unmeasured and this gate cannot speak for them: {unbuildable}"
    )
    assert leaves, "no CLI commands were measured; the parser walk is broken"
    return leaves


# A fence for a capability that has no command yet. Kept deliberately: removing a
# fence because its path is absent today is how the path comes back unfenced, and
# store migration is exactly the capability that must not arrive reachable. Listed
# here so that a *typo* or a retired path in FENCED_PATHS still fails the
# existence check below instead of hiding inside a blanket exemption.
PREEMPTIVE_FENCES = frozenset({"state migrate"})


def test_every_cli_command_is_registered_or_named_absent():
    leaves = _measured_surface()

    registered = {verb.cli_path for verb in VERBS.values() if verb.cli_path is not None}
    named_absent = {absent.cli_path for absent in ABSENT}
    # A fenced path is accounted for, and accounted for somewhere that deliberately
    # keeps it out of the catalog: naming it there would tell the caller it is
    # fenced from that the capability exists. That is not the silence this test is
    # about, so it is subtracted rather than demanded as an absent entry. The
    # subtraction is only safe because every fenced path is itself checked below.
    fenced = set(FENCED_PATHS)

    # Coverage is asked of canonical paths: an alias and its canonical name are one
    # parser, so one catalog entry answers for both. Asking of every spelling would
    # demand an entry per alias, which is a second name for one operation.
    silent = sorted(set(leaves.values()) - registered - named_absent - fenced)
    assert silent == [], (
        "these CLI commands are neither registered nor named absent, so the catalog "
        f"is silent about them: {silent}. Add a Verb if the path answers "
        "`--machine`, or an AbsentVerb with the reason it cannot."
    )


def test_an_alias_reaches_a_command_the_catalog_accounts_for():
    """Aliases are in scope, and their treatment is stated rather than assumed.

    `team ls`, `team recv`, `li o` and `li mon` are callable spellings. The policy
    is that a spelling is covered by its canonical path's catalog entry, which
    holds because they are the same parser. What this asserts is that every
    spelling resolves to a canonical path the catalog accounts for — so a command
    added only under an alias cannot slip through.
    """
    leaves = _measured_surface()
    accounted = (
        {verb.cli_path for verb in VERBS.values() if verb.cli_path is not None}
        | {absent.cli_path for absent in ABSENT}
        | set(FENCED_PATHS)
    )
    aliases = {
        spelling: canonical for spelling, canonical in leaves.items() if spelling != canonical
    }
    assert aliases, "no aliases were measured; the canonicalisation is not being exercised"
    unaccounted = sorted(
        f"{spelling} -> {canonical}"
        for spelling, canonical in aliases.items()
        if canonical not in accounted
    )
    assert unaccounted == [], (
        f"alias spellings resolving to nothing the catalog names: {unaccounted}"
    )


def test_every_fenced_path_is_a_real_command_or_a_declared_preemptive_fence():
    """The fenced set is subtracted from coverage, so it must not be a place to hide.

    Without this, adding a string to FENCED_PATHS silently waives coverage for it,
    and a typo or a retired path is invisible because nothing ever checks that a
    fenced string names anything.
    """
    leaves = _measured_surface()
    canonical = set(leaves.values())
    unreal = sorted(
        path for path in FENCED_PATHS if path not in canonical and path not in PREEMPTIVE_FENCES
    )
    assert unreal == [], (
        f"fenced paths that name no CLI command: {unreal}. Either the path is gone "
        "and the fence should say so, or it is pre-emptive and belongs in "
        "PREEMPTIVE_FENCES with the reason."
    )
    # The other direction: a pre-emptive fence whose command has since landed is
    # no longer pre-emptive, and leaving it here would exempt a real command from
    # the existence check for good.
    landed = sorted(path for path in PREEMPTIVE_FENCES if path in canonical)
    assert landed == [], (
        f"these are declared pre-emptive but now exist as commands: {landed}; "
        "drop them from PREEMPTIVE_FENCES so the fence is checked against the CLI"
    )


def test_no_absent_entry_names_a_command_that_is_gone():
    """The reverse drift: an absence outliving the command it speaks for.

    A stale absent entry is worse than a missing one. It answers a caller's
    question about a command that no longer exists, and the answer explains why
    it cannot be called rather than that there is nothing to call.
    """
    canonical = set(_measured_surface().values())
    stale = sorted(absent.cli_path for absent in ABSENT if absent.cli_path not in canonical)
    assert stale == [], f"absent entries naming commands the CLI no longer has: {stale}"


def test_absent_names_do_not_collide_with_registered_ones():
    overlap = sorted({absent.name for absent in ABSENT} & set(VERBS))
    assert overlap == [], f"named both available and absent: {overlap}"


@pytest.mark.parametrize("absent", ABSENT, ids=lambda a: a.name)
def test_every_absence_states_a_reason_and_a_path(absent):
    assert absent.cli_path, f"{absent.name} names no CLI path"
    assert absent.reason.strip(), f"{absent.name} gives no reason"
    # A reason is what a caller reads instead of a result, so a placeholder is a
    # silence with extra steps.
    assert len(absent.reason) > 40, (
        f"{absent.name} reason is too short to be one: {absent.reason!r}"
    )


@pytest.mark.parametrize("verb", ["team.create", "team.show", "team.send", "team.receive"])
def test_team_write_verbs_are_registered_not_absent(verb):
    """Pinned the way the bug this catalog exists to prevent would break it.

    These four used to be `AbsentVerb`s with the no-machine-seam reason —
    `team.py` had no versioned machine result to give them. Once it does, the
    catalog has to actually say so: a caller reading `available: false` here
    would still be told to fall back to a filesystem path a sandboxed worker
    cannot reach.
    """
    assert verb in VERBS, f"{verb} is still absent from the catalog"
    assert verb not in {absent.name for absent in ABSENT}
    assert VERBS[verb].executor == "machine"
    assert VERBS[verb].cli_path == verb.replace("team.", "team ")
