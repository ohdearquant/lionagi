# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Pin the existing lazy-export boundary before optional-feature loading changes."""

import importlib
import sys

import pytest

from lionagi.ln._lazy_init import lazy_import


@pytest.fixture
def lazy_package(tmp_path, monkeypatch):
    package = "_lionagi_lazy_contract_fixture"
    prefix = package + "."
    previous = {
        name: module
        for name, module in sys.modules.items()
        if name == package or name.startswith(prefix)
    }
    for name in previous:
        monkeypatch.delitem(sys.modules, name)
    directory = tmp_path / package
    directory.mkdir()
    (directory / "__init__.py").write_text("", encoding="utf-8")
    (directory / "exports.py").write_text("value = object()\n", encoding="utf-8")
    (directory / "broken.py").write_text(
        "raise ImportError('internal lazy target failure')\n", encoding="utf-8"
    )
    (directory / "dependency.py").write_text(
        "import _lionagi_lazy_contract_missing_dependency\n", encoding="utf-8"
    )
    (directory / "invalid.py").write_text("def broken(:\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        yield package
    finally:
        for name in tuple(sys.modules):
            if name == package or name.startswith(prefix):
                sys.modules.pop(name, None)
        sys.modules.update(previous)
        importlib.invalidate_caches()


@pytest.mark.parametrize("explicit_package", [True, False])
@pytest.mark.parametrize("name,import_name", [("Alias", "value"), ("value", None)])
def test_lazy_export_preserves_target_identity_and_caches_export_name(
    lazy_package, explicit_package, name, import_name
):
    namespace = {"__package__": lazy_package} if explicit_package else {}

    result = lazy_import(
        name, {name: ("exports", import_name)}, lazy_package + ".facade", namespace
    )

    target = importlib.import_module(lazy_package + ".exports")
    assert result is target.value
    assert namespace[name] is target.value
    assert set(namespace) == ({"__package__", name} if explicit_package else {name})


def test_unknown_export_does_not_import_target_or_change_namespace(lazy_package):
    namespace = {"__package__": lazy_package}
    before = dict(namespace)

    with pytest.raises(AttributeError) as caught:
        lazy_import("Unknown", {"value": ("exports", "value")}, lazy_package, namespace)

    assert str(caught.value) == f"module '{lazy_package}' has no attribute 'Unknown'"
    assert namespace == before
    assert lazy_package + ".exports" not in sys.modules


@pytest.mark.parametrize(
    "module,attribute,error_type,missing_name",
    [
        ("absent", "value", ModuleNotFoundError, "absent"),
        ("dependency", "value", ModuleNotFoundError, "dependency"),
        ("broken", "value", ImportError, None),
        ("invalid", "value", SyntaxError, None),
        ("exports", "absent", AttributeError, None),
    ],
)
def test_failed_export_preserves_error_type_and_leaves_no_cached_value(
    lazy_package, module, attribute, error_type, missing_name
):
    namespace = {"__package__": lazy_package, "existing": object()}
    before = dict(namespace)

    with pytest.raises(error_type) as caught:
        lazy_import("Requested", {"Requested": (module, attribute)}, lazy_package, namespace)

    assert type(caught.value) is error_type
    assert namespace == before
    if missing_name == "absent":
        assert caught.value.name == lazy_package + ".absent"
    elif missing_name == "dependency":
        assert caught.value.name == "_lionagi_lazy_contract_missing_dependency"
    elif module == "broken":
        assert str(caught.value) == "internal lazy target failure"
