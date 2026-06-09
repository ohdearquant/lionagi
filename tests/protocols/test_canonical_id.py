# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for ID bridge utilities between V0 and V1."""

from uuid import UUID, uuid4

import pytest

from lionagi.protocols.generic.element import ID, Element
from lionagi.protocols.generic.event import Event
from lionagi.protocols.generic.log import Log
from lionagi.protocols.ids import canonical_id, to_uuid


class TestToUuidUtility:
    def test_element_to_uuid(self):
        element = Element()
        result = to_uuid(element)
        assert isinstance(result, UUID)
        assert result.version == 4

    def test_element_id_to_uuid(self):
        element = Element()
        result = to_uuid(element.id)
        assert isinstance(result, UUID)
        assert result.version == 4

    def test_idtype_to_uuid(self):
        id_type = uuid4()
        result = to_uuid(id_type)
        assert isinstance(result, UUID)
        assert result.version == 4

    def test_uuid_to_uuid_passthrough(self):
        original_uuid = uuid4()
        result = to_uuid(original_uuid)
        assert result == original_uuid
        assert isinstance(result, UUID)

    def test_string_to_uuid(self):
        uuid_str = "550e8400-e29b-41d4-a716-446655440000"
        result = to_uuid(uuid_str)
        assert isinstance(result, UUID)
        assert str(result) == uuid_str

    def test_invalid_string_raises_error(self):
        with pytest.raises(ValueError):
            to_uuid("not-a-uuid")

    def test_consistency_with_idtype_validate(self):
        test_values = [
            "550e8400-e29b-41d4-a716-446655440000",
            uuid4(),
            uuid4(),
        ]

        for value in test_values:
            # Both should succeed or fail together
            try:
                validated = ID.get_id(value)
                converted = to_uuid(value)
                assert isinstance(converted, UUID)
                assert str(validated) == str(converted)
            except Exception as e1:
                with pytest.raises(type(e1)):
                    to_uuid(value)


class TestCanonicalIdUtility:
    def test_element_canonical_id(self):
        element = Element()
        result = canonical_id(element)
        assert isinstance(result, UUID)
        assert result.version == 4

    def test_event_canonical_id(self):
        event = Event()
        result = canonical_id(event)
        assert isinstance(result, UUID)
        assert result.version == 4

    def test_log_canonical_id(self):
        log = Log(content={"test": "data"})
        result = canonical_id(log)
        assert isinstance(result, UUID)
        assert result.version == 4

    def test_raw_uuid_canonical_id(self):
        original_uuid = uuid4()
        result = canonical_id(original_uuid)
        assert result == original_uuid

    def test_raw_string_canonical_id(self):
        uuid_str = "550e8400-e29b-41d4-a716-446655440000"
        result = canonical_id(uuid_str)
        assert isinstance(result, UUID)
        assert str(result) == uuid_str

    def test_observable_like_object(self):

        class MockObservable:
            def __init__(self):
                self.id = uuid4()

        mock = MockObservable()
        result = canonical_id(mock)
        assert result == mock.id

    def test_observable_like_with_idtype(self):

        class MockWithUUID:
            def __init__(self):
                self.id = uuid4()

        mock = MockWithUUID()
        result = canonical_id(mock)
        assert isinstance(result, UUID)
        assert str(result) == str(mock.id)

    def test_roundtrip_consistency(self):
        elements = [Element(), Event(), Log(content={"test": "value"})]

        for element in elements:
            canonical = canonical_id(element)
            # Converting back through to_uuid should yield same result
            assert canonical_id(canonical) == canonical

    def test_foreign_object_fallback(self):
        uuid_obj = uuid4()
        result = canonical_id(uuid_obj)
        assert result == uuid_obj
