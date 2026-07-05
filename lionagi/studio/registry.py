# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from typing import Any, Literal, TypeVar

from starlette.responses import Response

Handler = TypeVar("Handler", bound=Callable[..., Any])
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


@dataclass(frozen=True, slots=True)
class StudioRoute:
    """Value object holding all registration metadata for one studio endpoint."""

    order: int
    area: str
    path: str
    method: HttpMethod
    handler: Callable[..., Any]
    response_model: Any | None
    dependencies: tuple[Any, ...]
    status_code: int | None
    tags: tuple[str, ...]
    name: str | None
    summary: str | None
    description: str | None
    response_class: type[Response] | None
    responses: Mapping[int | str, Mapping[str, Any]] | None
    include_in_schema: bool


# Module-level registry state.
_ROUTES: list[StudioRoute] = []
_DEDUP_KEYS: set[tuple[str, str, str, str]] = set()

# Area modules imported at startup; order controls route registration sequence.
_STUDIO_ROUTE_MODULES: tuple[str, ...] = (
    "lionagi.studio.services.casts",
    "lionagi.studio.services.runs",
    "lionagi.studio.services.engine_runs",
    "lionagi.studio.services.definitions",
    "lionagi.studio.services.agents",
    "lionagi.studio.services.playbooks",
    "lionagi.studio.services.shows",
    "lionagi.studio.services.skills",
    "lionagi.studio.services.plugins",
    "lionagi.studio.services.teams",
    "lionagi.studio.services.invocations",
    "lionagi.studio.services.launches",
    "lionagi.studio.services.projects",
    "lionagi.studio.services.engine_defs",
    "lionagi.studio.services.workflow_defs",
    "lionagi.studio.services.sessions",
    "lionagi.studio.services.admin",
    "lionagi.studio.services.schedules",
    "lionagi.studio.services.stats",
)


def studio_route(
    path: str,
    *,
    method: HttpMethod,
    area: str,
    response_model: Any | None = None,
    dependencies: Sequence[Any] = (),
    status_code: int | None = None,
    tags: Sequence[str] | None = None,
    name: str | None = None,
    summary: str | None = None,
    description: str | None = None,
    response_class: type[Response] | None = None,
    responses: Mapping[int | str, Mapping[str, Any]] | None = None,
    include_in_schema: bool = True,
) -> Callable[[Handler], Handler]:
    """Register a handler as a studio route and return it unchanged."""

    def decorator(fn: Handler) -> Handler:
        key = (path, method, fn.__module__, fn.__qualname__)
        if key in _DEDUP_KEYS:
            raise ValueError(
                f"Duplicate studio_route registration: {method} {path} "
                f"({fn.__module__}.{fn.__qualname__})"
            )
        resolved_tags: tuple[str, ...] = (area,) if tags is None else tuple(tags)
        route = StudioRoute(
            order=len(_ROUTES),
            area=area,
            path=path,
            method=method,
            handler=fn,
            response_model=response_model,
            dependencies=tuple(dependencies),
            status_code=status_code,
            tags=resolved_tags,
            name=name,
            summary=summary,
            description=description,
            response_class=response_class,
            responses=responses,
            include_in_schema=include_in_schema,
        )
        _ROUTES.append(route)
        _DEDUP_KEYS.add(key)
        return fn

    return decorator


def load_studio_route_modules() -> None:
    """Import each area module listed in _STUDIO_ROUTE_MODULES to populate _ROUTES."""
    for module_path in _STUDIO_ROUTE_MODULES:
        import_module(module_path)


def iter_studio_routes(*, area: str | None = None) -> tuple[StudioRoute, ...]:
    """Return registered routes sorted by insertion order, optionally filtered by area."""
    routes = sorted(_ROUTES, key=lambda r: r.order)
    if area is not None:
        routes = [r for r in routes if r.area == area]
    return tuple(routes)


def _reset_registry() -> None:
    """Clear all registered routes and dedup keys — for use in tests only."""
    _ROUTES.clear()
    _DEDUP_KEYS.clear()
