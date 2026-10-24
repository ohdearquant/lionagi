# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Characterize the historical type facade before changing its import boundary."""

import importlib
import sys

import pytest


@pytest.fixture
def compat_facade(tmp_path, monkeypatch):
    facade = importlib.import_module("lionagi._types")
    package = "_lionagi_types_compat_fixture"
    prefix = package + "."
    previous = {
        name: module
        for name, module in sys.modules.items()
        if name == package or name.startswith(prefix)
    }
    for name in previous:
        monkeypatch.delitem(sys.modules, name)
    directory = tmp_path / package
    for subdirectory in (directory, directory / "operations", directory / "protocols"):
        subdirectory.mkdir(parents=True, exist_ok=True)
        (subdirectory / "__init__.py").write_text("", encoding="utf-8")
    for module in ("operations.fields", "models", "protocols.types"):
        path = directory.joinpath(*module.split(".")).with_suffix(".py")
        path.write_text("Target = object()\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    # Redirect only the relative-import anchor; retain the real resolver and source order.
    monkeypatch.setattr(facade, "__name__", package + ".facade")
    monkeypatch.setattr(facade, "_lazy_type_cache", {})
    try:
        yield facade, package, directory
    finally:
        for name in tuple(sys.modules):
            if name == package or name.startswith(prefix):
                sys.modules.pop(name, None)
        sys.modules.update(previous)
        importlib.invalidate_caches()


@pytest.mark.parametrize("winner", ["operations.fields", "models", "protocols.types"])
def test_type_facade_uses_first_available_source_without_importing_later_sources(
    compat_facade, winner
):
    facade, package, directory = compat_facade
    sources = ("operations.fields", "models", "protocols.types")
    index = sources.index(winner)
    for module in sources[:index]:
        directory.joinpath(*module.split(".")).with_suffix(".py").write_text("", encoding="utf-8")

    result = facade.Target

    assert result is importlib.import_module(package + "." + winner).Target
    assert facade._lazy_type_cache == {"Target": result}
    for module in sources[index + 1 :]:
        assert package + "." + module not in sys.modules


def test_successful_type_lookup_keeps_the_original_cached_object(compat_facade, monkeypatch):
    facade, package, _ = compat_facade
    first = facade.Target
    source = importlib.import_module(package + ".operations.fields")
    replacement = object()
    monkeypatch.setattr(source, "Target", replacement)

    assert facade.Target is first
    assert facade.Target is not replacement
    assert package + ".models" not in sys.modules


def test_missing_type_is_not_cached_and_can_become_available(compat_facade, monkeypatch):
    facade, package, directory = compat_facade
    for module in ("operations.fields", "models", "protocols.types"):
        directory.joinpath(*module.split(".")).with_suffix(".py").write_text("", encoding="utf-8")

    with pytest.raises(AttributeError) as caught:
        _ = facade.Target

    assert str(caught.value) == f"module '{package}.facade' has no attribute 'Target'"
    assert facade._lazy_type_cache == {}
    source = importlib.import_module(package + ".protocols.types")
    late_value = object()
    monkeypatch.setattr(source, "Target", late_value, raising=False)
    assert facade.Target is late_value


@pytest.mark.parametrize(
    "first_source",
    [
        None,
        "",
        "from .._missing_dependency import Target\n",
        "raise ImportError('internal source failure')\n",
        "raise AttributeError('internal source failure')\n",
        "def __getattr__(name):\n    raise ImportError('dynamic attribute failure')\n",
    ],
    ids=[
        "absent-module",
        "absent-export",
        "dependency",
        "import-error",
        "attribute-error",
        "getter",
    ],
)
def test_type_facade_falls_through_import_and_attribute_failures(compat_facade, first_source):
    facade, package, directory = compat_facade
    first_path = directory / "operations" / "fields.py"
    if first_source is None:
        first_path.unlink()
    else:
        first_path.write_text(first_source, encoding="utf-8")

    result = facade.Target

    assert result is importlib.import_module(package + ".models").Target
    assert facade._lazy_type_cache == {"Target": result}
    assert package + ".protocols.types" not in sys.modules


@pytest.mark.parametrize(
    "first_source,error_type",
    [("raise ValueError('invalid source')\n", ValueError), ("def broken(:\n", SyntaxError)],
    ids=["value-error", "syntax-error"],
)
def test_type_facade_preserves_other_failures_without_fallback_or_cache(
    compat_facade, first_source, error_type
):
    facade, package, directory = compat_facade
    (directory / "operations" / "fields.py").write_text(first_source, encoding="utf-8")

    with pytest.raises(error_type) as caught:
        _ = facade.Target

    assert type(caught.value) is error_type
    assert facade._lazy_type_cache == {}
    assert package + ".models" not in sys.modules
    assert package + ".protocols.types" not in sys.modules
