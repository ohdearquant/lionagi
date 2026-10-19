# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Pin the existing declaration boundaries before changing their authority."""

from typing import Any

import pytest
from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined

from lionagi.adapters.spec_adapters import PydanticSpecAdapter
from lionagi.ln.types import Meta, Operable
from lionagi.models import FieldModel, OperableModel


def _model(route, field, *, base_type=None):
    if route == "legacy":
        return OperableModel(extra_fields=[field]).new_model("LegacyBoundary", base_type=base_type)
    return PydanticSpecAdapter.create_model(
        Operable((field.to_spec(),)), "NeutralBoundary", base_type=base_type
    )


@pytest.mark.parametrize("route", ["legacy", "spec"])
@pytest.mark.parametrize("default", [0, False, "", None])
def test_falsey_defaults_remain_explicit(route, default):
    model = _model(route, FieldModel(Any, name="value", default=default))

    assert not model.model_fields["value"].is_required()
    value = model().value
    assert value == default
    assert type(value) is type(default)


@pytest.mark.parametrize("route", ["legacy", "spec"])
@pytest.mark.parametrize("nullable", [False, True])
def test_nullable_requiredness_and_error_locations(route, nullable):
    model = _model(route, FieldModel(int, name="value", nullable=nullable))
    field = model.model_fields["value"]

    assert field.annotation == (int | None if nullable else int)
    assert field.is_required() is (not nullable)
    if nullable:
        assert model().value is None
    else:
        assert field.default is PydanticUndefined
        with pytest.raises(ValidationError) as error:
            model()
        assert [(e["loc"], e["type"]) for e in error.value.errors()] == [(("value",), "missing")]
    with pytest.raises(ValidationError) as error:
        model(value="not-an-integer")
    assert [(e["loc"], e["type"]) for e in error.value.errors()] == [(("value",), "int_parsing")]


@pytest.mark.parametrize("route", ["legacy", "spec"])
@pytest.mark.parametrize("key", ["default", "default_factory"])
def test_callable_defaults_run_per_instance_and_keep_list_annotation(route, key):
    calls = []

    def factory():
        calls.append(None)
        return []

    field = FieldModel(list[int], name="value", listable=True, **{key: factory})
    direct = field.create_field()
    projected = PydanticSpecAdapter.create_field(field.to_spec())
    for info in (direct, projected):
        assert info.annotation == list[int]
        assert info.default is PydanticUndefined
        assert info.default_factory is factory
    model = _model(route, field)
    assert calls == []
    first, second = model(), model()
    first.value.append(7)
    assert first.value == [7]
    assert second.value == []
    assert len(calls) == 2


@pytest.mark.parametrize("route", ["legacy", "spec"])
def test_alias_and_nested_schema_default_do_not_replace_runtime_default(route):
    field = FieldModel(
        int,
        name="value",
        alias="wire",
        default=0,
        json_schema_extra={"default": 999, "x-example": "kept"},
    )
    model = _model(route, field)

    assert model().value == 0
    assert model.model_validate({"wire": 4}).model_dump(by_alias=True) == {"wire": 4}
    assert model.model_fields["value"].json_schema_extra == {
        "default": 999,
        "x-example": "kept",
    }
    schema = model.model_json_schema()["properties"]["wire"]
    assert schema["default"] == 999
    assert schema["x-example"] == "kept"


@pytest.mark.parametrize("route", ["legacy", "spec"])
def test_plain_named_field_currently_leaks_name_into_schema_extra(route):
    model = _model(route, FieldModel(int, name="value"))

    assert model.model_fields["value"].json_schema_extra == {"name": "value"}
    assert model.model_json_schema()["properties"]["value"]["name"] == "value"


def test_repeated_metadata_uses_first_owner_but_last_materialized_value():
    field = FieldModel(
        int,
        metadata=(
            Meta("name", "first"),
            Meta("name", "second"),
            Meta("description", "old"),
            Meta("description", "new"),
            Meta("default", 1),
            Meta("default", 2),
        ),
    )
    model = _model("legacy", field)

    assert field.name == "first"
    assert field.extract_metadata("default") == 1
    assert tuple(model.model_fields) == ("first",)
    assert model().first == 2
    assert model.model_fields["first"].description == "new"
    assert model.model_fields["first"].json_schema_extra == {"name": "second"}
    with pytest.raises(ValueError, match="Duplicate metadata key: name"):
        field.to_spec()


def test_repeated_validators_keep_only_the_last_materialized_validator():
    calls = []

    def first(value):
        calls.append("first")
        return value + 1

    def last(value):
        calls.append("last")
        return value * 10

    field = FieldModel(int, name="value", validator=first).with_validator(last)
    model = _model("legacy", field)

    assert model(value=3).value == 30
    assert calls == ["last"]
    with pytest.raises(ValueError, match="Duplicate metadata key: validator"):
        field.to_spec()


@pytest.mark.parametrize("route", ["legacy", "spec"])
def test_single_validator_preserves_value_and_normalized_failure(route):
    def positive(value):
        if value <= 0:
            raise ValueError("positive only")
        return value + 1

    model = _model(route, FieldModel(int, name="value", validator=positive))

    assert model(value=2).value == 3
    with pytest.raises(ValidationError) as error:
        model(value=0)
    assert [(e["loc"], e["type"], e["msg"]) for e in error.value.errors()] == [
        (("value",), "value_error", "Value error, positive only")
    ]


@pytest.mark.parametrize(("route", "expected"), [("legacy", 2), ("spec", 10)])
def test_base_collision_currently_has_different_precedence(route, expected):
    class Base(BaseModel):
        value: int = 10
        retained: str = "base"

    model = _model(route, FieldModel(int, name="value", default=2), base_type=Base)

    assert issubclass(model, Base)
    assert model().model_dump() == {"value": expected, "retained": "base"}
    assert Base().value == 10
