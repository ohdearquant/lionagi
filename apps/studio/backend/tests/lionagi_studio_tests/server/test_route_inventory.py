# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The application's route inventory continues the daemon's and registers through its decorator."""

from __future__ import annotations

from lionhub.serve.daemon import routes


def _application_inventory():
    from lionagi_studio import registry

    return registry._STUDIO_ROUTE_MODULES


def test_the_two_inventories_cover_one_contiguous_order():
    entries = [*routes.DAEMON_ROUTE_MODULES, *_application_inventory()]
    assert sorted(e.position for e in entries) == list(range(len(entries)))
    assert len({e.module for e in entries}) == len(entries)
    assert len(routes.DAEMON_ROUTE_MODULES) == 10


def test_the_application_registry_reuses_the_daemon_decorator_and_route_list():
    from lionagi_studio import registry

    assert registry.studio_route is routes.studio_route
    assert registry._ROUTES is routes._ROUTES
    assert registry.iter_studio_routes is routes.iter_studio_routes
