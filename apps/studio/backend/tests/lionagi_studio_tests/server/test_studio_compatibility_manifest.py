# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Studio's compatibility manifest, and the one path its attachment is called through (ADR-0138 F7)."""

from __future__ import annotations

import ast
import json
from importlib import resources
from pathlib import Path

import pytest

from lionhub.serve.daemon import compatibility
from lionhub.serve.daemon.compatibility import (
    ATTACHMENT_PROTOCOL,
    MANIFEST_NAME,
    IncompatibleApplicationError,
    check_attachment_compatibility,
)

_ROOT = Path(__file__).resolve().parents[6]
# The shipped code: where a call of the attachment would bypass the factory's check.
_SHIPPED = (
    _ROOT / "lionagi",
    _ROOT / "lionhub" / "src" / "lionhub",
    _ROOT / "apps" / "studio" / "backend" / "src" / "lionagi_studio",
)


def _calls_of(name: str, tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    ]


def test_the_shipped_studio_manifest_admits_the_installed_libraries() -> None:
    from lionagi_studio.app import attach_studio

    manifest = json.loads(resources.files("lionagi_studio").joinpath(MANIFEST_NAME).read_text())

    assert manifest["attachment_protocol"] == ATTACHMENT_PROTOCOL
    check_attachment_compatibility(attach_studio)


def test_a_stale_studio_installation_is_refused_before_any_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lionagi_studio.app import create_app

    monkeypatch.setitem(compatibility._LIBRARY_VERSIONS, "lionagi", lambda: "2.0.0")

    with pytest.raises(IncompatibleApplicationError, match="lionagi 2.0.0 is installed"):
        create_app()


def test_the_studio_attachment_is_called_only_by_the_daemon_factory() -> None:
    sources = [path for root in _SHIPPED for path in sorted(root.rglob("*.py"))]
    assert len(sources) > 100, "the shipped trees were not found"

    callers = []
    references = 0
    for path in sources:
        text = path.read_text(encoding="utf-8")
        if "attach_studio" not in text:
            continue
        tree = ast.parse(text, filename=str(path))
        callers += [
            f"{path.relative_to(_ROOT)}:{call.lineno}" for call in _calls_of("attach_studio", tree)
        ]
        references += sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Name) and node.id == "attach_studio"
        )

    # The control: the attachment is handed to the factory by name, so the scan sees it.
    assert references >= 1
    assert callers == []


def test_the_factory_checks_the_manifest_before_it_calls_the_attachment() -> None:
    path = _ROOT / "lionhub" / "src" / "lionhub" / "serve" / "daemon" / "app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    factory = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "create_daemon_app"
    )

    attach_calls = _calls_of("attach", tree)
    checks = _calls_of("check_attachment_compatibility", factory)

    # One call of the attachment in the whole module, inside the factory, after the check.
    assert len(attach_calls) == 1
    assert attach_calls[0] in list(ast.walk(factory))
    assert len(checks) == 1
    assert checks[0].lineno < attach_calls[0].lineno
