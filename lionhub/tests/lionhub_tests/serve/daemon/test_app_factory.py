# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The daemon factory serves alone or with one attached application (ADR-0138 F1)."""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute

from lionagi.version import __version__
from lionhub.serve.daemon import launch_access
from lionhub.serve.daemon.app import create_daemon_app
from lionhub.serve.daemon.compatibility import (
    ATTACHMENT_PROTOCOL,
    MANIFEST_NAME,
    IncompatibleApplicationError,
)
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


_ATTACHMENT_MODULE = """
from lionhub.serve.daemon.app import Attachment

calls = []


async def startup():
    pass


async def shutdown():
    pass


ATTACHMENT = Attachment(route_modules=(), startup=startup, shutdown=shutdown)


def attach(application, services):
    calls.append((application, services))
    if FAIL:
        raise RuntimeError("application cannot be built")
    return ATTACHMENT
"""

_VALID = {"attachment_protocol": ATTACHMENT_PROTOCOL, "requires": {"lionagi": ">=0.0"}}


def _application(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest: object = _VALID,
    *,
    fail: bool = False,
) -> ModuleType:
    """An application package on ``sys.path`` whose ``attachment`` module defines ``attach``.

    ``manifest`` is written as JSON, a ``str`` verbatim, and ``None`` writes no manifest.
    """
    name = f"attached_app_{uuid4().hex}"
    package = tmp_path / name
    package.mkdir()
    (package / "__init__.py").write_text("")
    if isinstance(manifest, str):
        (package / MANIFEST_NAME).write_text(manifest)
    elif manifest is not None:
        (package / MANIFEST_NAME).write_text(json.dumps(manifest))
    (package / "attachment.py").write_text(f"FAIL = {fail!r}\n" + _ATTACHMENT_MODULE)
    monkeypatch.syspath_prepend(str(tmp_path))
    return importlib.import_module(f"{name}.attachment")


def test_attach_is_called_once_with_the_daemons_own_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _application(tmp_path, monkeypatch)

    application = create_daemon_app(module.attach)

    assert len(module.calls) == 1
    assert module.calls[0][0] is application
    assert module.calls[0][1].launches is launches
    assert module.calls[0][1].launch_access is launch_access
    assert application.state.attachment is module.ATTACHMENT


def test_an_attach_that_fails_fails_the_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _application(tmp_path, monkeypatch, fail=True)

    with pytest.raises(RuntimeError, match="application cannot be built"):
        create_daemon_app(module.attach)


def test_an_application_without_a_manifest_is_refused_before_it_is_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _application(tmp_path, monkeypatch, manifest=None)

    with pytest.raises(IncompatibleApplicationError, match="has no compatibility manifest"):
        create_daemon_app(module.attach)

    assert module.calls == []


@pytest.mark.parametrize(
    ("manifest", "detail"),
    [
        ("{not json", "not readable JSON"),
        ([1], "not an object"),
        ({"requires": {"lionagi": ">=0.0"}}, "attachment_protocol is missing"),
        ({"attachment_protocol": "1", "requires": {"lionagi": ">=0.0"}}, "not an integer"),
        ({"attachment_protocol": True, "requires": {"lionagi": ">=0.0"}}, "not an integer"),
        ({"attachment_protocol": 1}, "requires is missing"),
        ({"attachment_protocol": 1, "requires": {}}, "requires is missing, empty"),
        ({"attachment_protocol": 1, "requires": {"numpy": ">=1"}}, "unknown library 'numpy'"),
        ({"attachment_protocol": 1, "requires": {"lionagi": 1}}, "are not a string"),
        ({"attachment_protocol": 1, "requires": {"lionagi": "banana"}}, "do not parse"),
    ],
)
def test_a_malformed_manifest_is_refused_before_the_application_is_attached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manifest: object, detail: str
) -> None:
    module = _application(tmp_path, monkeypatch, manifest=manifest)

    with pytest.raises(IncompatibleApplicationError, match="is malformed") as info:
        create_daemon_app(module.attach)

    assert detail in str(info.value)
    assert module.calls == []


def test_an_application_on_another_attachment_protocol_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = {"attachment_protocol": ATTACHMENT_PROTOCOL + 1, "requires": {"lionagi": ">=0.0"}}
    module = _application(tmp_path, monkeypatch, manifest=manifest)

    with pytest.raises(IncompatibleApplicationError, match="implements attachment protocol"):
        create_daemon_app(module.attach)

    assert module.calls == []


def test_an_application_that_does_not_support_this_lionagi_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = {"attachment_protocol": ATTACHMENT_PROTOCOL, "requires": {"lionagi": "<0.0.1"}}
    module = _application(tmp_path, monkeypatch, manifest=manifest)

    with pytest.raises(IncompatibleApplicationError, match=f"lionagi {__version__} is installed"):
        create_daemon_app(module.attach)

    assert module.calls == []


def test_the_daemon_alone_reads_no_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    app_module = importlib.import_module("lionhub.serve.daemon.app")

    def refuse(_attach):
        raise AssertionError("a manifest was read for a daemon with nothing attached")

    monkeypatch.setattr(app_module, "check_attachment_compatibility", refuse)

    create_daemon_app()
