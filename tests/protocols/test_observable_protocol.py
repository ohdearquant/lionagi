# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for the Observable contract: nominal admission, enforced by Pile."""

import pytest

from lionagi._errors import ValidationError
from lionagi.protocols._concepts import Observable
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.event import Event
from lionagi.protocols.generic.log import Log
from lionagi.protocols.generic.pile import Pile
from lionagi.protocols.generic.progression import Progression


class TestObservableNominalContract:
    """Observable is a nominal ABC; isinstance requires inheritance, not just an 'id'."""

    def test_element_is_observable(self):
        element = Element()
        assert isinstance(element, Observable)
        assert element.id is not None

    def test_event_is_observable(self):
        assert isinstance(Event(), Observable)

    def test_log_is_observable(self):
        assert isinstance(Log(content={"message": "test"}), Observable)

    def test_pile_is_observable(self):
        assert isinstance(Pile(), Observable)

    def test_progression_is_observable(self):
        assert isinstance(Progression(), Observable)

    def test_duck_typed_object_is_not_observable(self):
        """Exposing 'id' without inheriting the ABC does not satisfy nominal admission."""

        class DuckTyped:
            def __init__(self):
                self.id = "some-id"

        assert not isinstance(DuckTyped(), Observable)

    def test_object_without_id_is_not_observable(self):
        class NotObservable:
            pass

        assert not isinstance(NotObservable(), Observable)


class TestPileAdmissionIsNominal:
    """Pile item admission enforces the nominal Observable ABC, not structural duck-typing."""

    def test_element_subclass_is_admitted(self):
        pile = Pile()
        item = Element()
        pile.include(item)
        assert item in pile
        assert pile[item.id] is item

    def test_duck_typed_item_with_id_is_rejected_with_clear_error(self):
        """A class exposing 'id' but not inheriting Observable fails admission."""

        class DuckTypedItem:
            def __init__(self):
                self.id = "duck-id"

        pile = Pile()
        with pytest.raises(ValueError, match="Invalid pile item"):
            pile.include(DuckTypedItem())
        assert len(pile) == 0

    def test_item_without_id_is_rejected_with_clear_error(self):
        class Plain:
            pass

        pile = Pile()
        with pytest.raises(ValueError, match="Invalid pile item"):
            pile.include(Plain())
        assert len(pile) == 0

    def test_item_type_restriction_also_requires_nominal_observable(self):
        """A restricted item_type must itself subclass Observable — structural types are rejected."""

        class NotObservableType:
            id: str

        with pytest.raises(ValidationError) as excinfo:
            Pile(item_type={NotObservableType})
        assert excinfo.value.details.get("expected") == "A subclass of Observable."

    def test_construction_rejects_duck_typed_collections(self):
        class DuckTypedItem:
            def __init__(self):
                self.id = "duck-id"

        with pytest.raises(ValueError, match="Invalid pile item"):
            Pile(collections=[DuckTypedItem()])


class TestPublicContractMatchesEnforcement:
    """The public Observable export must be the exact contract Pile enforces.

    Previously ``lionagi.protocols.types.Observable`` pointed at the structural
    ``ObservableProto`` (any object with an 'id'), while Pile enforced the nominal
    ABC from ``_concepts.py``. That let ``isinstance(x, types.Observable)`` report
    True for objects Pile would still reject.
    """

    def test_public_observable_symbol_is_the_pile_admission_contract(self):
        from lionagi.protocols.types import Observable as PublicObservable

        class DuckTyped:
            def __init__(self):
                self.id = "duck-id"

        duck = DuckTyped()
        pile = Pile()
        try:
            pile.include(duck)
            admitted = True
        except (ValueError, TypeError):
            admitted = False

        assert isinstance(duck, PublicObservable) == admitted
