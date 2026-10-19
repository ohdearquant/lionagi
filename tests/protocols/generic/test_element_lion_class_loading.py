# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Element.from_dict resolves ``lion_class`` without importing modules outside the
allowed prefixes, and only to Element subclasses."""

from __future__ import annotations

import sys

import pytest

from lionagi.ln import _utils
from lionagi.protocols.generic.element import Element

_MODULE = "lion_class_loading_probe"


class LocalElement(Element):
    note: str = ""


class NotAnElement:
    called = False

    @classmethod
    def from_dict(cls, data):
        cls.called = True
        return cls()


@pytest.fixture
def probe_module(tmp_path, monkeypatch):
    """A module on sys.path, not yet imported, that records being imported."""
    marker = tmp_path / "imported"
    (tmp_path / f"{_MODULE}.py").write_text(
        "from pathlib import Path\n"
        "from lionagi.protocols.generic.element import Element\n"
        f"Path({str(marker)!r}).write_text('yes')\n"
        "class Thing(Element):\n"
        "    pass\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, _MODULE, raising=False)
    monkeypatch.setattr(_utils, "_ALLOWED_MODULE_PREFIXES", set(_utils._DEFAULT_ALLOWED_PREFIXES))
    monkeypatch.setattr(_utils, "_TYPE_CACHE", {})
    yield marker
    sys.modules.pop(_MODULE, None)


def _dumped(lion_class) -> dict:
    data = Element().to_dict(mode="python")
    data["metadata"]["lion_class"] = lion_class
    return data


def test_a_module_outside_the_allowed_prefixes_is_not_imported(probe_module):
    with pytest.raises(ValueError, match="cannot be loaded"):
        Element.from_dict(_dumped(f"{_MODULE}.Thing"))
    assert not probe_module.exists()
    assert _MODULE not in sys.modules


def test_the_same_module_loads_once_its_prefix_is_allowed(probe_module):
    _utils.register_type_prefix(f"{_MODULE}.")
    restored = Element.from_dict(_dumped(f"{_MODULE}.Thing"))
    assert type(restored).__name__ == "Thing"
    assert probe_module.read_text() == "yes"


def test_an_element_subclass_already_imported_still_round_trips():
    original = LocalElement(note="kept")
    restored = Element.from_dict(original.to_dict(mode="python"))
    assert type(restored) is LocalElement
    assert restored.note == "kept"
    assert restored.id == original.id


def test_a_type_outside_the_element_hierarchy_is_refused_before_it_is_called():
    NotAnElement.called = False
    with pytest.raises(ValueError, match="not an Element subclass"):
        Element.from_dict(_dumped(f"{__name__}.NotAnElement"))
    assert NotAnElement.called is False


@pytest.mark.parametrize("lion_class", [7, None, ["a.B"]])
def test_a_lion_class_that_is_not_a_string_is_refused(lion_class):
    with pytest.raises(ValueError, match="must be a string"):
        Element.from_dict(_dumped(lion_class))
