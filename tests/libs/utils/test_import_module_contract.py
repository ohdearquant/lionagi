# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Import-helper module, selector, and exception contracts."""

from __future__ import annotations

import importlib
import sys

import pytest

from lionagi.ln._utils import import_module

_PACKAGE = "_lionagi_import_contract_fixture"
_DEPENDENCY = "_lionagi_import_contract_missing_dependency"


@pytest.fixture
def package(tmp_path, monkeypatch):
    root = tmp_path / _PACKAGE
    root.mkdir()
    (root / "__init__.py").write_text("VALUE = object()\nOTHER = object()\n")
    (root / "child.py").write_text("MARKER = object()\n")
    (root / "broken.py").write_text(f"import {_DEPENDENCY}\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert not any(name == _PACKAGE or name.startswith(_PACKAGE + ".") for name in sys.modules)
    try:
        yield _PACKAGE
    finally:
        for name in list(sys.modules):
            if name == _PACKAGE or name.startswith(_PACKAGE + "."):
                sys.modules.pop(name)
        importlib.invalidate_caches()


def test_dotted_name_returns_the_leaf_module(package):
    result = import_module(package + ".child")

    assert result is sys.modules[package + ".child"]
    assert result.__name__ == package + ".child"


def test_relative_name_uses_explicit_package(package):
    result = import_module(".child", package=package)

    assert result is sys.modules[package + ".child"]


@pytest.mark.parametrize("selector", ["VALUE", [], ["VALUE"], ["OTHER", "VALUE"]])
def test_selector_preserves_string_or_list_shape(package, selector):
    module = importlib.import_module(package)

    result = import_module(package, import_name=selector)

    if isinstance(selector, str):
        assert result is module.VALUE
    else:
        assert isinstance(result, list)
        assert result == [getattr(module, name) for name in selector]


def test_empty_string_is_an_attribute_name(package):
    with pytest.raises(AttributeError):
        import_module(package, import_name="")


def test_empty_module_name_retains_package_only_behavior(package):
    assert import_module(package, module_name="") is sys.modules[package]


def test_missing_module_preserves_the_original_exception(monkeypatch):
    original = ModuleNotFoundError("missing dependency", name=_DEPENDENCY, path="dependency.py")

    def missing(*args, **kwargs):
        raise original

    monkeypatch.setattr(importlib, "import_module", missing)

    with pytest.raises(ModuleNotFoundError) as exc_info:
        import_module("missing_target")

    assert exc_info.value is original
    assert exc_info.value.name == _DEPENDENCY
    assert exc_info.value.path == "dependency.py"


def test_other_import_error_keeps_prefix_and_original_cause(monkeypatch):
    original = ImportError("fixture import failure")

    def broken(*args, **kwargs):
        raise original

    monkeypatch.setattr(importlib, "import_module", broken)

    with pytest.raises(
        ImportError, match="^Failed to import module target: fixture import failure$"
    ) as exc_info:
        import_module("target")

    assert type(exc_info.value) is ImportError
    assert exc_info.value.__cause__ is original


@pytest.mark.parametrize("selector", ["child", ["child"]], ids=["string", "list"])
def test_missing_package_attribute_keeps_cold_child_fallback(package, selector):
    module = importlib.import_module(package)
    assert not hasattr(module, "child")
    assert package + ".child" not in sys.modules

    result = import_module(package, import_name=selector)

    child = sys.modules[package + ".child"]
    if isinstance(selector, str):
        assert result is child
    else:
        assert result == [child]


def test_relative_attribute_fallback_uses_resolved_absolute_module(package):
    result = import_module(".", import_name="child", package=package)

    assert result is sys.modules[package + ".child"]


def test_absent_child_preserves_the_attribute_failure(package, monkeypatch):
    module = importlib.import_module(package)
    original = AttributeError("fixture attribute absent")

    def absent(name):
        raise original

    monkeypatch.setattr(module, "__getattr__", absent, raising=False)

    with pytest.raises(AttributeError) as exc_info:
        import_module(package, import_name="absent")

    assert exc_info.value is original


def test_child_missing_its_own_dependency_is_not_an_absent_attribute(package):
    with pytest.raises(ModuleNotFoundError) as exc_info:
        import_module(package, import_name="broken")

    assert exc_info.value.name == _DEPENDENCY


def test_explicitly_blocked_child_is_not_an_absent_attribute(package, monkeypatch):
    target = package + ".child"
    monkeypatch.setitem(sys.modules, target, None)

    with pytest.raises(ModuleNotFoundError) as exc_info:
        import_module(package, import_name="child")

    assert exc_info.value.name == target
