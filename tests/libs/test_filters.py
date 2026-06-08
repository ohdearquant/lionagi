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


# -- exception handling: FieldRef-safe vs user-predicate visible ------------


def test_field_ref_filter_safe_on_missing_field():
    # A FieldRef comparison on a payload lacking the field is a quiet non-match.
    flower = Spec(str, name="flower_name")
    flt = flower.q == "rose"
    assert flt.safe is True
    assert flt.matches(Bundle(flower_name="rose")) == [Bundle(flower_name="rose")]
    # a model without the field — safe, returns no match, raises nothing
    assert flt.matches(Finding(claim="x")) == []


def test_user_predicate_exception_is_logged_not_silent(caplog):
    import logging

    def boom(_payload):
        raise RuntimeError("predicate bug")

    flt = as_filter(boom)
    assert flt.safe is False
    with caplog.at_level(logging.WARNING, logger="lionagi.ln.types.filters"):
        assert flt.matches(Finding(claim="x")) == []  # no crash, no match
    assert any("raised on payload" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Edge cases: spec group "libs"
# ---------------------------------------------------------------------------

import dataclasses


@dataclasses.dataclass
class Point:
    x: float
    y: float


def test_filter_deep_composition_three_levels():
    """((a & b) | c) & d — three levels of nesting should evaluate correctly."""
    novelty = Spec(float, name="novelty")
    flower = Spec(str, name="flower_name")

    a = novelty.q > 0.2
    b = novelty.q < 0.9
    c = flower.q == "rose"
    d = novelty.q >= 0.0

    composed = ((a & b) | c) & d

    assert composed(Bundle(novelty=0.5, flower_name="tulip")) is True
    assert composed(Bundle(novelty=0.0, flower_name="rose")) is True
    assert composed(Bundle(novelty=0.0, flower_name="tulip")) is False


def test_spec_filter_lt_gt_on_none_field_value():
    """SpecFilter comparison against a None field value should be a quiet non-match.
    Uses a dict payload so the field can hold None without Pydantic validation."""
    novelty = Spec(float, name="novelty")

    gt_filter = novelty.q > 0.5
    lt_filter = novelty.q < 0.5

    # Plain dict payload where the field is None — comparison would raise TypeError
    # but the safe=True SpecFilter should swallow it and return False.
    payload = {"novelty": None}
    assert gt_filter(payload) is False
    assert lt_filter(payload) is False


def test_type_filter_scans_nested_pydantic_models():
    """TypeFilter scans one level of model_fields; a value nested two levels deep
    is not found at the outer level — this documents the intended one-level scan."""

    class Inner(BaseModel):
        finding: Finding | None = None

    class Outer(BaseModel):
        inner: Inner | None = None
        score: float = 0.0

    fnd = Finding(claim="deep")
    outer = Outer(inner=Inner(finding=fnd))

    f = TypeFilter(Finding)
    # Outer has 'inner' (Inner) and 'score' (float) — neither is a Finding
    assert f.matches(outer) == []

    # One level down, the finding is directly accessible
    inner_matches = f.matches(outer.inner)
    assert len(inner_matches) == 1
    assert inner_matches[0] is fnd


def test_as_filter_with_async_callable_wraps_in_spec_filter():
    """as_filter wraps a coroutine function in a SpecFilter without raising.
    The predicate is not awaited — the coroutine object (truthy) is treated as
    a match, but this documents that async predicates need explicit handling."""
    import warnings

    async def async_pred(payload):
        return True

    flt = as_filter(async_pred)
    assert isinstance(flt, SpecFilter)

    # Suppress the "coroutine never awaited" ResourceWarning from the test itself.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        result = flt.matches(Finding(claim="x"))

    assert isinstance(result, list)


def test_spec_filter_matches_on_plain_dict():
    """SpecFilter.matches should work when the payload is a plain dict."""
    flower = Spec(str, name="flower_name")
    flt = flower.q == "rose"

    assert flt({"flower_name": "rose"}) is True
    assert flt({"flower_name": "tulip"}) is False
    assert flt({"other_key": "rose"}) is False


def test_type_filter_matches_on_plain_dict():
    """TypeFilter.matches on a plain dict uses dict as field_values (no model_fields)."""
    f = TypeFilter(str)
    result = f.matches({"key": "hello", "num": 42})
    assert "hello" in result
    assert 42 not in result


def test_spec_filter_matches_on_dataclass():
    """SpecFilter.matches on a plain dataclass uses attribute access via resolve_path."""
    novelty = Spec(float, name="x")
    flt = novelty.q > 1.0
    p = Point(x=2.0, y=3.0)
    assert flt(p) is True

    p2 = Point(x=0.5, y=0.0)
    assert flt(p2) is False
