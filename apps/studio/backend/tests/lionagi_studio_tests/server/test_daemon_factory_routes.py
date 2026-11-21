# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Which routes the daemon serves, alone and with Studio attached, and in what order."""

from __future__ import annotations

from fastapi.routing import APIRoute

from lionhub.serve.daemon.app import create_daemon_app
from lionhub.serve.daemon.routes import DAEMON_ROUTE_MODULES


def _api_routes(application) -> list[APIRoute]:
    return [route for route in application.routes if isinstance(route, APIRoute)]


def _position_of(route: APIRoute, inventories) -> int:
    positions = {entry.module: entry.position for inventory in inventories for entry in inventory}
    return positions[route.endpoint.__module__]


def test_the_daemon_alone_serves_no_application_route_another_import_registered() -> None:
    # Importing an application route module registers its routes in this process; the
    # daemon's own inventory still decides what it serves.
    import lionagi_studio.services.casts  # noqa: F401

    application = create_daemon_app()

    served = {route.path for route in _api_routes(application)}
    assert "/api/runs/" in served
    assert "/api/casts/" not in served
    assert application.state.attachment is None


def test_routes_follow_the_composed_inventory_positions() -> None:
    from lionagi_studio.app import create_app
    from lionagi_studio.registry import _STUDIO_ROUTE_MODULES

    inventories = (DAEMON_ROUTE_MODULES, _STUDIO_ROUTE_MODULES)
    inventory_modules = {entry.module for inventory in inventories for entry in inventory}
    every = _api_routes(create_app())
    routes = [route for route in every if route.endpoint.__module__ in inventory_modules]
    # The daemon's own /health is the only route outside the inventories.
    assert [route.path for route in every if route not in routes] == ["/health"]
    positions = [_position_of(route, inventories) for route in routes]

    assert positions == sorted(positions)
    # Both owners' routes are served, interleaved by position.
    assert {route.endpoint.__module__ for route in routes} == inventory_modules
