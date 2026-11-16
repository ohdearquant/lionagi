# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Route registration and the daemon's route inventory.

A route module registers its handlers with :func:`studio_route` when it is imported. Each owner
keeps one inventory of the modules it serves: the daemon's is :data:`DAEMON_ROUTE_MODULES`, and an
attached application passes its own. :func:`load_route_modules` imports one or more inventories in
one deterministic order and refuses a method and path that two inventories both register.

An inventory entry carries the position its module had in the single route list both owners once
shared, so composing the two inventories imports the route modules in the same order as before.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, TypeVar

from starlette.responses import Response

from lionagi.ln import import_module

__all__ = (
    "DAEMON_ROUTE_MODULES",
    "HttpMethod",
    "RouteModule",
    "StudioRoute",
    "iter_studio_routes",
    "load_route_modules",
    "studio_route",
)

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


@dataclass(frozen=True, slots=True)
class RouteModule:
    """One inventory entry: a route module and its position in the composed route order."""

    position: int
    module: str


# Module-level registry state.
_ROUTES: list[StudioRoute] = []
_DEDUP_KEYS: set[tuple[str, str, str, str]] = set()

#: The route modules the daemon serves with or without an attached application.
DAEMON_ROUTE_MODULES: tuple[RouteModule, ...] = (
    RouteModule(1, "lionhub.serve.daemon.services.runs"),
    RouteModule(2, "lionhub.serve.daemon.services.run_resume"),
    RouteModule(3, "lionhub.serve.daemon.services.engine_runs"),
    RouteModule(12, "lionhub.serve.daemon.services.invocations"),
    RouteModule(13, "lionhub.serve.daemon.services.launches"),
    RouteModule(15, "lionhub.serve.daemon.services.identity"),
    RouteModule(16, "lionhub.serve.daemon.services.pairing"),
    RouteModule(19, "lionhub.serve.daemon.services.sessions"),
    RouteModule(25, "lionhub.serve.daemon.services.admin"),
    RouteModule(26, "lionhub.serve.daemon.services.schedules"),
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


def _composed(inventories: Iterable[Sequence[RouteModule]]) -> list[tuple[RouteModule, int]]:
    """Return every entry with the index of its inventory, in position order."""
    entries: list[tuple[RouteModule, int]] = []
    seen_positions: dict[int, str] = {}
    seen_modules: set[str] = set()
    for owner, inventory in enumerate(inventories):
        for entry in inventory:
            if entry.module in seen_modules:
                raise ValueError(f"Route module {entry.module} is listed in two inventories")
            if entry.position in seen_positions:
                raise ValueError(
                    f"Route modules {seen_positions[entry.position]} and {entry.module} "
                    f"share position {entry.position}"
                )
            seen_modules.add(entry.module)
            seen_positions[entry.position] = entry.module
            entries.append((entry, owner))
    entries.sort(key=lambda pair: pair[0].position)
    return entries


def load_route_modules(*inventories: Sequence[RouteModule]) -> None:
    """Import the modules of every inventory in position order to populate the route list.

    Raises ``ValueError`` when two inventories list the same module or position, or when routes
    from different inventories register the same method and path.
    """
    entries = _composed(inventories)
    for entry, _owner in entries:
        import_module(entry.module)
    owner_of = {entry.module: owner for entry, owner in entries}
    claimed: dict[tuple[str, str], tuple[int, str]] = {}
    for route in _ROUTES:
        owner = owner_of.get(route.handler.__module__)
        if owner is None:
            continue
        key = (route.method, route.path)
        previous = claimed.setdefault(key, (owner, route.handler.__module__))
        if previous[0] != owner:
            raise ValueError(
                f"{route.method} {route.path} is registered by both {previous[1]} "
                f"and {route.handler.__module__}"
            )


def iter_studio_routes(*, area: str | None = None) -> tuple[StudioRoute, ...]:
    """Return registered routes sorted by insertion order, optionally filtered by area."""
    routes = sorted(_ROUTES, key=lambda r: r.order)
    if area is not None:
        routes = [r for r in routes if r.area == area]
    return tuple(routes)


def _reset_registry() -> None:
    """Clear all registered routes and dedup keys; for use in tests only."""
    _ROUTES.clear()
    _DEDUP_KEYS.clear()
