# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""The Studio application's route inventory.

Route registration and the daemon's own inventory live in :mod:`lionhub.serve.daemon.routes`;
this module lists the application's route modules and composes both inventories when the
application is served.
"""

from __future__ import annotations

from lionhub.serve.daemon.routes import (  # noqa: F401
    _DEDUP_KEYS,
    _ROUTES,
    DAEMON_ROUTE_MODULES,
    HttpMethod,
    RouteModule,
    StudioRoute,
    _reset_registry,
    iter_studio_routes,
    load_route_modules,
    studio_route,
)

#: The application's route modules, each at its position in the composed route order.
_STUDIO_ROUTE_MODULES: tuple[RouteModule, ...] = (
    RouteModule(0, "lionagi_studio.services.casts"),
    RouteModule(4, "lionagi_studio.services.definitions"),
    RouteModule(5, "lionagi_studio.services.agents"),
    RouteModule(6, "lionagi_studio.services.playbooks"),
    RouteModule(7, "lionagi_studio.services.shows"),
    RouteModule(8, "lionagi_studio.services.skills"),
    RouteModule(9, "lionagi_studio.services.plugins"),
    RouteModule(10, "lionagi_studio.services.mcp_servers"),
    RouteModule(11, "lionagi_studio.services.teams"),
    RouteModule(14, "lionagi_studio.services.projects"),
    RouteModule(17, "lionagi_studio.services.engine_defs"),
    RouteModule(18, "lionagi_studio.services.workflow_defs"),
    RouteModule(20, "lionagi_studio.services.run_tags"),
    RouteModule(21, "lionagi_studio.services.operator"),
    RouteModule(22, "lionagi_studio.services.hooks_library"),
    RouteModule(23, "lionagi_studio.services.operator_hooks"),
    RouteModule(24, "lionagi_studio.services.approvals"),
    RouteModule(27, "lionagi_studio.services.stats"),
    RouteModule(28, "lionagi_studio.services.attention"),
)


def load_studio_route_modules() -> None:
    """Import the daemon's and the application's route modules to populate the route list."""
    load_route_modules(DAEMON_ROUTE_MODULES, _STUDIO_ROUTE_MODULES)
