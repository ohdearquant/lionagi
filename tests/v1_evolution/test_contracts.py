# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for V1 Observable Protocol contract definition.

Following TDD methodology - tests for protocol definition, runtime checkability,
and structural typing behavior.
"""

import inspect
from typing import Protocol

import pytest


class TestObservableProtocolDefinition:
    """Test the V1 Observable Protocol contract and structure."""

    def test_observable_is_runtime_checkable_protocol(self):
        """Verify Observable is a runtime-checkable Protocol."""
        # Should not fail due to ImportError
        try:
            from lionagi.protocols.contracts import Observable
        except ImportError:
            pytest.fail("V1 Observable Protocol could not be imported from contracts.py")

        # Verify it is a Protocol
        assert issubclass(Observable, Protocol), "Observable must be a Protocol"

        # Verify it is runtime_checkable (checking the attribute added by decorator)
        # Note: The attribute name might vary by Python version
        is_checkable = getattr(Observable, "_is_runtime_checkable", False) or getattr(
            Observable, "_is_runtime_protocol", False
        )
        assert is_checkable, "Observable must be @runtime_checkable"

    def test_observable_defines_id_property_permissive(self):
        """Verify Observable defines 'id' property with permissive typing."""
        from lionagi.protocols.contracts import Observable

        # Check for the 'id' attribute
        assert hasattr(Observable, "id"), "Observable must define 'id'"

        # Verify 'id' is a property
        assert isinstance(Observable.id, property), "'id' must be a property"

        # Verify the return type annotation is permissive (object)
        id_getter = Observable.id.fget
        annotations = inspect.getfullargspec(id_getter).annotations
        # Note: Using 'object' for V0 compatibility as per reviewer recommendation
        expected_type = annotations.get("return")
        # Handle both direct type object and string 'object' (forward references)
        assert expected_type is object or expected_type == object or expected_type == "object", (
            f"Return type of id should be 'object' for V0 compatibility, got {expected_type}"
        )

    def test_observable_structural_typing(self):
        """Test structural typing works with the Observable Protocol."""
        from lionagi.protocols.contracts import Observable

        class MockCompliant:
            @property
            def id(self):
                return "123-abc"

        class MockNonCompliant:
            pass

        # Structural typing should work for compliant objects
        assert isinstance(MockCompliant(), Observable)

        # Should reject non-compliant objects
        assert not isinstance(MockNonCompliant(), Observable)

    def test_observable_alias_consistency(self):
        """Test that Observable alias points to ObservableProto."""
        from lionagi.protocols.contracts import Observable, ObservableProto

        # They should be the same object
        assert Observable is ObservableProto, "Observable should be an alias for ObservableProto"

    def test_legacy_observable_import(self):
        """Test that LegacyObservable can be imported from contracts."""
        try:
            from lionagi.protocols.contracts import LegacyObservable
        except ImportError:
            pytest.fail("Could not import LegacyObservable from contracts.py")

        # Should be the original ABC from _concepts
        from lionagi.protocols._concepts import Observable as OriginalABC

        assert LegacyObservable is OriginalABC, (
            "LegacyObservable should reference the original V0 ABC"
        )
