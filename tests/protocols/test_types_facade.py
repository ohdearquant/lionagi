# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Facade coverage for lionagi.protocols.types: re-export identity and smoke imports."""

from lionagi.protocols import _concepts, types
from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.event import Event
from lionagi.protocols.generic.log import Log
from lionagi.protocols.generic.pile import Pile
from lionagi.protocols.ids import canonical_id as id_source
from lionagi.protocols.ids import to_uuid as uuid_source


def test_canonical_id_is_the_ids_module_source():
    assert types.canonical_id is id_source


def test_to_uuid_is_the_ids_module_source():
    assert types.to_uuid is uuid_source


def test_element_facade_import_works():
    element = types.Element()
    assert isinstance(element, Element)
    assert element.id is not None


def test_event_facade_import_works():
    assert isinstance(types.Event(), Event)


def test_log_facade_import_works():
    assert isinstance(types.Log(content={"message": "test"}), Log)


def test_pile_facade_import_works():
    assert isinstance(types.Pile(), Pile)


def test_public_pileitem_is_the_exact_abc_pile_imports():
    """types.PileItem must be the same object Pile's admission checks import, not a copy."""
    from lionagi.protocols.generic.pile import PileItem as pile_source

    assert types.PileItem is _concepts.PileItem
    assert types.PileItem is pile_source
