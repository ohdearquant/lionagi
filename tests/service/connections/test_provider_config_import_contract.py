# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Lazy provider type resolution uses the shared module import contract."""

from __future__ import annotations

import sys
import types

import pytest

from lionagi.service.connections import provider_config


@pytest.fixture
def provider_type_package(tmp_path, monkeypatch):
    name = "_provider_type_import_case"
    root = tmp_path / name
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    yield name, root
    for module_name in tuple(sys.modules):
        if module_name == name or module_name.startswith(name + "."):
            sys.modules.pop(module_name, None)


def test_lazy_type_resolves_a_real_class_once_through_the_shared_importer(
    provider_type_package, monkeypatch
):
    from lionagi.ln import import_module

    name, root = provider_type_package
    state = types.ModuleType(name + ".state")
    state.loads = []
    monkeypatch.setitem(sys.modules, state.__name__, state)
    (root / "options.py").write_text(
        "from pydantic import BaseModel\n"
        "from .state import loads\n"
        "loads.append('options')\n"
        "class Options(BaseModel):\n"
        "    value: str = 'ready'\n",
        encoding="utf-8",
    )
    target = name + ".options"
    bound_importer = getattr(provider_config, "import_module", None)
    calls = []

    def observed_import(module_name, *args, **kwargs):
        calls.append((module_name, args, kwargs))
        return import_module(module_name, *args, **kwargs)

    monkeypatch.setattr(provider_config, "import_module", observed_import, raising=False)
    lazy_type = provider_config.LazyType(target + ":Options")
    assert target not in sys.modules
    resolved = lazy_type.resolve()
    assert resolved is sys.modules[target].Options
    assert resolved().value == "ready"
    assert lazy_type.resolve() is resolved
    assert state.loads == ["options"]
    assert bound_importer is import_module
    assert [call[0] for call in calls] == [target]
    assert all(not args and kwargs.get("import_name") is None for _, args, kwargs in calls)


@pytest.mark.parametrize("error_kind", ["ordinary", "subclass", "missing"])
def test_lazy_type_preserves_import_errors_and_retries_without_caching_failure(
    provider_type_package, monkeypatch, error_kind
):
    name, root = provider_type_package

    class ProviderImportError(ImportError):
        pass

    error_type = {
        "ordinary": ImportError,
        "subclass": ProviderImportError,
        "missing": ModuleNotFoundError,
    }[error_kind]
    original = error_type(
        "provider options unavailable", name="optional_options", path="options.py"
    )
    state = types.ModuleType(name + ".state")
    state.error = original
    monkeypatch.setitem(sys.modules, state.__name__, state)
    target = name + ".options"
    module_file = root / "options.py"
    module_file.write_text("from .state import error\nraise error\n", encoding="utf-8")
    lazy_type = provider_config.LazyType(target + ":Options")

    with pytest.raises(ImportError) as caught:
        lazy_type.resolve()

    assert lazy_type._resolved is None
    assert target not in sys.modules
    if error_kind == "missing":
        assert caught.value is original
        assert caught.value.name == "optional_options"
        assert caught.value.path == "options.py"
        assert caught.value.msg == "provider options unavailable"
    else:
        assert type(caught.value) is ImportError
        assert str(caught.value) == f"Failed to import module {target}: {original}"
        assert caught.value.msg == str(caught.value)
        assert caught.value.name is None
        assert caught.value.path is None
        assert caught.value.__cause__ is original

    module_file.write_text(
        "from pydantic import BaseModel\n"
        "class Options(BaseModel):\n"
        "    value: str = 'recovered after import failure'\n",
        encoding="utf-8",
    )
    resolved = lazy_type.resolve()
    assert resolved is sys.modules[target].Options
    assert resolved().value == "recovered after import failure"
    assert lazy_type.resolve() is resolved


@pytest.mark.parametrize("attribute", ["child", ""], ids=["child", "empty"])
def test_lazy_type_missing_attribute_does_not_import_a_package_child(
    provider_type_package, attribute
):
    name, root = provider_type_package
    (root / "child.py").write_text("EXECUTED = True\n", encoding="utf-8")
    lazy_type = provider_config.LazyType(name + ":" + attribute)

    with pytest.raises(AttributeError):
        lazy_type.resolve()

    assert lazy_type._resolved is None
    assert name + ".child" not in sys.modules


def test_lazy_type_keeps_attribute_import_errors_outside_module_import_wrapping(
    provider_type_package, monkeypatch
):
    name, root = provider_type_package
    original = ImportError(
        "attribute unavailable", name="attribute_dependency", path="attribute.py"
    )
    state = types.ModuleType(name + ".state")
    state.error = original
    monkeypatch.setitem(sys.modules, state.__name__, state)
    (root / "__init__.py").write_text(
        "from .state import error\ndef __getattr__(name):\n    raise error\n",
        encoding="utf-8",
    )

    with pytest.raises(ImportError) as caught:
        provider_config.LazyType(name + ":Options").resolve()

    assert caught.value is original
    assert caught.value.name == "attribute_dependency"
    assert caught.value.path == "attribute.py"
