# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for lionagi/_class_registry.py

Module path: lionagi/_class_registry.py

Covers:
- FILE_REGISTRY population at import (filesystem scan)
- LION_CLASS_REGISTRY population via Node.__pydantic_init_subclass__
- get_class: hit (registry) + miss (unknown name)
- get_class file-registry fallback path (pinned as known-broken latent bug)
- Duplicate-name handling: real-path collision via actual subclass creation
- Registry isolation: autouse fixture snapshots/restores both registries
- Polymorphic round-trip: Node subclass -> to_dict -> Element.from_dict -> type preserved
- db-mode round-trip (node_metadata key instead of metadata)
- get_file_classes on a single file
- get_class_file_registry with non-existent folder and empty patterns

NOTE: Node subclasses used in tests are defined at MODULE LEVEL so their
full-qualified names are importable (required by Element.from_dict fallback
path via import_module).  Subclasses defined inside test-function local scope
would produce un-importable qualified names, causing ImportError in the
fallback deserialization path.
"""

import tempfile
from pathlib import Path

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
# 2. Test time — individual tests mutate the registries (deleting keys,
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
    """Snapshot both registries before each test; restore on teardown.

    Uses an explicit try/finally so that restoration is guaranteed even if
    the test body raises.  This prevents registry pollution across tests on
    the same xdist worker.
    """
    from lionagi._class_registry import LION_CLASS_FILE_REGISTRY, LION_CLASS_REGISTRY

    registry_snapshot = LION_CLASS_REGISTRY.copy()
    file_registry_snapshot = LION_CLASS_FILE_REGISTRY.copy()
    try:
        yield
    finally:
        LION_CLASS_REGISTRY.clear()
        LION_CLASS_REGISTRY.update(registry_snapshot)
        LION_CLASS_FILE_REGISTRY.clear()
        LION_CLASS_FILE_REGISTRY.update(file_registry_snapshot)


# ---------------------------------------------------------------------------
# 1. FILE_REGISTRY population at import
# ---------------------------------------------------------------------------


class TestFileRegistryPopulation:
    """LION_CLASS_FILE_REGISTRY is populated by a filesystem scan at import."""

    def test_file_registry_is_dict(self):
        from lionagi._class_registry import LION_CLASS_FILE_REGISTRY

        assert isinstance(LION_CLASS_FILE_REGISTRY, dict)

    def test_file_registry_nonempty(self):
        from lionagi._class_registry import LION_CLASS_FILE_REGISTRY

        assert len(LION_CLASS_FILE_REGISTRY) > 0

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
    def test_known_core_classes_present(self, class_name):
        from lionagi._class_registry import LION_CLASS_FILE_REGISTRY

        assert class_name in LION_CLASS_FILE_REGISTRY, (
            f"{class_name} missing from LION_CLASS_FILE_REGISTRY"
        )

    def test_file_registry_values_are_existing_paths(self):
        from lionagi._class_registry import LION_CLASS_FILE_REGISTRY

        for name, path in LION_CLASS_FILE_REGISTRY.items():
            assert Path(path).exists(), f"Path for {name!r} does not exist: {path}"

    def test_file_registry_values_are_py_files(self):
        from lionagi._class_registry import LION_CLASS_FILE_REGISTRY

        for name, path in LION_CLASS_FILE_REGISTRY.items():
            assert path.endswith(".py"), f"Path for {name!r} is not a .py file: {path}"

    def test_file_registry_paths_inside_scanned_patterns(self):
        """All scanned paths must be inside one of the declared pattern folders."""
        from lionagi._class_registry import (
            LION_CLASS_FILE_REGISTRY,
            pattern_list,
        )

        for name, path in LION_CLASS_FILE_REGISTRY.items():
            assert any(p in path for p in pattern_list), (
                f"Path for {name!r} ({path}) is outside all declared patterns"
            )

    def test_file_registry_size_is_stable(self):
        """FILE_REGISTRY size must stay constant across multiple accesses
        (the guard `if not LION_CLASS_FILE_REGISTRY` prevents re-scanning)."""
        from lionagi._class_registry import LION_CLASS_FILE_REGISTRY

        count_first = len(LION_CLASS_FILE_REGISTRY)
        # Access the registry again via a second import statement.
        # Python's module cache means this is the same object; size is unchanged.
        from lionagi._class_registry import LION_CLASS_FILE_REGISTRY as reg2

        assert len(reg2) == count_first
        assert reg2 is LION_CLASS_FILE_REGISTRY  # same dict object


# ---------------------------------------------------------------------------
# 2. LION_CLASS_REGISTRY population via Node.__pydantic_init_subclass__
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

        # Node is not registered via __pydantic_init_subclass__
        # (that hook fires only for classes that subclass Node, not Node itself)
        node_full = Node.class_name(full=True)
        # Node may appear in file registry (FILE_REGISTRY), not LION_CLASS_REGISTRY
        assert node_full not in LION_CLASS_REGISTRY


# ---------------------------------------------------------------------------
# 3. get_class: hit (direct registry lookup)
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
# 4. get_class: miss behavior (unknown name)
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


# ---------------------------------------------------------------------------
# 4b. get_class file-registry fallback path — pinned as known-broken
#
# LATENT BUG: get_class()'s fallback at _class_registry.py:100 calls
# get_class_objects() which uses importlib.util.spec_from_file_location with
# a dummy module name ("module.name").  Modules in the scanned directories
# (lionagi/protocols/generic, protocols/graph, protocols/messages) use
# relative imports (e.g. "from .element import Element").  When exec'd under a
# standalone spec with no parent package, those relative imports fail with
# "ImportError: attempted relative import beyond top-level package".
#
# As a result, only in-memory LION_CLASS_REGISTRY lookups work reliably.
# The file-registry fallback path is unreachable for any real lionagi class.
#
# This test pins the current behavior so that any accidental "fix" that changes
# the error type or suppresses the ValueError is caught immediately.
# ---------------------------------------------------------------------------


class TestGetClassFileRegistryFallback:
    """Pin the broken file-registry fallback in get_class().

    The fallback at _class_registry.py:100 always raises ImportError when
    trying to load modules that use relative imports, and get_class() wraps
    that in ValueError.  This is a documented latent bug.
    """

    @pytest.mark.xfail(
        strict=True,
        raises=ValueError,
        reason=(
            "LATENT BUG: get_class_objects() executes modules via a bare spec "
            "('module.name') with no parent package context.  Any module that "
            "uses relative imports (from .x import Y) raises "
            "'ImportError: attempted relative import beyond top-level package'. "
            "This makes the file-registry fallback path unreachable for all "
            "real lionagi classes.  Only in-memory LION_CLASS_REGISTRY hits "
            "work.  Tracked so a future fix is pinned explicitly."
        ),
    )
    def test_file_registry_fallback_raises_for_relative_import_module(self):
        """Calling get_class() on a scanned class NOT already in LION_CLASS_REGISTRY
        triggers the file-registry fallback and raises because relative imports fail."""
        from lionagi._class_registry import (
            LION_CLASS_FILE_REGISTRY,
            LION_CLASS_REGISTRY,
            get_class,
        )

        # Find a class that is in FILE_REGISTRY but not yet in LION_CLASS_REGISTRY
        # by temporarily removing it from the in-memory registry.
        # We use 'Node' which is guaranteed to be in FILE_REGISTRY.
        target = "Node"
        assert target in LION_CLASS_FILE_REGISTRY, (
            "Prerequisite: Node must be in LION_CLASS_FILE_REGISTRY"
        )

        # Remove from in-memory registry so get_class falls through to file path.
        # (The autouse _registry_snapshot fixture will restore this automatically.)
        keys_to_remove = [k for k in LION_CLASS_REGISTRY if k.endswith(f".{target}") or k == target]
        for k in keys_to_remove:
            del LION_CLASS_REGISTRY[k]

        # Also remove bare name if present.
        LION_CLASS_REGISTRY.pop(target, None)

        # This should succeed if the fallback worked.  Because modules use
        # relative imports, it actually raises ValueError wrapping ImportError.
        # The xfail marks that as the *expected* failure — it passes when broken,
        # fails (xpass) if someone fixes the underlying importlib loading logic.
        result = get_class(target)
        assert isinstance(result, type)  # only reached if fallback is fixed


# ---------------------------------------------------------------------------
# 5. Duplicate-name handling: last writer wins (overwrite semantics — pinned)
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
# 6. Polymorphic round-trip (python mode)
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
# 7. db-mode round-trip (node_metadata key)
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
# 8. get_file_classes utility function
# ---------------------------------------------------------------------------


class TestGetFileClasses:
    """get_file_classes parses a Python file and returns class names -> path."""

    @pytest.fixture
    def element_py_path(self):
        return str(
            Path(__file__).parent.parent / "lionagi" / "protocols" / "generic" / "element.py"
        )

    def test_returns_dict(self, element_py_path):
        from lionagi._class_registry import get_file_classes

        result = get_file_classes(element_py_path)
        assert isinstance(result, dict)

    def test_finds_element_class(self, element_py_path):
        from lionagi._class_registry import get_file_classes

        result = get_file_classes(element_py_path)
        assert "Element" in result

    def test_values_equal_input_path(self, element_py_path):
        from lionagi._class_registry import get_file_classes

        result = get_file_classes(element_py_path)
        for name, path in result.items():
            assert path == element_py_path

    def test_empty_file_returns_empty_dict(self):
        from lionagi._class_registry import get_file_classes

        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write("# no classes here\nx = 1\n")
            tmp_path = f.name

        result = get_file_classes(tmp_path)
        assert result == {}

    def test_file_with_multiple_classes(self):
        from lionagi._class_registry import get_file_classes

        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write("class Foo:\n    pass\nclass Bar:\n    pass\n")
            tmp_path = f.name

        result = get_file_classes(tmp_path)
        assert "Foo" in result
        assert "Bar" in result
        assert len(result) == 2


# ---------------------------------------------------------------------------
# 9. get_class_file_registry utility function
# ---------------------------------------------------------------------------


class TestGetClassFileRegistry:
    """get_class_file_registry walks a folder and builds the file registry."""

    def test_nonexistent_folder_returns_empty(self):
        from lionagi._class_registry import get_class_file_registry

        result = get_class_file_registry("/nonexistent/path/xyz_abc_123", ["pattern"])
        assert result == {}

    def test_empty_pattern_list_returns_empty(self):
        from lionagi._class_registry import get_class_file_registry

        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "sample.py").write_text("class SampleClass:\n    pass\n")
            result = get_class_file_registry(tmpdir, [])
        assert result == {}

    def test_matching_pattern_picks_up_class(self):
        from lionagi._class_registry import get_class_file_registry

        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = Path(tmpdir) / "mypackage" / "protocols"
            subdir.mkdir(parents=True)
            (subdir / "model.py").write_text("class MyModel:\n    pass\n")
            result = get_class_file_registry(tmpdir, [str(Path("mypackage") / "protocols")])

        assert "MyModel" in result

    def test_nonmatching_pattern_skips_file(self):
        from lionagi._class_registry import get_class_file_registry

        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = Path(tmpdir) / "other"
            subdir.mkdir()
            (subdir / "stuff.py").write_text("class SkippedClass:\n    pass\n")
            result = get_class_file_registry(tmpdir, ["protocols"])

        assert "SkippedClass" not in result

    def test_only_py_files_included(self):
        from lionagi._class_registry import get_class_file_registry

        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = Path(tmpdir) / "protocols"
            subdir.mkdir()
            (subdir / "model.py").write_text("class PyClass:\n    pass\n")
            (subdir / "model.txt").write_text("class TxtClass:\n    pass\n")
            result = get_class_file_registry(tmpdir, ["protocols"])

        assert "PyClass" in result
        assert "TxtClass" not in result
