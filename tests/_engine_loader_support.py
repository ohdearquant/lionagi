# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Temporary engine-import package fixture shared by loader and caller tests."""

from __future__ import annotations

import importlib
import sys

import pytest


@pytest.fixture
def engine_import_package(tmp_path, monkeypatch):
    package = tmp_path / "_engine_loader_fixture"
    package.mkdir()
    (package / "__init__.py").write_text(
        "class TaggedImportError(ImportError): pass\n"
        "broken = TaggedImportError('engine module broke', "
        "name='forged_optional', path='forged/engine.py')\n"
        "missing = ModuleNotFoundError('missing engine dependency', "
        "name='engine_optional', path='dependency/engine.py')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in (
        "_engine_loader_fixture",
        "_engine_loader_fixture.engine",
        "_engine_loader_fixture.Unexported",
    ):
        monkeypatch.setitem(sys.modules, name, None)
        monkeypatch.delitem(sys.modules, name)
    errors = importlib.import_module("_engine_loader_fixture")
    return package, errors
