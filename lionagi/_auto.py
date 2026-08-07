# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Private declaration compiler for Studio HTTP routes and CLI commands."""

from __future__ import annotations

import argparse
import dataclasses
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Literal, Protocol, TypeVar, overload

Handler = TypeVar("Handler", bound=Callable[..., Any])
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
CliParserResult = argparse.ArgumentParser | Mapping[str, argparse.ArgumentParser] | None

_HTTP_METHODS: tuple[HttpMethod, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE")


class CliParserFactory(Protocol):
    """Callable that installs a command's subparser and returns its parser(s)."""

    def __call__(self, subparsers: argparse._SubParsersAction) -> CliParserResult: ...


@dataclass(frozen=True, slots=True)
class HttpDeclaration:
    """Transport-neutral metadata for one auto-registered HTTP route."""

    path: str
    method: HttpMethod
    response_model: Any | None = None
    dependencies: tuple[Any, ...] = ()
    status_code: int | None = None
    tags: tuple[str, ...] | None = None
    name: str | None = None
    summary: str | None = None
    description: str | None = None
    response_class: type[Any] | None = None
    responses: Mapping[int | str, Mapping[str, Any]] | None = None
    include_in_schema: bool = True


@dataclass(frozen=True, slots=True)
class CliDeclaration:
    """Marks a handler as the parser factory for a given CLI seed."""

    seed: str
    parser_factory: CliParserFactory


@dataclass(frozen=True, slots=True)
class CliSeed:
    """Discovery metadata for one canonical CLI command."""

    name: str
    help: str
    module: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Registration:
    """One compiled auto-registration: the declaration plus the original handler."""

    order: int
    area: str
    module: str
    qualname: str
    handler: Callable[..., Any]
    http: HttpDeclaration | None = None
    cli: CliDeclaration | None = None


@dataclass(frozen=True, slots=True)
class CliBuild:
    """Result of building the root CLI parser for a selected (or no) seed."""

    parser: argparse.ArgumentParser
    seed: CliSeed | None
    registration: Registration | None
    selected_parser: CliParserResult


class RegistrationError(ValueError):
    """Base error for invalid or conflicting auto-registration declarations."""


class InvalidRegistrationError(RegistrationError):
    """Raised when a declaration itself is malformed."""


class DuplicateRegistrationError(RegistrationError):
    """Raised when two distinct handlers claim the same registration identity."""


class RegistrationContractError(RegistrationError):
    """Raised when compiled markers violate the one-marker-per-seed contract."""


@dataclass(frozen=True, slots=True)
class _Marker:
    """Private immutable marker attached to a decorated handler."""

    area: str
    http: HttpDeclaration | None
    cli: CliDeclaration | None


_MARKER_ATTR = "_lionagi_auto_marker"

# Fixed HTTP module import order, mirroring lionagi/studio/registry.py's
# current _STUDIO_ROUTE_MODULES exactly; no consumer is wired to this
# compiler yet (C2 is deferred), so it must import what actually exists today.
_HTTP_MODULES: tuple[str, ...] = (
    "lionagi.studio.services.casts",
    "lionagi.studio.services.runs",
    "lionagi.studio.services.run_resume",
    "lionagi.studio.services.engine_runs",
    "lionagi.studio.services.definitions",
    "lionagi.studio.services.agents",
    "lionagi.studio.services.playbooks",
    "lionagi.studio.services.shows",
    "lionagi.studio.services.skills",
    "lionagi.studio.services.plugins",
    "lionagi.studio.services.mcp_servers",
    "lionagi.studio.services.teams",
    "lionagi.studio.services.invocations",
    "lionagi.studio.services.launches",
    "lionagi.studio.services.projects",
    "lionagi.studio.services.engine_defs",
    "lionagi.studio.services.workflow_defs",
    "lionagi.studio.services.sessions",
    "lionagi.studio.services.run_tags",
    "lionagi.studio.services.operator",
    "lionagi.studio.services.approvals",
    "lionagi.studio.services.admin",
    "lionagi.studio.services.schedules",
    "lionagi.studio.services.stats",
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


# Module-level registry state.
_http: list[Registration] = []
_http_keys: dict[tuple[str, HttpMethod, str, str], Callable[..., Any]] = {}
_cli_seeds: tuple[CliSeed, ...] = _CLI_SEEDS
_cli_seed_by_token: dict[str, CliSeed] = _build_seed_by_token(_cli_seeds)
_cli_realized: dict[str, Registration] = {}


@overload
def auto_register(
    *, area: str, http: HttpDeclaration, cli: None = None
) -> Callable[[Handler], Handler]: ...


@overload
def auto_register(
    *, area: str, http: None = None, cli: CliDeclaration
) -> Callable[[Handler], Handler]: ...


def auto_register(
    *,
    area: str,
    http: HttpDeclaration | None = None,
    cli: CliDeclaration | None = None,
) -> Callable[[Handler], Handler]:
    """Attach one immutable auto-registration marker and return the handler unchanged."""
    if not area:
        raise InvalidRegistrationError("auto_register requires a non-empty area")
    if (http is None) == (cli is None):
        raise InvalidRegistrationError("auto_register requires exactly one of http or cli")
    if http is not None:
        if not http.path:
            raise InvalidRegistrationError(
                "auto_register http declaration requires a non-empty path"
            )
        if http.method not in _HTTP_METHODS:
            raise InvalidRegistrationError(
                f"auto_register http declaration has an invalid method: {http.method!r}"
            )
    if cli is not None and not cli.seed:
        raise InvalidRegistrationError("auto_register cli declaration requires a non-empty seed")
    marker = _Marker(area=area, http=http, cli=cli)

    def decorator(fn: Handler) -> Handler:
        setattr(fn, _MARKER_ATTR, marker)
        return fn

    return decorator


def _iter_local_http_markers(module: Any) -> Iterator[tuple[Callable[..., Any], _Marker]]:
    for obj in vars(module).values():
        marker = getattr(obj, _MARKER_ATTR, None)
        if marker is None or marker.http is None:
            continue
        if getattr(obj, "__module__", None) != module.__name__:
            continue
        yield obj, marker


def _compile_http(fn: Callable[..., Any], area: str, http: HttpDeclaration) -> None:
    key = (http.path, http.method, fn.__module__, fn.__qualname__)
    existing = _http_keys.get(key)
    if existing is not None:
        if existing is fn:
            return
        raise DuplicateRegistrationError(
            f"Duplicate auto_register http registration: {http.method} {http.path} "
            f"({fn.__module__}.{fn.__qualname__})"
        )
    resolved = http if http.tags is not None else dataclasses.replace(http, tags=(area,))
    registration = Registration(
        order=len(_http),
        area=area,
        module=fn.__module__,
        qualname=fn.__qualname__,
        handler=fn,
        http=resolved,
        cli=None,
    )
    _http.append(registration)
    _http_keys[key] = fn


def load_http_modules() -> None:
    """Import each HTTP module in fixed order and compile its http markers."""
    for module_path in _HTTP_MODULES:
        module = import_module(module_path)
        for fn, marker in _iter_local_http_markers(module):
            assert marker.http is not None
            _compile_http(fn, marker.area, marker.http)


def iter_http(*, area: str | None = None) -> tuple[Registration, ...]:
    """Return compiled HTTP registrations sorted by order, optionally filtered by area."""
    registrations = sorted(_http, key=lambda r: r.order)
    if area is not None:
        registrations = [r for r in registrations if r.area == area]
    return tuple(registrations)


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


@contextmanager
def _isolated_registry_for_tests() -> Iterator[None]:
    """Snapshot both compiled surfaces, clear them, and restore on exit."""
    http_snapshot = list(_http)
    http_keys_snapshot = dict(_http_keys)
    cli_realized_snapshot = dict(_cli_realized)
    _http.clear()
    _http_keys.clear()
    _cli_realized.clear()
    try:
        yield
    finally:
        _http.clear()
        _http.extend(http_snapshot)
        _http_keys.clear()
        _http_keys.update(http_keys_snapshot)
        _cli_realized.clear()
        _cli_realized.update(cli_realized_snapshot)
