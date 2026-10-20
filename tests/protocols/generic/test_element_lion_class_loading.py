# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Element.from_dict resolves ``lion_class`` importing a module only under an allowed
prefix, and only to Element subclasses."""

from __future__ import annotations

import sys
import types
from typing import Annotated, Any, ForwardRef, Literal, Optional, Union

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


def test_a_module_under_an_allowed_prefix_that_fails_to_import_says_why(tmp_path, monkeypatch):
    broken = "lion_class_broken_probe"
    (tmp_path / f"{broken}.py").write_text("raise ImportError('needs an optional dependency')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, broken, raising=False)
    monkeypatch.setattr(_utils, "_ALLOWED_MODULE_PREFIXES", set(_utils._DEFAULT_ALLOWED_PREFIXES))
    monkeypatch.setattr(_utils, "_TYPE_CACHE", {})
    _utils.register_type_prefix(f"{broken}.")
    try:
        with pytest.raises(
            ValueError,
            match="cannot be loaded: .*loading it failed: .*needs an optional dependency",
        ) as raised:
            Element.from_dict(_dumped(f"{broken}.Thing"))
    finally:
        sys.modules.pop(broken, None)
    # Chained from get_class's own error, which holds the remedy and the reason.
    cause = raised.value.__cause__
    assert isinstance(cause, ValueError)
    assert str(raised.value).endswith(str(cause))


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
    with pytest.raises(ValueError, match="Unable to find class") as raised:
        get_class(lion_class)
    # Chained from the refused load, so the reason survives beside the remedy.
    assert "not in allowed prefixes" in str(raised.value.__cause__)
    with pytest.raises(ValueError, match="cannot be loaded"):
        Element.from_dict(_dumped(lion_class))
    assert "lion_class_unimported_zz" not in sys.modules


def test_a_built_in_lionagi_class_whose_module_moved_still_resolves_by_its_short_name():
    from lionagi.protocols.messages.instruction import Instruction

    assert get_class("lionagi.no_such_module_zz.Instruction") is Instruction


@pytest.mark.parametrize(
    "lion_class",
    [
        "",
        ".",
        "untrusted.",
        "lionagi.",
        ".Foo",
        ".LocalElement",
        "lionagi..Element",
        "probe..LocalElement",
    ],
)
def test_a_name_with_an_empty_module_or_class_part_is_refused(lion_class, monkeypatch):
    monkeypatch.setitem(LION_CLASS_REGISTRY, "probe.", LocalElement)
    monkeypatch.setitem(LION_CLASS_REGISTRY, "probe.LocalElement", LocalElement)
    loads = []
    monkeypatch.setattr(_class_registry, "load_type_from_string", loads.append)
    with pytest.raises(ValueError, match="Unable to find class .*not a dotted class path"):
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
    with pytest.raises(ValidationError, match="no class of that name loads") as raised:
        Element.from_dict(_dumped_pile([f"lionagi.no_such_mod_zz.{short}"]))
    assert "register_type_prefix" not in str(raised.value)


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


def test_a_registered_class_falls_back_by_the_module_it_is_defined_in(monkeypatch):
    # A registry key records the module a class had when it was registered; the
    # fallback follows the module the class is defined in now.
    foreign = type("ForeignKeyedElem", (LocalElement,), {"__module__": "foreign_pkg_zz"})
    monkeypatch.setitem(LION_CLASS_REGISTRY, "lionagi.old_mod_zz.ForeignKeyedElem", foreign)
    assert get_class("ForeignKeyedElem") is foreign
    with pytest.raises(ValueError, match="Unable to find class"):
        get_class("lionagi.moved_mod_zz.ForeignKeyedElem")

    moved = type("RekeyedElem", (LocalElement,), {"__module__": "lionagi.custom_mod_zz"})
    monkeypatch.setitem(LION_CLASS_REGISTRY, "foreign_pkg_zz.RekeyedElem", moved)
    assert get_class("lionagi.moved_mod_zz.RekeyedElem") is moved
    assert type(Element.from_dict(_dumped("lionagi.custom_mod_zz.RekeyedElem"))) is moved


def test_a_class_registered_under_its_bare_name_falls_back_by_its_module(monkeypatch):
    bare = type("BareKeyedElem", (LocalElement,), {"__module__": "lionagi.custom_mod_zz"})
    monkeypatch.setitem(LION_CLASS_REGISTRY, "BareKeyedElem", bare)
    assert get_class("lionagi.moved_mod_zz.BareKeyedElem") is bare
    elsewhere = type("BareKeyedOther", (LocalElement,), {"__module__": "foreign_pkg_zz"})
    monkeypatch.setitem(LION_CLASS_REGISTRY, "BareKeyedOther", elsewhere)
    with pytest.raises(ValueError, match="Unable to find class"):
        get_class("lionagi.moved_mod_zz.BareKeyedOther")


class _UnreadableModule(type):
    @property
    def __module__(cls):
        raise AttributeError("__module__")


def test_a_class_whose_module_cannot_be_read_does_not_fall_back(monkeypatch):
    unreadable = _UnreadableModule("UnreadableModuleZz", (), {})
    monkeypatch.setitem(LION_CLASS_REGISTRY, "lionagi.old_mod_zz.UnreadableModuleZz", unreadable)
    assert get_class("UnreadableModuleZz") is unreadable
    with pytest.raises(ValueError, match="Unable to find class"):
        get_class("lionagi.moved_mod_zz.UnreadableModuleZz")

    import importlib

    builtin = importlib.import_module(_class_registry._BUILTIN_MODULES[0])
    unlisted = _UnreadableModule("UnlistedModuleZz", (), {})
    monkeypatch.setattr(builtin, "UnlistedModuleZz", unlisted, raising=False)
    with pytest.raises(ValueError, match="Unable to find class"):
        get_class("UnlistedModuleZz")


def test_a_union_item_type_counts_as_each_of_its_members():
    from lionagi.protocols.graph.node import Node

    assert Pile(item_type=[Element | Node]).item_type == {Element, Node}
    with pytest.raises(ValidationError, match="duplicated item types"):
        Pile(item_type=[Element, Element | Node])


def test_a_union_nested_in_a_union_counts_as_each_of_its_members():
    from lionagi.protocols.generic.log import Log
    from lionagi.protocols.graph.node import Node

    nested = Annotated[Node | Log, "meta"] | LocalElement
    assert Pile(item_type=[nested]).item_type == {Node, Log, LocalElement}
    with pytest.raises(ValidationError, match="duplicated item types"):
        Pile(item_type=[Node, nested])
    pile = Pile(collections=[LocalElement()], item_type=[nested], strict_type=True)
    assert len(pile) == 1


@pytest.mark.parametrize(
    "item_type",
    [
        Union[ForwardRef("lionagi.protocols.graph.node.Node"), LocalElement],
        Union[LocalElement, Literal["x"]],
        LocalElement | list[Element],
        list[Element],
        LocalElement | None,
        Optional[LocalElement],
        Any,
    ],
    ids=["forward-refs", "literal", "generic-member", "generic", "none", "optional", "any"],
)
def test_an_item_type_that_is_not_a_class_is_refused(item_type):
    with pytest.raises(ValidationError, match="item_type cannot hold ") as exc:
        Pile(item_type=[item_type])
    assert exc.value.details.get("expected") == "A type."


def test_a_bare_item_type_name_is_refused_with_the_dotted_form_to_use():
    with pytest.raises(
        ValidationError, match="names no module: pass the class itself or its dotted"
    ):
        Pile(item_type=["Element"])


@pytest.mark.parametrize(
    "item_type", ["lionagi.protocols.generic.element.Element", "lionagi.protocols.graph.node.Node"]
)
def test_a_single_dotted_name_as_item_type_is_resolved(item_type):
    assert Pile(item_type=item_type).item_type == {get_class(item_type)}


def test_a_single_bare_name_as_item_type_is_refused():
    with pytest.raises(ValidationError, match="names no module"):
        Pile(item_type="Element")


def test_a_single_name_as_item_type_outside_the_prefixes_is_refused(probe_module):
    with pytest.raises(ValidationError, match="register_type_prefix"):
        Pile(item_type=f"{_MODULE}.Thing")
    assert not probe_module.exists()


@pytest.mark.parametrize(
    ("name", "says"),
    [
        ("", "not a dotted class path or a class name"),
        ("lionagi.", "not a dotted class path or a class name"),
        ("NoSuchClassZz", "give its dotted path"),
        ("lionagi.no_such_mod_zz.Thing", "no class of that name loads from that module path"),
        ("json.NoSuchClassZz", "its module is imported and holds no class of that name"),
        (f"{_MODULE}.Thing", "allow its package with lionagi.ln.register_type_prefix()"),
    ],
)
def test_the_not_found_remedy_fits_the_shape_of_the_name(name, says, probe_module):
    with pytest.raises(ValueError) as raised:
        get_class(name)
    assert says in str(raised.value)
    if not says.startswith("allow"):
        assert "register_type_prefix" not in str(raised.value)
    assert not probe_module.exists()


def test_typing_any_as_an_item_type_is_refused_by_name():
    # A class from Python 3.11 on, which isinstance() refuses, so it could admit nothing.
    with pytest.raises(ValidationError, match=r"item_type cannot hold typing\.Any"):
        Pile(item_type=Any)


def test_a_name_under_a_subpackage_prefix_imports_the_packages_above_it(tmp_path, monkeypatch):
    top = "lion_class_parent_zz"
    (tmp_path / top / "sub").mkdir(parents=True)
    (tmp_path / top / "__init__.py").write_text("")
    (tmp_path / top / "sub" / "__init__.py").write_text("")
    (tmp_path / top / "sub" / "thing.py").write_text(
        "from lionagi.protocols.generic.element import Element\nclass Thing(Element):\n    pass\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(_utils, "_ALLOWED_MODULE_PREFIXES", set(_utils._DEFAULT_ALLOWED_PREFIXES))
    monkeypatch.setattr(_utils, "_TYPE_CACHE", {})
    _utils.register_type_prefix(f"{top}.sub.")
    loaded = [top, f"{top}.sub", f"{top}.sub.thing"]
    try:
        assert not any(name in sys.modules for name in loaded)
        assert get_class(f"{top}.sub.thing.Thing").__name__ == "Thing"
        # Python imports a module's parent packages first, the one above the prefix too.
        assert all(name in sys.modules for name in loaded)
    finally:
        for name in loaded:
            sys.modules.pop(name, None)


def test_the_remedy_fits_a_nested_class_a_module_getattr_and_a_moved_class(monkeypatch):
    class Outer:
        class Inner(LocalElement):
            pass

    def supply(name):
        return LocalElement

    nest = types.ModuleType("lion_class_nest_zz")
    nest.Outer = Outer
    lazy = types.ModuleType("lion_class_getattr_zz")
    lazy.__getattr__ = supply
    monkeypatch.setitem(sys.modules, "lion_class_nest_zz", nest)
    monkeypatch.setitem(sys.modules, "lion_class_getattr_zz", lazy)
    monkeypatch.setattr(_utils, "_ALLOWED_MODULE_PREFIXES", set(_utils._DEFAULT_ALLOWED_PREFIXES))
    monkeypatch.setattr(_utils, "_TYPE_CACHE", {})
    remedies = {
        "lion_class_nest_zz.Outer.Inner": "add a nested class to LION_CLASS_REGISTRY",
        "lion_class_getattr_zz.Thing": "module-level __getattr__",
        "lionagi.no_such_mod_zz.Thing": "give its current dotted path",
        "lion_class_unimported_zz.old.Thing": "give its current dotted path",
    }
    for name, remedy in remedies.items():
        with pytest.raises(ValueError) as raised:
            get_class(name)
        assert remedy in str(raised.value), name

    # Each remedy works: a prefix does not reach the nested class, a registry entry does.
    _utils.register_type_prefix("lion_class_nest_zz.")
    with pytest.raises(ValueError, match="add a nested class to LION_CLASS_REGISTRY"):
        get_class("lion_class_nest_zz.Outer.Inner")
    monkeypatch.setitem(LION_CLASS_REGISTRY, "lion_class_nest_zz.Outer.Inner", Outer.Inner)
    assert get_class("lion_class_nest_zz.Outer.Inner") is Outer.Inner
    _utils.register_type_prefix("lion_class_getattr_zz.")
    assert get_class("lion_class_getattr_zz.Thing") is LocalElement


def test_a_long_name_is_cut_short_in_each_error():
    long_name = "lionagi." + "x" * 5000 + ".Thing"
    with pytest.raises(ValueError) as raised:
        Element.from_dict(_dumped(long_name))
    assert len(str(raised.value)) < 1000
    assert "more characters]" in str(raised.value)
    for item_type in (long_name, "x" * 5000):
        with pytest.raises(ValidationError) as refused:
            Pile(item_type=item_type)
        assert len(str(refused.value)) < 1000
