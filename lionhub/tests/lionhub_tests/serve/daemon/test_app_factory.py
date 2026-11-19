# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The daemon factory serves alone or with one attached application (ADR-0138 F1)."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from lionhub.serve.daemon import launch_access
from lionhub.serve.daemon.app import Attachment, DaemonServices, create_daemon_app
from lionhub.serve.daemon.services import launches

_ROOT = Path(__file__).resolve().parents[5]


def _api_routes(application) -> list[APIRoute]:
    return [route for route in application.routes if isinstance(route, APIRoute)]


def test_the_daemon_alone_imports_no_application_module() -> None:
    script = textwrap.dedent(
        """
        import importlib.abc, json, sys

        refused = []

        class RefuseApplication(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "lionagi.studio" or fullname.startswith("lionagi.studio."):
                    refused.append(fullname)
                    raise ImportError("blocked application import: " + fullname)

        sys.meta_path.insert(0, RefuseApplication())
        from lionhub.serve.daemon.app import create_daemon_app

        application = create_daemon_app()
        paths = sorted({getattr(route, "path", "") for route in application.routes})
        print(json.dumps({"refused": refused, "paths": paths}))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["refused"] == []
    # A daemon route is served and an application route is not: the control and the subject.
    assert "/api/runs/" in report["paths"]
    assert "/api/casts/" not in report["paths"]
    assert "/health" in report["paths"]


def test_attach_is_called_once_with_the_daemons_own_services() -> None:
    calls: list[tuple[object, DaemonServices]] = []

    async def startup() -> None:
        pass

    async def shutdown() -> None:
        pass

    attachment = Attachment(route_modules=(), startup=startup, shutdown=shutdown)

    def attach(application, services: DaemonServices) -> Attachment:
        calls.append((application, services))
        return attachment

    application = create_daemon_app(attach)

    assert len(calls) == 1
    assert calls[0][0] is application
    assert calls[0][1].launches is launches
    assert calls[0][1].launch_access is launch_access
    assert application.state.attachment is attachment


def test_an_attach_that_fails_fails_the_factory() -> None:
    def attach(application, services):
        raise RuntimeError("application cannot be built")

    with pytest.raises(RuntimeError, match="application cannot be built"):
        create_daemon_app(attach)
