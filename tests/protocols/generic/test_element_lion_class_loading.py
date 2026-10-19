# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Element.from_dict resolves ``lion_class`` without importing modules outside the
allowed prefixes, and only to Element subclasses."""

from __future__ import annotations

import sys
import types

import pytest

from lionagi import _class_registry
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


@pytest.mark.parametrize("lion_class", ["", ".", "untrusted.", "lionagi.", ".Foo", ".LocalElement"])
def test_a_name_with_an_empty_module_or_class_part_is_refused(lion_class, monkeypatch):
    monkeypatch.setitem(LION_CLASS_REGISTRY, "probe.", LocalElement)
    monkeypatch.setitem(LION_CLASS_REGISTRY, "probe.LocalElement", LocalElement)
    loads = []
    monkeypatch.setattr(_class_registry, "load_type_from_string", loads.append)
    with pytest.raises(ValueError, match="Unable to find class .*register_type_prefix"):
        get_class(lion_class)
    assert loads == []


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
    assert "register_type_prefix" in str(raised.value)
    assert not probe_module.exists()
    assert _MODULE not in sys.modules


def test_a_pile_item_type_already_imported_still_resolves():
    restored = Element.from_dict(_dumped_pile([LocalElement.class_name(full=True)]))
    assert type(restored) is Pile
    assert restored.item_type == {LocalElement}


def test_a_pile_item_type_without_a_module_is_refused():
    with pytest.raises(ValidationError):
        Element.from_dict(_dumped_pile(["Instruction"]))


_OUTSIDE = "lion_class_outside_zz"


@pytest.fixture
def outside_node(tmp_path, monkeypatch):
    """A Node subclass in an imported module outside the allowed prefixes."""
    import importlib

    (tmp_path / f"{_OUTSIDE}.py").write_text(
        "from lionagi.protocols.graph.node import Node\nclass ExternalNode(Node):\n    pass\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(_utils, "_ALLOWED_MODULE_PREFIXES", set(_utils._DEFAULT_ALLOWED_PREFIXES))
    monkeypatch.setattr(_utils, "_TYPE_CACHE", {})
    cls = importlib.import_module(_OUTSIDE).ExternalNode
    assert LION_CLASS_REGISTRY[f"{_OUTSIDE}.ExternalNode"] is cls
    yield cls
    LION_CLASS_REGISTRY.pop(f"{_OUTSIDE}.ExternalNode", None)
    sys.modules.pop(_OUTSIDE, None)


def test_a_lionagi_name_does_not_fall_back_to_a_class_outside_lionagi(outside_node):
    assert get_class(f"{_OUTSIDE}.ExternalNode") is outside_node
    with pytest.raises(ValueError, match="Unable to find class"):
        get_class("lionagi.no_such_module_zz.ExternalNode")
    with pytest.raises(ValueError, match="cannot be loaded"):
        Element.from_dict(_dumped("lionagi.no_such_module_zz.ExternalNode"))


def test_a_registered_prefix_name_falls_back_only_to_a_class_under_that_prefix(outside_node):
    _utils.register_type_prefix(f"{_OUTSIDE}.")
    assert get_class(f"{_OUTSIDE}.moved_zz.ExternalNode") is outside_node
    with pytest.raises(ValueError, match="Unable to find class"):
        get_class(f"{_OUTSIDE}.moved_zz.Instruction")
    with pytest.raises(ValueError, match="Unable to find class"):
        get_class(f"{_OUTSIDE}.moved_zz.Node")


@pytest.mark.parametrize("short", ["Path", "BaseModel", "UUID"])
def test_a_name_does_not_resolve_to_a_type_a_built_in_module_imports(short):
    import importlib

    from lionagi._class_registry import _BUILTIN_MODULES

    held = [getattr(importlib.import_module(m), short, None) for m in _BUILTIN_MODULES]
    assert any(isinstance(t, type) and not t.__module__.startswith("lionagi.") for t in held)
    for name in (short, f"lionagi.no_such_mod_zz.{short}"):
        with pytest.raises(ValueError, match="Unable to find class"):
            get_class(name)
    with pytest.raises(ValidationError, match="register_type_prefix"):
        Element.from_dict(_dumped_pile([f"lionagi.no_such_mod_zz.{short}"]))


class _SlottedProxy:
    __slots__ = ()
    calls: list[str] = []

    def __getattr__(self, name):
        _SlottedProxy.calls.append(name)
        return LocalElement


def test_a_non_module_in_sys_modules_is_skipped_not_read(monkeypatch):
    from lionagi.protocols.messages.instruction import Instruction

    _SlottedProxy.calls = []
    monkeypatch.setitem(sys.modules, "lion_class_nonmod_zz", object())
    monkeypatch.setitem(sys.modules, "lion_class_proxy_zz", _SlottedProxy())
    monkeypatch.setitem(sys.modules, "lionagi.lion_class_removed_zz", object())
    for name in ("lion_class_nonmod_zz.Thing", "lion_class_proxy_zz.Thing"):
        with pytest.raises(ValueError, match="Unable to find class"):
            get_class(name)
    assert _SlottedProxy.calls == []
    assert get_class("lionagi.lion_class_removed_zz.Instruction") is Instruction


def test_a_name_ending_in_a_dot_imports_nothing_under_an_allowed_prefix(probe_module):
    _utils.register_type_prefix(f"{_MODULE}.")
    with pytest.raises(ValueError, match="Unable to find class"):
        get_class(f"{_MODULE}.")
    assert not probe_module.exists()
    assert _MODULE not in sys.modules


@pytest.mark.parametrize(
    "item_type",
    [
        [Element, "lionagi.protocols.generic.element.Element"],
        ["lionagi.protocols.generic.element.Element", "lionagi.no_such_mod_zz.Element"],
    ],
    ids=["type-and-name", "two-names"],
)
def test_one_item_type_given_twice_is_refused_after_resolution(item_type):
    with pytest.raises(ValidationError, match="duplicated item types"):
        Pile(item_type=item_type)


def test_distinct_item_types_given_by_type_and_name_are_kept():
    from lionagi.protocols.graph.node import Node

    pile = Pile(item_type=[Element, "lionagi.protocols.graph.node.Node"])
    assert pile.item_type == {Element, Node}


def test_a_registered_class_falls_back_only_under_the_prefix_it_was_registered_under(monkeypatch):
    spoof = type("SpoofNode", (LocalElement,), {"__module__": "lionagi.spoofed_zz"})
    monkeypatch.setitem(LION_CLASS_REGISTRY, "ext_unallowed_zz.SpoofNode", spoof)
    assert get_class("SpoofNode") is spoof
    with pytest.raises(ValueError, match="Unable to find class"):
        get_class("lionagi.no_such_mod_zz.SpoofNode")


def test_a_union_item_type_counts_as_each_of_its_members():
    from lionagi.protocols.graph.node import Node

    assert Pile(item_type=[Element | Node]).item_type == {Element, Node}
    with pytest.raises(ValidationError, match="duplicated item types"):
        Pile(item_type=[Element, Element | Node])
