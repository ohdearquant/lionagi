# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Guards for the Observable contract: structural (protocol) admission by Pile.

Observable is a runtime-checkable protocol -- an object conforms by exposing an
``id``, whether or not it inherits anything. These tests are the regression
guard for that behavior: a 2026-07 change briefly made admission nominal
(isinstance required inheritance), and this suite fails under that nominal
variant. Structural admission is the intended design, not a defect -- do not
"fix" these tests toward inheritance-only admission.
"""

from uuid import uuid4

import pytest

from lionagi._errors import ValidationError
from lionagi.protocols._concepts import Observable
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.event import Event
from lionagi.protocols.generic.log import Log
from lionagi.protocols.generic.pile import Pile
from lionagi.protocols.generic.progression import Progression


class _Duck:
    """A duck-typed item: exposes a UUID ``id`` without inheriting anything."""

    def __init__(self):
        self.id = uuid4()


class TestObservableIsStructural:
    """isinstance(obj, Observable) is true for any object exposing an ``id``."""

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

    def test_element_does_not_inherit_observable(self):
        """Conformance is by capability: Element satisfies the protocol without
        inheriting it (a runtime-checkable Protocol cannot be a pydantic base)."""
        assert Observable not in Element.__mro__
        assert isinstance(Element(), Observable)

    def test_duck_typed_object_with_id_is_observable(self):
        """Exposing ``id`` is sufficient -- inheritance is not required.

        This is the regression guard: under nominal-only admission this assertion
        is False.
        """
        assert isinstance(_Duck(), Observable)

    def test_object_without_id_is_not_observable(self):
        class NoId:
            pass

        assert not isinstance(NoId(), Observable)


class TestPileAdmissionIsStructural:
    """Pile admits any id-bearing object, not just Observable subclasses."""

    def test_element_is_admitted(self):
        pile = Pile()
        item = Element()
        pile.include(item)
        assert item in pile
        assert pile[item.id] is item

    def test_duck_typed_item_with_uuid_id_is_admitted(self):
        """A class exposing a UUID ``id`` without inheriting is a first-class
        pile item: admitted, found, retrievable, and removable by identity."""
        pile = Pile()
        duck = _Duck()
        pile.include(duck)
        assert duck in pile
        assert pile[duck.id] is duck
        assert len(pile) == 1
        pile.exclude(duck)
        assert duck not in pile
        assert len(pile) == 0

    def test_duck_typed_item_admitted_at_construction(self):
        duck = _Duck()
        pile = Pile(collections=[duck])
        assert duck in pile
        assert pile[duck.id] is duck

    def test_progression_accepts_duck_typed_object(self):
        duck = _Duck()
        prog = Progression(order=[duck])
        assert duck in prog
        assert list(prog) == [duck.id]

    def test_item_without_id_is_rejected(self):
        class Plain:
            pass

        pile = Pile()
        with pytest.raises(ValueError, match="Invalid pile item"):
            pile.include(Plain())
        assert len(pile) == 0

    def test_item_type_accepts_an_observable_shaped_type(self):
        """A restricted item_type is validated structurally: a class whose
        instances expose ``id`` is accepted, whether or not it inherits."""

        class DuckType:
            id: object

        pile = Pile(item_type={DuckType})
        assert DuckType in pile.item_type

    def test_item_type_rejects_a_type_without_id(self):
        class NoIdType:
            pass

        with pytest.raises(ValidationError) as excinfo:
            Pile(item_type={NoIdType})
        assert "id" in excinfo.value.details.get("expected", "")


class TestPublicContractMatchesEnforcement:
    """The public Observable export is the exact contract Pile enforces.

    Previously ``lionagi.protocols.types.Observable`` (a structural protocol) and
    Pile admission (a nominal ABC) disagreed: isinstance could report True for an
    object Pile rejected, or -- after the nominal regression -- Pile could be
    asked to admit only what inherits while the name still read as structural.
    A single structural contract closes the gap in both directions: isinstance
    and admission give the same answer.
    """

    def test_public_symbol_is_the_admission_contract(self):
        from lionagi.protocols.types import Observable as PublicObservable

        duck = _Duck()
        pile = Pile()
        try:
            pile.include(duck)
            admitted = True
        except (ValueError, TypeError):
            admitted = False

        assert isinstance(duck, PublicObservable) == admitted
        assert admitted is True

    def test_public_symbol_identity(self):
        from lionagi.protocols.types import Observable as PublicObservable

        assert PublicObservable is Observable
