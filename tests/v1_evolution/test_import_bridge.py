# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for import bridge between V0 and V1 Observable.

Validates that the central import location (types.py) correctly exports
both V1 (preferred) and V0 (legacy) Observable classes with proper identity.
"""

from abc import ABCMeta
from typing import Protocol

import pytest


class TestImportBridge:
    """Test import bridge exports and identity verification."""

    def test_import_bridge_exports_and_identity(self):
        """Verify import bridge exports correct classes under correct names."""
        # Should not fail due to ImportError
        try:
            from lionagi.protocols.types import LegacyObservable, Observable
        except ImportError:
            pytest.fail("Could not import Observable and LegacyObservable from types.py")

        # Import the sources of truth
        from lionagi.protocols._concepts import Observable as V0Source
        from lionagi.protocols.contracts import Observable as V1Source

        # Verify V1 Observable (the preferred export)
        assert Observable is V1Source, "types.Observable should be the V1 Protocol"
        assert issubclass(Observable, Protocol)

        # Verify LegacyObservable (the V0 ABC)
        assert LegacyObservable is V0Source, "types.LegacyObservable should be the V0 ABC"
        # Check if it's an ABCMeta (standard check for Abstract Base Classes)
        assert isinstance(LegacyObservable, ABCMeta), "LegacyObservable should be an ABC"

        # Ensure they are distinct
        assert Observable is not LegacyObservable, (
            "V1 Observable and LegacyObservable should be distinct objects"
        )

    def test_bridge_utilities_exported(self):
        """Verify bridge utilities are exported from types.py."""
        try:
            from lionagi.protocols.types import canonical_id, to_uuid
        except ImportError:
            pytest.fail("Could not import canonical_id and to_uuid from types.py")

        # Verify they are callables (functions)
        assert callable(canonical_id), "canonical_id should be callable"
        assert callable(to_uuid), "to_uuid should be callable"

        # Verify they come from the ids module
        from lionagi.protocols.ids import canonical_id as id_source
        from lionagi.protocols.ids import to_uuid as uuid_source

        assert canonical_id is id_source, "canonical_id should be imported from ids.py"
        assert to_uuid is uuid_source, "to_uuid should be imported from ids.py"

    def test_observable_proto_explicit_export(self):
        """Verify ObservableProto is explicitly exported alongside Observable alias."""
        try:
            from lionagi.protocols.types import ObservableProto
        except ImportError:
            pytest.fail("Could not import ObservableProto from types.py")

        from lionagi.protocols.types import Observable

        # Should be the same object
        assert ObservableProto is Observable, "ObservableProto should be the same as Observable"

    def test_all_exports_in_module_all(self):
        """Verify all new exports are listed in __all__."""
        from lionagi.protocols import types

        required_exports = [
            "Observable",
            "ObservableProto",
            "LegacyObservable",
            "canonical_id",
            "to_uuid",
        ]

        for export in required_exports:
            assert export in types.__all__, f"'{export}' should be in types.__all__"

        # Verify they can actually be imported
        for export in required_exports:
            assert hasattr(types, export), f"'{export}' should be available in types module"

    def test_backward_compatibility_import_paths(self):
        """Verify existing import paths still work."""
        # These imports should continue to work for V0 compatibility
        try:
            from lionagi.protocols.types import Element, Event, Log, Pile
        except ImportError:
            pytest.fail("Existing V0 imports from types.py should still work")

        # Existing V0 classes should still work
        element = Element()
        assert hasattr(element, "id"), "Element should still have id attribute"
