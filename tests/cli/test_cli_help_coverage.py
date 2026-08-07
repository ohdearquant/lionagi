# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Every ``li`` flag must describe itself.

The argparse parsers under ``lionagi/cli/`` are the single source of parameter
documentation for two audiences at once: a human reading ``li <cmd> --help``,
and a calling agent reading the generated MCP tool schema, which is projected
from these same parsers. A flag with no ``help=`` is blank in both places, and
a capability nobody can describe is a capability nobody uses.

Two passes, because neither alone is sufficient:

* :func:`test_built_parsers_describe_every_argument` builds the real parsers
  the CLI builds, walking every subcommand, and reads the ``help`` argparse
  actually resolved. This is what a reader sees.
* :func:`test_every_add_argument_call_passes_help` reads the source instead,
  so parsers constructed inside a command's own entry point — ``li agent
  status`` and friends build theirs at call time and never register them on
  the top-level parser — are covered too.

Both discover their targets rather than listing them, so a flag added later is
covered without touching this file.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

import lionagi.cli.main as cli_main

CLI_ROOT = Path(cli_main.__file__).parent

# Arguments that legitimately carry no help text, each with the reason it is
# exempt. Keys are "<path relative to lionagi/cli/>::<first flag or dest>".
# This is a per-argument list on purpose: a pattern that excused a whole file
# would silently absorb every future flag added to it.
#
# `help=argparse.SUPPRESS` needs no entry here — it is an explicit, readable
# declaration in the source that the flag is hidden from the help output.
HELP_EXEMPT: dict[str, str] = {}


def _argument_key(path: Path, flags: list[str], dest: str | None) -> str:
    name = flags[0] if flags else (dest or "?")
    return f"{path.relative_to(CLI_ROOT).as_posix()}::{name}"


def _walk_actions(parser: argparse.ArgumentParser, prefix: str):
    """Yield ``(command_path, action)`` for a parser and every subparser under it."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _walk_actions(sub, f"{prefix} {name}")
            continue
        yield prefix, action


def _built_parsers() -> list[argparse.ArgumentParser]:
    """Build the root parser once per command, so each one loads its real subparser.

    ``li`` registers every command for usage listing but only builds the
    selected one for real; the rest stay metadata-only stubs. Selecting each in
    turn is therefore the only way to reach the whole surface.
    """
    from lionagi._auto import build_cli_parser, iter_cli_seeds

    return [build_cli_parser(seed).parser for seed in iter_cli_seeds()]


def test_built_parsers_describe_every_argument():
    """Every argument on every parser the CLI builds resolves a non-empty help."""
    missing: set[str] = set()
    seen: set[str] = set()
    for parser in _built_parsers():
        for command_path, action in _walk_actions(parser, "li"):
            if action.help is argparse.SUPPRESS:
                continue
            name = (action.option_strings or [action.dest])[0]
            key = f"{command_path}::{name}"
            if key in HELP_EXEMPT:
                continue
            seen.add(key)
            if not (action.help or "").strip():
                missing.add(f"{command_path} {name}")

    assert len(seen) > 100, f"parser discovery collapsed — only {len(seen)} arguments walked"
    assert not missing, "arguments with no help text:\n  " + "\n  ".join(sorted(missing))


def _add_argument_calls():
    """Yield ``(path, call_node)`` for every ``.add_argument(...)`` under lionagi/cli/."""
    for path in sorted(CLI_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
            ):
                yield path, node


def _is_suppress(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "SUPPRESS"


def test_every_add_argument_call_passes_help():
    """Every ``add_argument`` call site in lionagi/cli/ passes a non-empty help."""
    failures: list[str] = []
    seen = 0
    for path, call in _add_argument_calls():
        flags = [a.value for a in call.args if isinstance(a, ast.Constant)]
        keywords = {kw.arg: kw.value for kw in call.keywords}
        dest = keywords.get("dest")
        key = _argument_key(path, flags, dest.value if isinstance(dest, ast.Constant) else None)
        if key in HELP_EXEMPT:
            continue
        seen += 1

        help_node = keywords.get("help")
        if help_node is None:
            failures.append(f"{key} (line {call.lineno}): no help= argument")
            continue
        if _is_suppress(help_node):
            continue
        # A computed help string (an f-string, or text a playbook supplies) is
        # accepted as present; only a literal can be checked for emptiness here.
        if isinstance(help_node, ast.Constant) and not str(help_node.value).strip():
            failures.append(f"{key} (line {call.lineno}): help= is empty")

    assert seen > 100, f"source discovery collapsed — only {seen} call sites found"
    assert not failures, "add_argument calls without usable help:\n  " + "\n  ".join(failures)


def test_exemptions_carry_a_reason():
    """An exemption without a stated reason is an undocumented gap."""
    unexplained = sorted(key for key, reason in HELP_EXEMPT.items() if not reason.strip())
    assert not unexplained, "exempt with no reason given: " + ", ".join(unexplained)
