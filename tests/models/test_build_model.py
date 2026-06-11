# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Tests for build_model_type (the post-ModelParams direct constructor)."""

import pytest
from pydantic import BaseModel, Field

from lionagi.models import FieldModel
from lionagi.models._build_model import build_model_type


def test_parameter_fields():
    model = build_model_type(name="P", parameter_fields={"x": Field(default=1)})
    assert model.__name__ == "P"
    assert model().x == 1


def test_field_models_with_validator_and_description():
    fm = FieldModel(int, name="count", default=0)
    model = build_model_type(
        name="C",
        field_models=[fm],
        field_descriptions={"count": "how many"},
    )
    assert model().count == 0
    assert model.model_fields["count"].description == "how many"


def test_base_type_inherit_and_exclude():
    class Base(BaseModel):
        a: int = 1
        b: int = 2

    inherited = build_model_type(name="Sub", base_type=Base, inherit_base=True)
    assert issubclass(inherited, Base)

    flat = build_model_type(name="Flat", base_type=Base, inherit_base=False)
    assert not issubclass(flat, Base)
    assert flat().a == 1

    excluded = build_model_type(name="Ex", base_type=Base, exclude_fields=["b"])
    assert "b" not in excluded.model_fields


def test_frozen():
    model = build_model_type(name="F", parameter_fields={"x": Field(default=1)}, frozen=True)
    inst = model()
    with pytest.raises(Exception):
        inst.x = 5


def test_rejects_non_basemodel_base():
    with pytest.raises(ValueError):
        build_model_type(name="Bad", base_type=str)


def test_name_falls_back_to_base_type():
    class Named(BaseModel):
        v: int = 0

    model = build_model_type(base_type=Named)
    assert model.__name__ == "Named"


def _make_finding_class():
    """Two calls return two *distinct* classes with identical name + shape."""

    class Finding(BaseModel):
        claim: str

    return Finding


def test_distinct_same_shaped_classes_are_not_cross_wired():
    # Regression: the former global model cache cross-wired two distinct classes
    # of the same name + shape, breaking downstream isinstance checks.
    F1 = _make_finding_class()
    F2 = _make_finding_class()
    assert F1 is not F2

    # Build a model whose "finding" field is annotated to each distinct class.
    m1 = build_model_type(name="Bundle", field_models=[FieldModel(F1, name="finding")])
    m2 = build_model_type(name="Bundle", field_models=[FieldModel(F2, name="finding")])

    assert m1 is not m2
    inst2 = m2.model_validate({"finding": {"claim": "x"}})
    assert isinstance(inst2.finding, F2)
    assert not isinstance(inst2.finding, F1)
