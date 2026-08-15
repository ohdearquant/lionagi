# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for main __init__.py module imports."""

import pytest

import lionagi
from lionagi.ln import import_module


class TestMainImports:
    # All exports from lionagi.__all__ (dunders first, then alphabetical).
    EXPECTED_EXPORTS = (
        "__version__",
        "Adaptable",
        "AdapterError",
        "AdapterRegistry",
        "AsyncAdaptable",
        "AsyncAdapterRegistry",
        "BaseModel",
        "Branch",
        "Broadcaster",
        "Builder",
        "ContextProvider",
        "ContextProviderRegistry",
        "CsvAdapter",
        "DataClass",
        "Edge",
        "Element",
        "Event",
        "Field",
        "FieldModel",
        "Graph",
        "HookRegistry",
        "HookedEvent",
        "InMemoryStore",
        "InvalidConstructorError",
        "JsonAdapter",
        "LNDLError",
        "LNDLOutput",
        "MemoryItem",
        "MemoryQuery",
        "MemoryStore",
        "Message",
        "MissingFieldError",
        "MissingLvarError",
        "Node",
        "Operable",
        "OperableModel",
        "Operation",
        "Params",
        "Pile",
        "Progression",
        "ProviderReport",
        "Session",
        "Spec",
        "TomlAdapter",
        "TypeMismatchError",
        "Undefined",
        "Unset",
        "alcall",
        "create_message",
        "extract_lndl_blocks",
        "get_lndl_system_prompt",
        "iModel",
        "json_dumps",
        "lcall",
        "ln",
        "load_mcp_tools",
        "logger",
        "normalize_lndl_text",
        "to_dict",
        "to_list",
        "types",
    )

    def test_all_exports_defined(self):
        assert hasattr(lionagi, "__all__")
        assert lionagi.__all__ == self.EXPECTED_EXPORTS

    def test_all_exports_alphabetically_sorted(self):
        dunder_names = [name for name in lionagi.__all__ if name.startswith("__")]
        regular_names = [name for name in lionagi.__all__ if not name.startswith("__")]

        # Dunder names must be sorted among themselves
        assert tuple(dunder_names) == tuple(sorted(dunder_names))

        # Regular names must be globally sorted (not just per-group)
        assert tuple(regular_names) == tuple(sorted(regular_names))

        # Dunder names must appear before any regular name
        if dunder_names and regular_names:
            dunder_indices = [i for i, n in enumerate(lionagi.__all__) if n.startswith("__")]
            regular_indices = [i for i, n in enumerate(lionagi.__all__) if not n.startswith("__")]
            assert max(dunder_indices) < min(regular_indices), (
                "All dunder names must precede regular names in __all__"
            )

    @pytest.mark.parametrize("export_name", EXPECTED_EXPORTS)
    def test_import_all_exports(self, export_name):
        obj = import_module("lionagi", import_name=export_name)
        assert obj is not None

    @pytest.mark.parametrize("export_name", EXPECTED_EXPORTS)
    def test_getattr_all_exports(self, export_name):
        obj = getattr(lionagi, export_name)
        assert obj is not None

    def test_lazy_import_caching(self):
        # First access
        session1 = lionagi.Session
        # Second access should return cached version
        session2 = lionagi.Session
        assert session1 is session2

    def test_invalid_import_raises_attribute_error(self):
        with pytest.raises(AttributeError, match="has no attribute"):
            _ = lionagi.NonExistentAttribute

    def test_pydantic_imports(self):
        from pydantic import BaseModel, Field

        assert lionagi.BaseModel is BaseModel
        assert lionagi.Field is Field

    def test_ln_import(self):
        from lionagi import ln

        assert hasattr(ln, "import_module")
        assert hasattr(ln, "types")

    def test_types_module_import(self):
        from lionagi import types

        assert types is not None
        # Should be the _types module
        assert hasattr(types, "__name__")

    def test_version_import(self):
        """Test that __version__ is importable and is a string."""
        from lionagi import __version__

        assert isinstance(__version__, str)
        assert len(__version__) > 0

    def test_logger_import(self):
        from lionagi import logger

        assert logger is not None
        assert hasattr(logger, "info")
        assert hasattr(logger, "error")

    def test_data_classes_import(self):
        from lionagi import DataClass, Params, Undefined, Unset

        assert DataClass is not None
        assert Params is not None
        assert Undefined is not None
        assert Unset is not None


class TestLazyLoadingBehavior:
    def test_lazy_loading_on_first_access(self):
        # Access a lazy-loaded object
        branch = lionagi.Branch
        assert branch is not None
        # Should now be cached in module globals
        assert "Branch" in vars(lionagi)

    def test_multiple_imports_same_object(self):
        obj1 = lionagi.iModel
        obj2 = lionagi.iModel
        obj3 = lionagi.iModel
        assert obj1 is obj2 is obj3

    def test_all_protocol_types_importable(self):
        from lionagi import Edge, Element, Event, Graph, Node, Pile, Progression

        assert Element is not None
        assert Pile is not None
        assert Progression is not None
        assert Node is not None
        assert Edge is not None
        assert Graph is not None
        assert Event is not None

    def test_all_models_importable(self):
        from lionagi import FieldModel, OperableModel

        assert FieldModel is not None
        assert OperableModel is not None

    def test_all_service_types_importable(self):
        from lionagi import Broadcaster, HookedEvent, HookRegistry, iModel

        assert iModel is not None
        assert HookRegistry is not None
        assert HookedEvent is not None
        assert Broadcaster is not None

    def test_all_operation_types_importable(self):
        from lionagi import Builder, Operation, load_mcp_tools

        assert Builder is not None
        assert Operation is not None
        assert load_mcp_tools is not None

    def test_all_session_types_importable(self):
        from lionagi import Branch, Session

        assert Session is not None
        assert Branch is not None
