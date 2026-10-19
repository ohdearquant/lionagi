# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Characterize the synchronous intermediate response declaration boundary."""

import pytest
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined

from lionagi.operations.ReAct.ReAct import handle_field_models


@pytest.mark.parametrize("nullable", [False, True])
@pytest.mark.parametrize("listable", [False, True])
def test_intermediate_fields_are_ordered_required_nullable_models(nullable, listable):
    class Alpha(BaseModel):
        value: int

    class Beta(BaseModel):
        value: str

    fields = handle_field_models(
        None,
        intermediate_response_options=[Beta, Alpha],
        intermediate_nullable=nullable,
        intermediate_listable=listable,
    )

    assert len(fields) == 1
    field = fields[0]
    assert field.name == "intermediate_response_options"
    assert field.is_nullable is nullable
    assert field.is_listable is listable
    inner = field.base_type.__args__[0] if listable else field.base_type
    assert inner.__name__ == "IntermediateResponseOptions"
    assert tuple(inner.model_fields) == ("beta", "alpha")
    assert inner.model_fields["beta"].annotation == Beta | None
    assert inner.model_fields["alpha"].annotation == Alpha | None
    assert all(info.is_required() for info in inner.model_fields.values())
    assert inner(beta=None, alpha={"value": 3}).model_dump() == {
        "beta": None,
        "alpha": {"value": 3},
    }
    with pytest.raises(ValidationError) as error:
        inner()
    assert [(e["loc"], e["type"]) for e in error.value.errors()] == [
        (("beta",), "missing"),
        (("alpha",), "missing"),
    ]
    info = field.create_field()
    assert info.is_required() is (not nullable)
    assert info.default is (None if nullable else PydanticUndefined)
