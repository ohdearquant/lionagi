# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Private declaration compiler for Studio HTTP routes and CLI commands."""

from __future__ import annotations

import argparse
import dataclasses
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
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

# Module-level registry state.
_http: list[Registration] = []
_http_keys: dict[tuple[str, HttpMethod, str, str], Callable[..., Any]] = {}


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
    from lionagi.ln import import_module

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


@contextmanager
def _isolated_registry_for_tests() -> Iterator[None]:
    """Snapshot compiled HTTP registrations, clear them, and restore on exit."""
    http_snapshot = list(_http)
    http_keys_snapshot = dict(_http_keys)
    _http.clear()
    _http_keys.clear()
    try:
        yield
    finally:
        _http.clear()
        _http.extend(http_snapshot)
        _http_keys.clear()
        _http_keys.update(http_keys_snapshot)


# Consumers can describe the unchanged marker without using its private class name.
RegistrationMarker = _Marker


def read_registration_marker(obj: Any) -> RegistrationMarker | None:
    """Return the declaration attached to an object, or None when unmarked."""
    return getattr(obj, _MARKER_ATTR, None)
