# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Filter DSL: TypeFilter scans payload fields by type; SpecFilter (built via
Spec.q) matches a named field by value; both compose with & | ~.
"""

from __future__ import annotations

from pydantic import BaseModel

from lionagi.ln.types import Filter, Spec, SpecFilter, TypeFilter, as_filter


class Finding(BaseModel):
    claim: str


class Bundle(BaseModel):
    finding: Finding | None = None
    flower_name: str | None = None
    novelty: float = 0.0


# -- TypeFilter -------------------------------------------------------------


def test_type_filter_direct_instance():
    f = TypeFilter(Finding)
    fnd = Finding(claim="x")
    assert f.matches(fnd) == [fnd]
    assert f(fnd) is True


def test_type_filter_scans_fields():
    f = TypeFilter(Finding)
    b = Bundle(finding=Finding(claim="x"))
    matched = f.matches(b)
    assert len(matched) == 1 and matched[0].claim == "x"


def test_type_filter_no_match():
    assert TypeFilter(Finding).matches(Bundle(flower_name="rose")) == []


# -- SpecFilter via Spec.q --------------------------------------------------


def test_spec_q_returns_field_ref():
    flower = Spec(str, name="flower_name")
    cond = flower.q == "rose"
    assert isinstance(cond, SpecFilter)


def test_spec_filter_value_match():
    flower = Spec(str, name="flower_name")
    cond = flower.q == "rose"
    assert cond(Bundle(flower_name="rose")) is True
    assert cond(Bundle(flower_name="tulip")) is False
    # missing field → no match, never raises
    assert cond(Finding(claim="x")) is False


def test_spec_filter_comparison_ops():
    novelty = Spec(float, name="novelty")
    assert (novelty.q > 0.5)(Bundle(novelty=0.9)) is True
    assert (novelty.q > 0.5)(Bundle(novelty=0.1)) is False
    assert (novelty.q >= 0.9)(Bundle(novelty=0.9)) is True


def test_spec_filter_is_in_and_present():
    flower = Spec(str, name="flower_name")
    assert flower.q.is_in({"rose", "tulip"})(Bundle(flower_name="rose")) is True
    assert flower.q.present()(Bundle(flower_name="rose")) is True
    assert flower.q.present()(Bundle()) is False


# -- composition ------------------------------------------------------------


def test_filter_composition():
    flower = Spec(str, name="flower_name")
    novelty = Spec(float, name="novelty")
    both = (flower.q == "rose") & (novelty.q > 0.5)
    assert both(Bundle(flower_name="rose", novelty=0.9)) is True
    assert both(Bundle(flower_name="rose", novelty=0.1)) is False

    either = (flower.q == "rose") | (flower.q == "tulip")
    assert either(Bundle(flower_name="tulip")) is True
    assert (~(flower.q == "rose"))(Bundle(flower_name="tulip")) is True


def test_as_filter_coercion():
    assert isinstance(as_filter(Finding), TypeFilter)
    assert isinstance(as_filter(lambda p: True), Filter)
    flower = Spec(str, name="flower_name")
    assert isinstance(as_filter(flower.q == "rose"), SpecFilter)


def test_field_ref_not_hashable():
    flower = Spec(str, name="flower_name")
    # __eq__ returns a Filter, so FieldRef must not be hashable
    import pytest

    with pytest.raises(TypeError):
        {flower.q}  # noqa: B018
