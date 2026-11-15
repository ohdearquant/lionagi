# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Private declaration types and the registration marker for CLI commands."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
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


# Consumers can describe the unchanged marker without using its private class name.
RegistrationMarker = _Marker


def _read_registration_marker(obj: Any) -> RegistrationMarker | None:
    """Return the declaration attached to an object, or None when unmarked."""
    return getattr(obj, _MARKER_ATTR, None)


_DEPRECATED_ALIASES = {
    "build_cli_parser": ("lionhub.serve.cli.catalog.build_cli_parser", "2.0.0"),
    "command_exists": ("lionhub.serve.cli.catalog.command_exists", "2.0.0"),
    "iter_cli_seeds": ("lionhub.serve.cli.catalog.iter_cli_seeds", "2.0.0"),
    "load_cli_command": ("lionhub.serve.cli.catalog.load_cli_command", "2.0.0"),
    "seed_for": ("lionhub.serve.cli.catalog.seed_for", "2.0.0"),
}


def __getattr__(name: str) -> Any:
    alias = _DEPRECATED_ALIASES.get(name)
    if alias is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import warnings

    from lionagi.ln import import_module

    target, removed_in = alias
    warnings.warn(
        f"{__name__}.{name} is deprecated and will be removed in lionagi {removed_in}. "
        f"Import from {target} instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    module, _, attribute = target.rpartition(".")
    return getattr(import_module(module), attribute)
