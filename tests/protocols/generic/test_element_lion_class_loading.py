# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Element.from_dict resolves ``lion_class`` without importing modules outside the
allowed prefixes, and only to Element subclasses."""

from __future__ import annotations

import sys
import types

import pytest

from lionagi._class_registry import LION_CLASS_REGISTRY, get_class
from lionagi._errors import ValidationError
from lionagi.ln import _utils
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.pile import Pile

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


@pytest.mark.parametrize(
    "lion_class",
    ["lion_class_unimported_zz.Instruction", "lion_class_unimported_zz.Node", "sys.Element"],
)
def test_a_dotted_name_outside_the_prefixes_does_not_fall_back_to_its_short_name(lion_class):
    with pytest.raises(ValueError, match="Unable to find class"):
        get_class(lion_class)
    with pytest.raises(ValueError, match="cannot be loaded"):
        Element.from_dict(_dumped(lion_class))
    assert "lion_class_unimported_zz" not in sys.modules


def test_a_lionagi_class_whose_module_moved_still_resolves_by_its_short_name():
    from lionagi.protocols.messages.instruction import Instruction

    assert get_class("lionagi.no_such_module_zz.Instruction") is Instruction


@pytest.mark.parametrize("lion_class", [".", "untrusted.", "lionagi."])
def test_a_name_ending_in_a_dot_is_refused(lion_class, monkeypatch):
    monkeypatch.setitem(LION_CLASS_REGISTRY, "probe.", LocalElement)
    with pytest.raises(ValueError, match="Unable to find class"):
        get_class(lion_class)


def test_a_module_level_getattr_outside_the_prefixes_is_not_called(monkeypatch):
    calls = []
    lazy = types.ModuleType("lion_class_lazy_zz")

    def lazy_getattr(name):
        calls.append(name)
        return LocalElement

    lazy.__getattr__ = lazy_getattr
    monkeypatch.setitem(sys.modules, "lion_class_lazy_zz", lazy)
    with pytest.raises(ValueError, match="cannot be loaded"):
        Element.from_dict(_dumped("lion_class_lazy_zz.Thing"))
    assert calls == []


def test_a_module_attribute_that_is_not_a_type_is_refused_and_not_called(monkeypatch):
    calls = []
    module = types.ModuleType("lion_class_attr_zz")
    module.make = lambda *a, **k: calls.append(a) or LocalElement()
    monkeypatch.setitem(sys.modules, "lion_class_attr_zz", module)
    with pytest.raises(ValueError, match="cannot be loaded"):
        Element.from_dict(_dumped("lion_class_attr_zz.make"))
    assert calls == []


def _dumped_pile(item_type: list[str]) -> dict:
    data = Pile().to_dict(mode="python")
    data["item_type"] = item_type
    return data


def test_a_pile_item_type_outside_the_prefixes_is_not_imported(probe_module):
    with pytest.raises(ValidationError) as raised:
        Element.from_dict(_dumped_pile([f"{_MODULE}.Thing"]))
    assert "Unable to find class" in str(raised.value.__cause__)
    assert not probe_module.exists()
    assert _MODULE not in sys.modules


def test_a_pile_item_type_already_imported_still_resolves():
    restored = Element.from_dict(_dumped_pile([LocalElement.class_name(full=True)]))
    assert type(restored) is Pile
    assert restored.item_type == {LocalElement}


def test_a_pile_item_type_without_a_module_is_refused():
    with pytest.raises(ValidationError):
        Element.from_dict(_dumped_pile(["Instruction"]))
