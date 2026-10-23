# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Flow deserialization resolves scalar IDs through the structural identity contract."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from lionagi.protocols.generic.element import Element
from lionagi.protocols.generic.flow import Flow

_ID = UUID("00000000-0000-4000-8000-000000000001")
_OTHER_ID = UUID("00000000-0000-4000-8000-000000000002")


class _StringOnlyId:
    def __str__(self) -> str:
        return str(_OTHER_ID)


class _IdWithDifferentString(_StringOnlyId):
    id = _ID


@pytest.mark.parametrize(
    "make_id",
    [
        lambda: Element(id=_ID),
        lambda: SimpleNamespace(id=_ID),
        lambda: SimpleNamespace(id=str(_ID)),
    ],
    ids=["element", "duck-uuid", "duck-string"],
)
def test_from_dict_resolves_observable_id_without_replacing_caller_value(make_id):
    id_source = make_id()
    payload = {"id": id_source}

    restored = Flow.from_dict(payload)

    assert restored.id == _ID
    assert isinstance(restored.id, UUID)
    assert payload["id"] is id_source


def test_from_dict_uses_observable_id_instead_of_string_representation():
    restored = Flow.from_dict({"id": _IdWithDifferentString()})

    assert restored.id == _ID
    assert restored.id != _OTHER_ID


def test_from_dict_rejects_object_that_only_stringifies_to_uuid():
    with pytest.raises(ValueError, match=r"^Cannot get ID from item\.$"):
        Flow.from_dict({"id": _StringOnlyId()})


@pytest.mark.parametrize("value", [123, None], ids=["integer", "none"])
def test_from_dict_rejects_unsupported_id_with_canonical_error(value):
    with pytest.raises(ValueError, match=r"^Cannot get ID from item\.$"):
        Flow.from_dict({"id": value})


@pytest.mark.parametrize("value", [_ID, str(_ID)], ids=["uuid", "string"])
def test_from_dict_keeps_raw_uuid_and_string_ids(value):
    restored = Flow.from_dict({"id": value})

    assert restored.id == _ID
    if isinstance(value, UUID):
        assert restored.id is value


def test_from_dict_preserves_malformed_string_parser_error():
    with pytest.raises(ValueError) as parser_error:
        UUID("not-a-uuid")

    with pytest.raises(ValueError) as flow_error:
        Flow.from_dict({"id": "not-a-uuid"})

    assert str(flow_error.value) == str(parser_error.value)


def test_from_dict_without_id_still_generates_uuid():
    assert isinstance(Flow.from_dict({}).id, UUID)
