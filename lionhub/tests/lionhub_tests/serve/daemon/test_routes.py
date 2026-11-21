"""The daemon and the application each keep one route inventory, composed in the original order."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from lionhub.serve.daemon import routes

_ROOT = Path(__file__).resolve().parents[5]


@pytest.fixture
def scratch_registry(monkeypatch, tmp_path):
    """An empty route list and a directory of throwaway route modules on sys.path."""
    monkeypatch.setattr(routes, "_ROUTES", [])
    monkeypatch.setattr(routes, "_DEDUP_KEYS", set())
    monkeypatch.syspath_prepend(str(tmp_path))
    names: list[str] = []

    def write(name: str, *paths: str) -> str:
        body = "from lionhub.serve.daemon.routes import studio_route\n"
        for i, path in enumerate(paths):
            body += textwrap.dedent(
                f"""
                @studio_route({path!r}, method="GET", area="t")
                def handler_{i}():
                    return None
                """
            )
        (tmp_path / f"{name}.py").write_text(body, encoding="utf-8")
        names.append(name)
        return name

    yield write
    for name in names:
        sys.modules.pop(name, None)


def test_a_method_and_path_from_two_inventories_is_refused(scratch_registry):
    a = scratch_registry("routes_probe_owner_a", "/same")
    b = scratch_registry("routes_probe_owner_b", "/same")
    with pytest.raises(ValueError, match="registered by both"):
        routes.load_route_modules(
            (routes.RouteModule(0, a),),
            (routes.RouteModule(1, b),),
        )


def test_composition_follows_position_not_inventory_order(scratch_registry):
    first = scratch_registry("routes_probe_first", "/first")
    second = scratch_registry("routes_probe_second", "/second")
    third = scratch_registry("routes_probe_third", "/third")
    routes.load_route_modules(
        (routes.RouteModule(0, first), routes.RouteModule(2, third)),
        (routes.RouteModule(1, second),),
    )
    assert [r.path for r in routes.iter_studio_routes()] == ["/first", "/second", "/third"]


@pytest.mark.parametrize(
    ("daemon", "application", "message"),
    [
        ((0, "routes_probe_x"), (0, "routes_probe_y"), "share position 0"),
        ((0, "routes_probe_x"), (1, "routes_probe_x"), "listed in two inventories"),
    ],
)
def test_an_inventory_conflict_is_refused_before_any_import(daemon, application, message):
    with pytest.raises(ValueError, match=message):
        routes.load_route_modules(
            (routes.RouteModule(*daemon),), (routes.RouteModule(*application),)
        )
    assert "routes_probe_x" not in sys.modules
    assert "routes_probe_y" not in sys.modules


def test_a_daemon_only_load_serves_no_application_route():
    script = textwrap.dedent(
        """
        import json, sys
        from lionhub.serve.daemon import routes

        def is_application(name):
            # The application package under its own name, or a module forwarded from its old
            # one. The core keeps lionagi.studio.services (empty) and lionagi.studio.operator
            # (which only forwards three names) as parents of daemon modules moved to the hub.
            if name.split(".")[0] == "lionagi_studio":
                return True
            return name.startswith("lionagi.studio.") and name not in {
                "lionagi.studio.services",
                "lionagi.studio.operator",
            }

        routes.load_route_modules(routes.DAEMON_ROUTE_MODULES)
        daemon = {e.module for e in routes.DAEMON_ROUTE_MODULES}
        print(json.dumps({
            "foreign_routes": sorted(
                {r.handler.__module__ for r in routes._ROUTES} - daemon
            ),
            "loaded": sorted(m for m in sys.modules if is_application(m)),
            # The control: the predicate does match an application module name.
            "control": is_application("lionagi_studio.services.casts"),
        }))
        """
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(_ROOT), str(_ROOT / "lionhub/src")])}
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=_ROOT,
        env=env,
        timeout=120,
    )
    report = json.loads(out.stdout.strip().splitlines()[-1])
    assert report["control"] is True
    assert report["foreign_routes"] == []
    assert report["loaded"] == []
