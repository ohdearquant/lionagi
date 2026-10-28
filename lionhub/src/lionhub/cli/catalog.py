# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Lazy discovery and composition of built-in CLI commands."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from lionagi._auto import (
    _MARKER_ATTR,
    CliBuild,
    CliParserResult,
    CliSeed,
    DuplicateRegistrationError,
    Registration,
    RegistrationContractError,
    _Marker,
)

__all__ = (
    "build_cli_parser",
    "command_exists",
    "iter_cli_seeds",
    "load_cli_command",
    "seed_for",
)

# Fixed CLI seed tuple: the 21 canonical commands in their existing order,
# copied byte-for-byte from lionagi/cli/main.py's _COMMAND_REGISTRY.
_CLI_SEEDS: tuple[CliSeed, ...] = (
    CliSeed(
        name="orchestrate",
        help="Multi-agent orchestration patterns.",
        module="lionagi.cli.orchestrate",
        aliases=("o",),
    ),
    CliSeed(
        name="agent",
        help="Spawn one-shot subagent (blocking); prints final response.",
        module="lionagi.cli.agent",
    ),
    CliSeed(
        name="casts",
        help="inspect built-in roles and modes",
        module="lionagi.casts.surfaces",
    ),
    CliSeed(
        name="engine",
        help="Run domain-specific multi-agent engine pipelines.",
        module="lionagi.cli.engine",
    ),
    CliSeed(
        name="team",
        help="Team messaging — send/receive between named agents.",
        module="lionagi.cli.team",
    ),
    CliSeed(
        name="studio",
        help="Lion Studio server",
        module="lionagi.studio.cli",
    ),
    CliSeed(
        name="schedule",
        help="Manage lionagi Studio schedules.",
        module="lionagi.studio.cli",
    ),
    CliSeed(
        name="state",
        help="Inspect and migrate lionagi state.db.",
        module="lionagi.cli.state",
    ),
    CliSeed(
        name="invoke",
        help="Track a skill-level orchestration.",
        module="lionagi.cli.invoke",
    ),
    CliSeed(
        name="kill",
        help="Terminate a running entity (run/session/play/show).",
        module="lionagi.cli.kill",
    ),
    CliSeed(
        name="mirror",
        help="Mirror Claude Code sessions into studio (live).",
        module="lionagi.cli.mirror",
    ),
    CliSeed(
        name="monitor",
        help="Observe play/agent/run progress in real-time.",
        module="lionagi.cli.monitor",
        aliases=("mon",),
    ),
    CliSeed(
        name="dispatch",
        help="Inspect and acknowledge durable dispatch_outbox rows.",
        module="lionagi.cli.dispatch",
    ),
    CliSeed(
        name="doctor",
        help="Check the lionagi CLI environment/install for common failure modes.",
        module="lionagi.cli.doctor",
    ),
    CliSeed(
        name="stats",
        help="Read-only aggregate reporting over lionagi's StateDB.",
        module="lionagi.cli.stats",
    ),
    CliSeed(
        name="plugin",
        help="Inspect, trust, and enable/disable LionAGI plugin bundles.",
        module="lionagi.cli.plugin",
    ),
    CliSeed(
        name="hooks",
        help="Import Claude Code / Codex hook configs; trust imported hook commands.",
        module="lionagi.cli.hooks",
    ),
    CliSeed(
        name="handshake",
        help="Report the machine-result contract version this build speaks.",
        module="lionagi.cli.machine",
    ),
    CliSeed(
        name="runs",
        help="List recorded runs and what each one wrote.",
        module="lionagi.cli.machine",
    ),
    CliSeed(
        name="lifecycle",
        help="Report the recorded lifecycle state of a run.",
        module="lionagi.cli.machine",
    ),
    CliSeed(
        name="mcp",
        help="Serve the lionagi MCP server (background job submit/query) over stdio.",
        module="lionagi.cli.mcp",
    ),
)


def _build_seed_by_token(seeds: tuple[CliSeed, ...]) -> dict[str, CliSeed]:
    by_token: dict[str, CliSeed] = {}
    for seed in seeds:
        for token in (seed.name, *seed.aliases):
            existing = by_token.get(token)
            if existing is not None:
                raise DuplicateRegistrationError(
                    f"Duplicate CLI seed token {token!r}: claimed by both "
                    f"{existing.name!r} and {seed.name!r}"
                )
            by_token[token] = seed
    return by_token


_cli_seeds: tuple[CliSeed, ...] = _CLI_SEEDS
_cli_seed_by_token: dict[str, CliSeed] = _build_seed_by_token(_cli_seeds)
_cli_realized: dict[str, Registration] = {}


def iter_cli_seeds() -> tuple[CliSeed, ...]:
    """Return the fixed CLI seed tuple in canonical order."""
    return _cli_seeds


def seed_for(command_name_or_alias: str) -> CliSeed | None:
    """Resolve a canonical command name or alias to its seed, or None if unknown."""
    return _cli_seed_by_token.get(command_name_or_alias)


def command_exists(command_name_or_alias: str) -> bool:
    """Report whether a token is a known canonical CLI command name or alias."""
    return command_name_or_alias in _cli_seed_by_token


def load_cli_command(seed: CliSeed) -> Registration:
    """Import a seed's module and compile its single matching CLI marker."""
    cached = _cli_realized.get(seed.name)
    if cached is not None:
        return cached
    from lionagi.ln import import_module

    module = import_module(seed.module)
    matches: list[tuple[Callable[..., Any], _Marker]] = []
    for obj in vars(module).values():
        marker = getattr(obj, _MARKER_ATTR, None)
        if marker is None or marker.cli is None or marker.cli.seed != seed.name:
            continue
        matches.append((obj, marker))
    if len(matches) != 1:
        raise RegistrationContractError(
            f"expected exactly one auto_register cli marker for seed {seed.name!r} "
            f"in {seed.module!r}, found {len(matches)}"
        )
    fn, marker = matches[0]
    if fn.__module__ != seed.module:
        raise RegistrationContractError(
            f"auto_register cli marker for seed {seed.name!r} is defined by "
            f"{fn.__module__!r}, not its seed module {seed.module!r}"
        )
    registration = Registration(
        order=_cli_seeds.index(seed),
        area=marker.area,
        module=fn.__module__,
        qualname=fn.__qualname__,
        handler=fn,
        http=None,
        cli=marker.cli,
    )
    _cli_realized[seed.name] = registration
    return registration


def _cli_version() -> str:
    from lionagi.version import __version__

    return __version__


def build_cli_parser(selected: CliSeed | None) -> CliBuild:
    """Build the root CLI parser, realizing only the selected seed's real parser."""
    parser = argparse.ArgumentParser(
        prog="li",
        description="lionagi command line — spawn subagents via any CLI-backed provider.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_cli_version()}",
        help="Print the installed lionagi version and exit.",
    )
    parser.add_argument(
        "--machine",
        action="store_true",
        help=(
            "Emit one machine-result JSON object on stdout and send every "
            "human-facing line to stderr."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    registration: Registration | None = None
    selected_parser: CliParserResult = None
    for seed in _cli_seeds:
        if selected is not None and seed.name == selected.name:
            registration = load_cli_command(seed)
            assert registration.cli is not None
            selected_parser = registration.cli.parser_factory(subparsers)
        else:
            subparsers.add_parser(seed.name, aliases=list(seed.aliases), help=seed.help)
    return CliBuild(
        parser=parser, seed=selected, registration=registration, selected_parser=selected_parser
    )
