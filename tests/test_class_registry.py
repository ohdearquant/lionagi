# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for lionagi/_class_registry.py

Module path: lionagi/_class_registry.py

Covers:
- FILE_REGISTRY population at import (filesystem scan)
- LION_CLASS_REGISTRY population via Node.__pydantic_init_subclass__
- get_class: hit (registry) + miss (unknown name)
- Duplicate-name handling (last writer wins — overwrite semantics pinned)
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


# Duplicate-name tests cannot reuse the same Python name at module level
# (the second definition would be a different class but same name binding).
# We use pre-defined sentinel classes instead.
class _DupFirst(Node):
    """First of two classes used to test overwrite semantics."""


class _DupSecond(Node):
    """Second class; will be manually inserted under _DupFirst's key to
    simulate a same-name collision."""


class _LenBefore(Node):
    """Used for registry-length-stable test."""


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
# 5. Duplicate-name handling: last writer wins (overwrite semantics — pinned)
#
# When a second entry is manually inserted under an existing key, the dict
# silently overwrites the prior value. This is the contract for the registry.
# ---------------------------------------------------------------------------


class TestDuplicateNameHandling:
    """Registry uses plain dict assignment: last writer wins.

    This test pins the overwrite semantics so that any future change
    (e.g., raising on collision) is caught explicitly.
    """

    def test_manual_overwrite_replaces_entry(self):
        """Manually overwriting a key with a different class value succeeds."""
        from lionagi._class_registry import LION_CLASS_REGISTRY

        # _DupFirst is already registered under its full-qualified name.
        key = _DupFirst.class_name(full=True)
        assert LION_CLASS_REGISTRY[key] is _DupFirst

        # Manually overwrite with _DupSecond (simulates a same-name redefinition).
        LION_CLASS_REGISTRY[key] = _DupSecond
        assert LION_CLASS_REGISTRY[key] is _DupSecond
        assert LION_CLASS_REGISTRY[key] is not _DupFirst

        # Restore for other tests.
        LION_CLASS_REGISTRY[key] = _DupFirst

    def test_overwrite_does_not_grow_registry(self):
        """Overwriting a key must not increase registry length."""
        from lionagi._class_registry import LION_CLASS_REGISTRY

        key = _LenBefore.class_name(full=True)
        assert key in LION_CLASS_REGISTRY
        size_before = len(LION_CLASS_REGISTRY)

        # Overwrite same key.
        LION_CLASS_REGISTRY[key] = _LenBefore

        size_after = len(LION_CLASS_REGISTRY)
        assert size_after == size_before

    def test_get_class_returns_most_recently_written(self):
        """get_class must return whatever value is currently under the key."""
        from lionagi._class_registry import LION_CLASS_REGISTRY, get_class

        key = _DupFirst.class_name(full=True)
        LION_CLASS_REGISTRY[key] = _DupSecond
        result = get_class(key)
        assert result is _DupSecond

        # Restore.
        LION_CLASS_REGISTRY[key] = _DupFirst


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
