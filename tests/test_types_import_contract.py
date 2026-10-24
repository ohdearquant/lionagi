# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Cold relative imports preserve the compatibility type facade's lookup order."""

from __future__ import annotations

import importlib
import sys
from uuid import uuid4

import pytest

from lionagi import _types


@pytest.fixture
def type_sources(tmp_path, monkeypatch):
    package = f"type_sources_{uuid4().hex}"
    root = tmp_path / package
    root.mkdir()
    (root / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(_types, "__name__", f"{package}.types")
    monkeypatch.setattr(_types, "_lazy_type_cache", {})

    def write(name, source):
        (root / f"{name}.py").write_text(source, encoding="utf-8")
        importlib.invalidate_caches()
        return f"{package}.{name}"

    yield package, write
    for name in tuple(sys.modules):
        if name == package or name.startswith(f"{package}."):
            sys.modules.pop(name, None)


def test_cold_relative_leaf_and_cached_identity(type_sources, monkeypatch):
    package, write = type_sources
    leaf = write("first", "TOKEN = object()\n")
    monkeypatch.setattr(_types, "_SOURCE_MODULES", ("first",))
    assert leaf not in sys.modules

    resolved = _types.__getattr__("TOKEN")

    assert resolved is sys.modules[leaf].TOKEN
    assert _types._lazy_type_cache == {"TOKEN": resolved}
    sys.modules[leaf].TOKEN = object()
    assert _types.__getattr__("TOKEN") is resolved
    assert package in sys.modules


@pytest.mark.parametrize("error_type", ["ImportError", "AttributeError"])
def test_ordered_fallback_preserves_first_success(type_sources, monkeypatch, error_type):
    package, write = type_sources
    write("broken", f"raise {error_type}('unavailable source')\n")
    write("empty", "OTHER = object()\n")
    first = write("first", "TOKEN = object()\n")
    later = write("later", "TOKEN = object()\n")
    monkeypatch.setattr(_types, "_SOURCE_MODULES", ("missing", "broken", "empty", "first", "later"))

    resolved = _types.__getattr__("TOKEN")

    assert resolved is sys.modules[first].TOKEN
    assert later not in sys.modules
    assert f"{package}.broken" not in sys.modules


def test_missing_name_is_not_cached(type_sources, monkeypatch):
    package, write = type_sources
    leaf = write("first", "OTHER = object()\n")
    monkeypatch.setattr(_types, "_SOURCE_MODULES", ("first",))

    with pytest.raises(AttributeError) as caught:
        _types.__getattr__("TOKEN")

    assert str(caught.value) == f"module '{package}.types' has no attribute 'TOKEN'"
    assert "TOKEN" not in _types._lazy_type_cache
    token = object()
    sys.modules[leaf].TOKEN = token
    assert _types.__getattr__("TOKEN") is token
