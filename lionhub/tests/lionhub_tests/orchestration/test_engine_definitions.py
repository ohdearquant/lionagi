# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Engine class loading preserves identity and original import failures."""

from __future__ import annotations

import sys

import pytest


def test_engine_class_loader_keeps_class_identity_and_attribute_boundary(engine_import_package):
    from lionhub.orchestration.engine_definitions import import_engine_class

    package, _ = engine_import_package
    (package / "engine.py").write_text("class SelectedEngine: pass\n")
    selected = import_engine_class("_engine_loader_fixture.engine", "SelectedEngine")
    assert selected is sys.modules["_engine_loader_fixture.engine"].SelectedEngine
    assert import_engine_class("_engine_loader_fixture.engine", "SelectedEngine") is selected

    marker = package / "child-imported"
    (package / "Unexported.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
    )
    with pytest.raises(AttributeError, match="Unexported"):
        import_engine_class("_engine_loader_fixture", "Unexported")
    assert not marker.exists()
    assert "_engine_loader_fixture.Unexported" not in sys.modules


def test_engine_class_loader_keeps_plain_import_error_cause(engine_import_package):
    from lionhub.orchestration.engine_definitions import import_engine_class

    package, errors = engine_import_package
    (package / "engine.py").write_text("from . import broken\nraise broken\n")
    with pytest.raises(ImportError) as caught:
        import_engine_class("_engine_loader_fixture.engine", "SelectedEngine")
    assert type(caught.value) is ImportError
    assert str(caught.value) == (
        "Failed to import module _engine_loader_fixture.engine: engine module broke"
    )
    assert caught.value.__cause__ is errors.broken
    assert caught.value.name is None
    assert caught.value.path is None
    assert caught.value.msg == str(caught.value)
    assert errors.broken.name == "forged_optional"
    assert errors.broken.path == "forged/engine.py"


def test_engine_class_loader_preserves_original_missing_module(engine_import_package):
    from lionhub.orchestration.engine_definitions import import_engine_class

    package, errors = engine_import_package
    (package / "engine.py").write_text("from . import missing\nraise missing\n")
    with pytest.raises(ModuleNotFoundError) as caught:
        import_engine_class("_engine_loader_fixture.engine", "SelectedEngine")
    assert caught.value is errors.missing
    assert caught.value.name == "engine_optional"
    assert caught.value.path == "dependency/engine.py"
    assert caught.value.msg == "missing engine dependency"
