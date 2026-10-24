# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Existing caller semantics that a declaration-authority migration must decide."""

import warnings

import pytest
from pydantic import BaseModel

from lionagi.ln.types import Operable, Spec
from lionagi.models import FieldModel
from lionagi.operations.operate.operate import _specs_from_fields
from lionagi.operations.operate.operative import Operative
from lionagi.operations.operate.step import Step
from lionagi.operations.schema.structure import Structure


@pytest.mark.parametrize("route", ["operative", "structure"])
@pytest.mark.parametrize("base_collision", [False, True])
def test_request_exclusion_filters_declarations_but_not_base_fields(route, base_collision):
    class Base(BaseModel):
        value: int = 10

    class WithResponse(Base):
        action_responses: int = 7

    base = WithResponse if base_collision else Base
    spec = Spec(int, name="value", default=2)
    if route == "operative":
        caller = Operative(
            name="Boundary",
            base_type=base,
            operable=Operable((spec, Spec(int, name="action_responses", default=99))),
            request_exclude={"action_responses"},
        )
        request = caller.create_request_model()
        response = caller.create_response_model()
        assert caller.create_request_model() is request
        assert caller.create_response_model() is response
        absent_base_response_default = 99
    else:
        caller = Structure(base, specs=(spec,), actions=True, name="Boundary")
        request = caller.request_schema()
        response = caller.response_schema()
        assert caller.request_schema() is request
        assert caller.response_schema() is response
        absent_base_response_default = None

    assert issubclass(request, base)
    assert issubclass(response, request)
    assert request.__name__ == "BoundaryRequest"
    assert response.__name__ == "BoundaryResponse"
    assert request().value == response().value == 10
    assert ("action_responses" in request.model_fields) is base_collision
    expected = 7 if base_collision else absent_base_response_default
    assert response().action_responses == expected
    if base_collision:
        assert request().action_responses == 7
        assert response.model_fields["action_responses"].annotation is int


def test_structure_dict_mode_returns_original_schema_and_projects_only_declared_keys():
    schema = {"answer": str, "confidence": float}
    structure = Structure(schema, specs=(Spec(int, name="ignored"),), actions=True, reason=True)

    assert structure.request_schema() is schema
    assert structure.response_schema() is schema
    assert structure.operable is None
    assert structure.to_format({"answer": "done", "extra": 7}) == {
        "answer": "done",
        "confidence": None,
    }


@pytest.mark.parametrize("route", ["step", "operate"])
def test_mixed_field_models_keep_first_slot_and_last_declaration_without_warning(route):
    first = FieldModel(int, name="value", default=1)
    middle = Spec(str, name="label", default="kept")
    last = Spec(int, name="value", default=2)

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always", DeprecationWarning)
        if route == "step":
            operative = Step.request_operative(field_models=[first, middle, last])
            declarations = operative.operable.__op_fields__
        else:
            declarations = tuple(_specs_from_fields([first, middle, last]).values())

    assert not [w for w in recorded if issubclass(w.category, DeprecationWarning)]
    assert tuple(s.name for s in declarations) == ("value", "label")
    assert declarations[0] is last
    assert declarations[1] is middle


def test_step_explicit_fields_take_precedence_over_legacy_container():
    canonical = Spec(int, name="value", default=2)

    operative = Step.request_operative(
        fields={"value": canonical}, field_models=[FieldModel(str, name="ignored")]
    )

    assert operative.operable.__op_fields__ == (canonical,)
    assert operative.request_type().model_dump() == {"value": 2}
