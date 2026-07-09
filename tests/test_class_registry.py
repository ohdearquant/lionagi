# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for lionagi/_class_registry.py: LION_CLASS_REGISTRY, get_class,
and lion_class round-trips (both fully-qualified and legacy short names)."""

import pytest

# ---------------------------------------------------------------------------
# Module-level Node subclasses
# These classes MUST live here (module scope) so that their __module__ and
# __qualname__ produce a valid importable fully-qualified name that
# Element.from_dict can resolve via LION_CLASS_REGISTRY or import_module.
# ---------------------------------------------------------------------------
# Imported at module level; the import triggers no registration by itself
# (Node.__pydantic_init_subclass__ fires only for *subclasses* of Node).
from lionagi.protocols.graph.node import Node


class _RegistryTestNode(Node):
    """Used to verify single-subclass registration."""


class _FQNameTestNode(Node):
    """Used to verify full-qualified-name registration key."""


class _MultiA(Node):
    """Used in multi-subclass registration test."""


class _MultiB(Node):
    """Used in multi-subclass registration test."""


class _GetClassHitNode(Node):
    """Used to verify get_class hit path."""


class _TypeCheckNode(Node):
    """Used to verify get_class returns type."""


class _InstantiateNode(Node):
    """Used to verify instantiability of a get_class result."""


class _RoundTripNode(Node):
    """Used for polymorphic python-mode round-trip."""


class _ContentNode(Node):
    """Used to verify content preservation in round-trip."""


class _IdNode(Node):
    """Used to verify id preservation in round-trip."""


class _TsNode(Node):
    """Used to verify created_at preservation in round-trip."""


class _DbKeyNode(Node):
    """Used to verify db-mode produces node_metadata key."""


class _DbRoundTripNode(Node):
    """Used for db-mode round-trip type restoration."""


class _DbContentNode(Node):
    """Used to verify content preservation in db round-trip."""


class _LenBefore(Node):
    """Used for registry-length-stable test."""


# ---------------------------------------------------------------------------
# Autouse fixtures: registry isolation
#
# Two layers, because pollution happens at two times:
#
# 1. Import time — the module-level Node subclasses above are registered by
#    Node.__pydantic_init_subclass__ the moment this file is imported, BEFORE
#    any fixture runs.  A per-test snapshot can't see a pre-import state, so a
#    module-scoped finalizer removes every registry entry whose class was
#    defined in this module once the module's tests finish.  Without it, all
#    test-only keys leak to later test modules on the same xdist worker.
#
# 2. Test time — individual tests mutate the registry (deleting keys,
#    registering type()-created collisions).  A per-test snapshot/restore
#    guarantees those mutations never outlive the test, even on failure.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True, scope="module")
def _purge_module_test_classes():
    """Remove this module's import-time Node subclasses from the registry
    after the last test in this file runs."""
    from lionagi._class_registry import LION_CLASS_REGISTRY

    try:
        yield
    finally:
        stale = [
            k for k, v in LION_CLASS_REGISTRY.items() if getattr(v, "__module__", None) == __name__
        ]
        for k in stale:
            del LION_CLASS_REGISTRY[k]


@pytest.fixture(autouse=True)
def _registry_snapshot():
    """Snapshot the registry before each test; restore on teardown.

    Uses an explicit try/finally so that restoration is guaranteed even if
    the test body raises.  This prevents registry pollution across tests on
    the same xdist worker.
    """
    from lionagi._class_registry import LION_CLASS_REGISTRY

    registry_snapshot = LION_CLASS_REGISTRY.copy()
    try:
        yield
    finally:
        LION_CLASS_REGISTRY.clear()
        LION_CLASS_REGISTRY.update(registry_snapshot)


# ---------------------------------------------------------------------------
# 1. LION_CLASS_REGISTRY population via Node.__pydantic_init_subclass__
# ---------------------------------------------------------------------------


class TestNodeSubclassRegistration:
    """LION_CLASS_REGISTRY is populated when a Node subclass is defined."""

    def test_registry_is_a_dict(self):
        from lionagi._class_registry import LION_CLASS_REGISTRY

        assert isinstance(LION_CLASS_REGISTRY, dict)

    def test_subclass_registered_on_definition(self):
        from lionagi._class_registry import LION_CLASS_REGISTRY

        full_name = _RegistryTestNode.class_name(full=True)
        assert full_name in LION_CLASS_REGISTRY
        assert LION_CLASS_REGISTRY[full_name] is _RegistryTestNode

    def test_registration_key_is_full_qualified_name(self):
        from lionagi._class_registry import LION_CLASS_REGISTRY

        key = _FQNameTestNode.class_name(full=True)
        # Full-qualified name contains at least one '.' (module.ClassName)
        assert "." in key
        assert key.endswith("_FQNameTestNode")
        assert LION_CLASS_REGISTRY[key] is _FQNameTestNode

    def test_multiple_subclasses_registered_independently(self):
        from lionagi._class_registry import LION_CLASS_REGISTRY

        assert LION_CLASS_REGISTRY[_MultiA.class_name(full=True)] is _MultiA
        assert LION_CLASS_REGISTRY[_MultiB.class_name(full=True)] is _MultiB

    def test_node_base_class_itself_not_in_registry(self):
        """Node.__pydantic_init_subclass__ is only called for *subclasses*,
        not for Node itself.  Node's own key must NOT be auto-inserted."""
        from lionagi._class_registry import LION_CLASS_REGISTRY

        node_full = Node.class_name(full=True)
        assert node_full not in LION_CLASS_REGISTRY


# ---------------------------------------------------------------------------
# 2. get_class: hit (direct registry lookup)
# ---------------------------------------------------------------------------


class TestGetClassHit:
    """get_class returns the class when key is already in LION_CLASS_REGISTRY."""

    def test_get_class_returns_correct_type(self):
        from lionagi._class_registry import get_class

        key = _GetClassHitNode.class_name(full=True)
        result = get_class(key)
        assert result is _GetClassHitNode

    def test_get_class_returns_type_object(self):
        from lionagi._class_registry import get_class

        key = _TypeCheckNode.class_name(full=True)
        result = get_class(key)
        assert isinstance(result, type)

    def test_get_class_instantiable(self):
        from lionagi._class_registry import get_class

        key = _InstantiateNode.class_name(full=True)
        cls = get_class(key)
        inst = cls(content="test")
        assert isinstance(inst, _InstantiateNode)


# ---------------------------------------------------------------------------
# 3. get_class: miss behavior (unknown name)
# ---------------------------------------------------------------------------


class TestGetClassMiss:
    """get_class raises ValueError for unknown class names."""

    def test_unknown_name_raises_value_error(self):
        from lionagi._class_registry import get_class

        with pytest.raises(ValueError, match="Unable to find class"):
            get_class("CompletelyNonexistentClass_xyz_abc_12345")

    def test_error_message_contains_class_name(self):
        from lionagi._class_registry import get_class

        class_name = "ThisClassDefinitelyDoesNotExist_99"
        with pytest.raises(ValueError) as exc_info:
            get_class(class_name)
        assert class_name in str(exc_info.value)

    def test_empty_string_raises_value_error(self):
        from lionagi._class_registry import get_class

        with pytest.raises(ValueError):
            get_class("")

    def test_unknown_dotted_path_raises_value_error(self):
        from lionagi._class_registry import get_class

        with pytest.raises(ValueError, match="Unable to find class"):
            get_class("lionagi.protocols.generic.element.NoSuchClass_xyz")


# ---------------------------------------------------------------------------
# 4. get_class: legacy short-name resolution via built-in modules
#
# Persisted `lion_class` metadata predating the full-qualified-name
# convention stores a bare class name (e.g. "Instruction") instead of a
# dotted path. get_class() must still resolve these without any filesystem
# scan, by importing the fixed set of built-in modules and looking the name
# up as a module attribute (or, for Node subclasses, via LION_CLASS_REGISTRY
# after that import triggers registration).
# ---------------------------------------------------------------------------


class TestGetClassShortNameFallback:
    """get_class resolves legacy short (unqualified) class names."""

    @pytest.mark.parametrize(
        "class_name",
        [
            "Element",
            "Node",
            "Graph",
            "Edge",
            "Pile",
            "Progression",
            "Message",
            "Instruction",
            "System",
            "ActionRequest",
            "ActionResponse",
            "AssistantResponse",
            "Event",
            "Flow",
            "Log",
        ],
    )
    def test_short_name_resolves_to_correct_class(self, class_name):
        from lionagi._class_registry import LION_CLASS_REGISTRY, get_class

        # Strip any registry entries matching this short name so resolution
        # is forced through the built-in-module fallback, not a lucky
        # pre-existing full-qualified registry hit.
        keys_to_remove = [
            k for k in LION_CLASS_REGISTRY if k == class_name or k.endswith(f".{class_name}")
        ]
        for k in keys_to_remove:
            del LION_CLASS_REGISTRY[k]

        result = get_class(class_name)
        assert isinstance(result, type)
        assert result.__name__ == class_name

    def test_short_name_returns_same_object_as_direct_import(self):
        from lionagi._class_registry import get_class
        from lionagi.protocols.messages.instruction import Instruction

        result = get_class("Instruction")
        assert result is Instruction

    def test_short_name_for_non_node_element_subclass(self):
        """Event/Flow/Log/Pile/Progression/Edge/Graph are Element subclasses
        that are NOT Node subclasses, so they never self-register into
        LION_CLASS_REGISTRY. Short-name resolution must still work via the
        built-in-module attribute lookup."""
        from lionagi._class_registry import get_class
        from lionagi.protocols.generic.log import Log

        result = get_class("Log")
        assert result is Log


# ---------------------------------------------------------------------------
# 5. get_class: dotted-path import fallback (fully-qualified names)
# ---------------------------------------------------------------------------


class TestGetClassDottedPathFallback:
    """get_class imports an arbitrary dotted "module.Class" path when the
    fully-qualified name is not (yet) present in LION_CLASS_REGISTRY."""

    def test_dotted_path_resolves_via_import(self):
        from lionagi._class_registry import LION_CLASS_REGISTRY, get_class
        from lionagi.protocols.generic.element import Element

        target = "lionagi.protocols.generic.element.Element"
        LION_CLASS_REGISTRY.pop(target, None)

        result = get_class(target)
        assert result is Element

    def test_dotted_path_for_node_subclass(self):
        from lionagi._class_registry import LION_CLASS_REGISTRY, get_class
        from lionagi.protocols.messages.system import System

        target = System.class_name(full=True)
        LION_CLASS_REGISTRY.pop(target, None)

        result = get_class(target)
        assert result is System


# ---------------------------------------------------------------------------
# 6. Duplicate-name handling: last writer wins (overwrite semantics — pinned)
#
# Tests exercise the real registration hook (Node.__pydantic_init_subclass__)
# by creating two classes with the same __name__ using type() in function
# scope.  Because pydantic's __pydantic_init_subclass__ fires on each class
# statement / type() call, both classes are registered under the same key
# (their full-qualified name derives from __module__ + __qualname__).
# The second registration silently overwrites the first — last-writer-wins.
#
# NOTE: classes created with bare type() get __module__ == "abc" (pydantic's
# ModelMetaclass construction runs through abc machinery, and the frame-based
# module detection lands there), producing keys like "abc._DupCollisionNode".
# Two distinct type() calls sharing the same __name__ still collide on exactly
# the same registry key — exactly the scenario we want.  The per-test snapshot
# fixture restores these keys afterwards.
# ---------------------------------------------------------------------------


class TestDuplicateNameHandling:
    """Registry uses plain dict assignment: last writer wins.

    Collision is created via actual subclass creation (the real registration
    hook), not by direct dict mutation.  This tests pins the overwrite
    semantics so that any future change (e.g., raising on collision) is
    caught explicitly.
    """

    def test_real_hook_last_writer_wins(self):
        """Two Node subclasses with identical __name__ collide in the registry;
        the second class definition overwrites the first via the real hook."""
        from lionagi._class_registry import LION_CLASS_REGISTRY

        # Create first class through the real hook.
        CollisionClass_v1 = type("_DupCollisionNode", (Node,), {})  # noqa: N806
        key_v1 = CollisionClass_v1.class_name(full=True)
        assert LION_CLASS_REGISTRY[key_v1] is CollisionClass_v1

        # Create a second class with the SAME __name__ via type().
        # __pydantic_init_subclass__ fires again, overwriting the key.
        CollisionClass_v2 = type("_DupCollisionNode", (Node,), {})  # noqa: N806
        key_v2 = CollisionClass_v2.class_name(full=True)

        # Same key (same module + same __name__).
        assert key_v1 == key_v2

        # Last writer wins: registry now holds v2.
        assert LION_CLASS_REGISTRY[key_v2] is CollisionClass_v2
        assert LION_CLASS_REGISTRY[key_v2] is not CollisionClass_v1

    def test_real_hook_overwrite_does_not_grow_registry(self):
        """Re-registering under an existing key must not increase registry size."""
        from lionagi._class_registry import LION_CLASS_REGISTRY

        # First registration.
        StableClass_v1 = type("_SizePinNode", (Node,), {})  # noqa: N806
        size_after_first = len(LION_CLASS_REGISTRY)

        # Second class with same name: re-registers under the same key.
        _StableClass_v2 = type("_SizePinNode", (Node,), {})  # noqa: N806
        size_after_second = len(LION_CLASS_REGISTRY)

        assert size_after_second == size_after_first

    def test_get_class_returns_most_recently_registered(self):
        """get_class() must return whichever class was registered last."""
        from lionagi._class_registry import get_class

        # Two sequential definitions with the same name.
        type("_GetClassLastWriterNode", (Node,), {})
        LastClass = type("_GetClassLastWriterNode", (Node,), {})  # noqa: N806

        key = LastClass.class_name(full=True)
        result = get_class(key)
        assert result is LastClass

    def test_overwrite_does_not_grow_registry(self):
        """Overwriting a key via direct assignment must not increase registry length
        (kept to pin dict-level contract alongside the real-hook tests)."""
        from lionagi._class_registry import LION_CLASS_REGISTRY

        key = _LenBefore.class_name(full=True)
        assert key in LION_CLASS_REGISTRY
        size_before = len(LION_CLASS_REGISTRY)

        # Re-assign same value: no new key.
        LION_CLASS_REGISTRY[key] = _LenBefore

        assert len(LION_CLASS_REGISTRY) == size_before


# ---------------------------------------------------------------------------
# 7. Polymorphic round-trip (python mode)
# ---------------------------------------------------------------------------


class TestPolymorphicRoundTrip:
    """Serialize a Node subclass and deserialize via Element.from_dict.

    The lion_class key stored in metadata drives class resolution.
    """

    def test_round_trip_restores_original_type(self):
        from lionagi.protocols.generic.element import Element

        inst = _RoundTripNode(content="payload")
        d = inst.to_dict()
        restored = Element.from_dict(d)
        assert type(restored) is _RoundTripNode

    def test_round_trip_preserves_content(self):
        from lionagi.protocols.generic.element import Element

        inst = _ContentNode(content={"key": "value", "num": 42})
        d = inst.to_dict()
        restored = Element.from_dict(d)
        assert restored.content == {"key": "value", "num": 42}

    def test_round_trip_preserves_id(self):
        from lionagi.protocols.generic.element import Element

        inst = _IdNode(content="id-test")
        d = inst.to_dict()
        restored = Element.from_dict(d)
        assert restored.id == inst.id

    def test_round_trip_preserves_created_at(self):
        from lionagi.protocols.generic.element import Element

        inst = _TsNode(content="ts-test")
        d = inst.to_dict()
        restored = Element.from_dict(d)
        assert restored.created_at == inst.created_at

    def test_element_base_class_round_trip(self):
        """Element itself (no subclass) round-trips correctly."""
        from lionagi.protocols.generic.element import Element

        inst = Element(metadata={"note": "base"})
        d = inst.to_dict()
        restored = Element.from_dict(d)
        assert type(restored) is Element
        assert restored.id == inst.id

    def test_metadata_lion_class_key_present_after_serialization(self):
        """to_dict must embed lion_class in metadata."""
        inst = _RoundTripNode(content="meta-check")
        d = inst.to_dict()
        assert "lion_class" in d["metadata"]
        assert _RoundTripNode.class_name(full=True) == d["metadata"]["lion_class"]

    def test_round_trip_via_json(self):
        """to_json / from_json preserves subclass type."""
        from lionagi.protocols.generic.element import Element

        inst = _RoundTripNode(content="json-test")
        json_str = inst.to_json()
        restored = Element.from_json(json_str)
        assert type(restored) is _RoundTripNode
        assert restored.id == inst.id


# ---------------------------------------------------------------------------
# 8. db-mode round-trip (node_metadata key)
# ---------------------------------------------------------------------------


class TestDbModeRoundTrip:
    """to_dict(mode='db') stores metadata under 'node_metadata'; from_dict
    must handle that key for correct polymorphic dispatch."""

    def test_db_mode_produces_node_metadata_key(self):
        inst = _DbKeyNode(content="db-key")
        d = inst.to_dict(mode="db")
        assert "node_metadata" in d
        assert "metadata" not in d

    def test_db_mode_embeds_lion_class(self):
        inst = _DbKeyNode(content="db-meta")
        d = inst.to_dict(mode="db")
        assert "lion_class" in d["node_metadata"]

    def test_db_mode_round_trip_restores_type(self):
        from lionagi.protocols.generic.element import Element

        inst = _DbRoundTripNode(content="db-round-trip")
        d = inst.to_dict(mode="db")
        restored = Element.from_dict(d)
        assert type(restored) is _DbRoundTripNode

    def test_db_mode_round_trip_preserves_content(self):
        from lionagi.protocols.generic.element import Element

        inst = _DbContentNode(content="db-content-check")
        d = inst.to_dict(mode="db")
        restored = Element.from_dict(d)
        assert restored.content == "db-content-check"


# ---------------------------------------------------------------------------
# 9. Legacy short-name lion_class round-trip for built-in message classes
#
# HARD CONSTRAINT: old persisted `lion_class` strings must keep round-
# tripping whether they store the fully-qualified dotted path
# (e.g. "lionagi.protocols.messages.instruction.Instruction") or the bare
# legacy short name (e.g. "Instruction"), for every built-in Element/Node
# subclass that ships with lionagi.
# ---------------------------------------------------------------------------


def _short_name_round_trip(inst):
    """Serialize inst, rewrite its metadata.lion_class to the bare class
    name, and confirm Element.from_dict still restores the correct type."""
    from lionagi.protocols.generic.element import Element

    d = inst.to_dict()
    d["metadata"]["lion_class"] = type(inst).__name__
    restored = Element.from_dict(d)
    assert type(restored) is type(inst)
    assert restored.id == inst.id
    return restored


class TestLegacyShortNameMessageRoundTrip:
    """Both full-qualified and legacy short-name lion_class values round-trip
    for every representative built-in message/Node class."""

    def test_system_full_qualified(self):
        from lionagi.protocols.generic.element import Element
        from lionagi.protocols.messages.system import System

        inst = System(content={"system_message": "be helpful"})
        d = inst.to_dict()
        assert d["metadata"]["lion_class"] == System.class_name(full=True)
        restored = Element.from_dict(d)
        assert type(restored) is System

    def test_system_short_name(self):
        from lionagi.protocols.messages.system import System

        inst = System(content={"system_message": "be helpful"})
        restored = _short_name_round_trip(inst)
        assert restored.content.system_message == "be helpful"

    def test_instruction_full_qualified(self):
        from lionagi.protocols.generic.element import Element
        from lionagi.protocols.messages.instruction import Instruction

        inst = Instruction(
            content={"instruction": "do the thing"}, sender="user", recipient="assistant"
        )
        d = inst.to_dict()
        assert d["metadata"]["lion_class"] == Instruction.class_name(full=True)
        restored = Element.from_dict(d)
        assert type(restored) is Instruction

    def test_instruction_short_name(self):
        from lionagi.protocols.messages.instruction import Instruction

        inst = Instruction(
            content={"instruction": "do the thing"}, sender="user", recipient="assistant"
        )
        restored = _short_name_round_trip(inst)
        assert restored.sender == inst.sender

    def test_assistant_response_full_qualified(self):
        from lionagi.protocols.generic.element import Element
        from lionagi.protocols.messages.assistant_response import AssistantResponse

        inst = AssistantResponse(content={"assistant_response": "hello"})
        d = inst.to_dict()
        assert d["metadata"]["lion_class"] == AssistantResponse.class_name(full=True)
        restored = Element.from_dict(d)
        assert type(restored) is AssistantResponse

    def test_assistant_response_short_name(self):
        from lionagi.protocols.messages.assistant_response import AssistantResponse

        inst = AssistantResponse(content={"assistant_response": "hello"})
        restored = _short_name_round_trip(inst)
        assert restored.response == inst.response

    def test_action_request_full_qualified(self):
        from lionagi.protocols.generic.element import Element
        from lionagi.protocols.messages.action_request import (
            ActionRequest,
            ActionRequestContent,
        )

        content = ActionRequestContent(function="do_it", arguments={"x": 1})
        inst = ActionRequest(content=content, sender="user", recipient="assistant")
        d = inst.to_dict()
        assert d["metadata"]["lion_class"] == ActionRequest.class_name(full=True)
        restored = Element.from_dict(d)
        assert type(restored) is ActionRequest

    def test_action_request_short_name(self):
        from lionagi.protocols.messages.action_request import (
            ActionRequest,
            ActionRequestContent,
        )

        content = ActionRequestContent(function="do_it", arguments={"x": 1})
        inst = ActionRequest(content=content, sender="user", recipient="assistant")
        restored = _short_name_round_trip(inst)
        assert restored.function == "do_it"

    def test_action_response_full_qualified(self):
        from lionagi.protocols.generic.element import Element
        from lionagi.protocols.messages.action_response import (
            ActionResponse,
            ActionResponseContent,
        )

        content = ActionResponseContent(function="do_it", arguments={"x": 1}, output={"ok": True})
        inst = ActionResponse(content=content, sender="assistant", recipient="user")
        d = inst.to_dict()
        assert d["metadata"]["lion_class"] == ActionResponse.class_name(full=True)
        restored = Element.from_dict(d)
        assert type(restored) is ActionResponse

    def test_action_response_short_name(self):
        from lionagi.protocols.messages.action_response import (
            ActionResponse,
            ActionResponseContent,
        )

        content = ActionResponseContent(function="do_it", arguments={"x": 1}, output={"ok": True})
        inst = ActionResponse(content=content, sender="assistant", recipient="user")
        restored = _short_name_round_trip(inst)
        assert restored.output == {"ok": True}

    def test_node_full_qualified(self):
        from lionagi.protocols.generic.element import Element
        from lionagi.protocols.graph.node import Node

        inst = Node(content="plain node")
        d = inst.to_dict()
        assert d["metadata"]["lion_class"] == Node.class_name(full=True)
        restored = Element.from_dict(d)
        assert type(restored) is Node

    def test_node_short_name(self):
        from lionagi.protocols.graph.node import Node

        inst = Node(content="plain node")
        restored = _short_name_round_trip(inst)
        assert restored.content == "plain node"


# ---------------------------------------------------------------------------
# 10. Message Pile round-trip with mixed full/short lion_class entries
#
# Branch/session state persists its message history as a Pile of messages;
# Pile.from_dict / _validate_collections deserializes each item via
# Element.from_dict. This exercises the whole stack (Pile -> Element.from_dict
# -> get_class) the way a real Branch snapshot load would.
# ---------------------------------------------------------------------------


class TestMessagePileRoundTrip:
    def test_pile_round_trip_with_mixed_lion_class_forms(self):
        from lionagi.protocols.generic.pile import Pile
        from lionagi.protocols.messages.instruction import Instruction
        from lionagi.protocols.messages.system import System

        system_msg = System(content={"system_message": "be helpful"})
        instruction_msg = Instruction(
            content={"instruction": "hello"}, sender="user", recipient="assistant"
        )

        pile = Pile(collections=[system_msg, instruction_msg])
        serialized = pile.to_dict()

        # Rewrite one entry to the legacy short-name form to prove the pile
        # tolerates a mix of fully-qualified and short lion_class values.
        for item in serialized["collections"]:
            if item["metadata"]["lion_class"] == System.class_name(full=True):
                item["metadata"]["lion_class"] = "System"

        restored = Pile.from_dict(serialized)
        types_by_id = {v.id: type(v) for v in restored.collections.values()}
        assert types_by_id[system_msg.id] is System
        assert types_by_id[instruction_msg.id] is Instruction
