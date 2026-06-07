# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for V1 Observable Protocol compatibility with V0 classes."""

from lionagi.protocols.contracts import ObservableProto as Observable
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.event import Event
from lionagi.protocols.generic.log import Log
from lionagi.protocols.generic.pile import Pile
from lionagi.protocols.generic.progression import Progression


class TestObservableProtocolCompliance:
    """Test that all V0 classes satisfy the V1 Observable Protocol."""

    def test_element_satisfies_protocol(self):
        """Element satisfies Observable Protocol."""
        element = Element()
        assert isinstance(element, Observable)
        assert hasattr(element, "id")
        assert element.id is not None

    def test_event_satisfies_protocol(self):
        """Event (Element subclass) satisfies Observable Protocol."""
        event = Event()
        assert isinstance(event, Observable)
        assert hasattr(event, "id")
        assert event.id is not None

    def test_log_satisfies_protocol(self):
        """Log (Element subclass) satisfies Observable Protocol."""
        log = Log(content={"message": "test"})
        assert isinstance(log, Observable)
        assert hasattr(log, "id")
        assert log.id is not None

    def test_pile_satisfies_protocol(self):
        """Pile (Element subclass) satisfies Observable Protocol."""
        pile = Pile()
        assert isinstance(pile, Observable)
        assert hasattr(pile, "id")
        assert pile.id is not None

    def test_progression_satisfies_protocol(self):
        """Progression (Element subclass) satisfies Observable Protocol."""
        progression = Progression()
        assert isinstance(progression, Observable)
        assert hasattr(progression, "id")
        assert progression.id is not None

    def test_all_v0_classes_satisfy_protocol(self):
        """Batch test that all V0 classes automatically satisfy V1 Protocol."""
        instances = [
            Element(),
            Event(),
            Log(content={"test": "data"}),
            Pile(),
            Progression(),
        ]

        for instance in instances:
            assert isinstance(instance, Observable), (
                f"{type(instance).__name__} should satisfy Observable Protocol"
            )
            assert hasattr(instance, "id"), f"{type(instance).__name__} should have id attribute"
            assert instance.id is not None, f"{type(instance).__name__}.id should not be None"

    def test_protocol_duck_typing(self):
        """Test that objects with id property satisfy protocol without inheritance."""

        class ForeignObservable:
            def __init__(self):
                self.id = "test-id"

        foreign = ForeignObservable()
        assert isinstance(foreign, Observable)

        class AnotherObservable:
            @property
            def id(self):
                return "another-test-id"

        another = AnotherObservable()
        assert isinstance(another, Observable)

    def test_protocol_rejection(self):
        """Test that objects without id property don't satisfy protocol."""

        class NotObservable:
            pass

        not_obs = NotObservable()
        assert not isinstance(not_obs, Observable)

        class AlmostObservable:
            def __init__(self):
                self.name = "test"  # Has attribute but not 'id'

        almost = AlmostObservable()
        assert not isinstance(almost, Observable)
