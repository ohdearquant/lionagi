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


def _leaves(parser: argparse.ArgumentParser, prefix: str) -> list[str]:
    """Every leaf command path under *parser*, spelled as it is typed."""
    subactions = [
        action
        for action in parser._actions  # noqa: SLF001 — the parser tree has no public reader
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
    ]
    if not subactions:
        return [prefix]
    found: list[str] = []
    for action in subactions:
        seen: set[int] = set()
        for name, sub in action.choices.items():
            # Aliases point at the same parser object; count the command once.
            if id(sub) in seen:
                continue
            seen.add(id(sub))
            found.extend(_leaves(sub, f"{prefix} {name}"))
    return found


def _cli_leaves() -> tuple[frozenset[str], dict[str, str]]:
    """The CLI's leaf command paths, and the commands that would not build.

    A top-level command whose parser needs an uninstalled extra is reported
    rather than skipped silently: its subcommands are invisible to this test, so
    a gap under it would not be caught, and the caller deserves to know which
    part of the surface went unmeasured.
    """
    leaves: set[str] = set()
    unbuildable: dict[str, str] = {}
    for spec in _COMMAND_REGISTRY:
        root = argparse.ArgumentParser(prog="li")
        subparsers = root.add_subparsers(dest="command")
        try:
            getattr(spec.loader(), spec.parser_factory)(subparsers)
        except Exception as exc:  # noqa: BLE001 — a missing extra, not a catalog gap
            unbuildable[spec.name] = f"{type(exc).__name__}: {exc}"
            continue
        for name, sub in subparsers.choices.items():
            if name == spec.name:
                leaves.update(_leaves(sub, spec.name))
    return frozenset(leaves), unbuildable


def test_every_cli_command_is_registered_or_named_absent():
    leaves, unbuildable = _cli_leaves()
    assert leaves, "no CLI commands were measured; the parser walk is broken"

    registered = {verb.cli_path for verb in VERBS.values() if verb.cli_path is not None}
    named_absent = {absent.cli_path for absent in ABSENT}
    # A fenced path is accounted for, and accounted for somewhere that deliberately
    # keeps it out of the catalog: naming it there would tell the caller it is
    # fenced from that the capability exists. That is not the silence this test is
    # about, so it is subtracted rather than demanded as an absent entry.
    fenced = set(FENCED_PATHS)

    silent = sorted(leaves - registered - named_absent - fenced)
    assert silent == [], (
        "these CLI commands are neither registered nor named absent, so the catalog "
        f"is silent about them: {silent}. Add a Verb if the path answers "
        "`--machine`, or an AbsentVerb with the reason it cannot."
    )
    # Reported, not asserted: an extra that is absent here is an environment
    # fact, and failing on it would make this test's verdict depend on which
    # extras the runner installed.
    if unbuildable:
        print(f"unmeasured top-level commands: {unbuildable}")


def test_no_absent_entry_names_a_command_that_is_gone():
    """The reverse drift: an absence outliving the command it speaks for.

    A stale absent entry is worse than a missing one. It answers a caller's
    question about a command that no longer exists, and the answer explains why
    it cannot be called rather than that there is nothing to call.
    """
    leaves, unbuildable = _cli_leaves()
    stale = []
    for absent in ABSENT:
        if absent.cli_path in leaves:
            continue
        # Its whole top-level command went unmeasured, so its absence here says
        # nothing about whether the command exists.
        if absent.cli_path.split()[0] in unbuildable:
            continue
        stale.append(absent.cli_path)
    assert sorted(stale) == [], (
        f"absent entries naming commands the CLI no longer has: {sorted(stale)}"
    )


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
