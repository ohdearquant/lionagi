# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Non-finite floats must fail serialization rather than turn into null."""

import math

import pytest

from lionagi.ln import json_dumpb, json_dumps, json_lines_iter
from lionagi.ln._hash import compute_hash
from lionagi.protocols.generic.element import Element

NON_FINITE = [float("inf"), float("-inf"), float("nan")]


class _Scored(Element):
    value: float = 0.0


@pytest.mark.parametrize("bad", NON_FINITE)
def test_json_dumps_rejects_non_finite(bad):
    with pytest.raises(ValueError, match="non-finite float at \\$.v"):
        json_dumps({"v": bad})


@pytest.mark.parametrize("bad", NON_FINITE)
def test_json_dumpb_rejects_non_finite(bad):
    with pytest.raises(ValueError, match="non-finite float"):
        json_dumpb({"v": bad})


def test_error_reports_the_path_to_the_offending_value():
    with pytest.raises(ValueError, match="\\$\\.a\\[1\\]\\.b\\[0\\]"):
        json_dumps({"a": [1.0, {"b": [float("inf")]}]})


def test_non_finite_inside_a_tuple_is_rejected():
    with pytest.raises(ValueError, match="\\$\\.t\\[1\\]"):
        json_dumps({"t": (1.0, float("-inf"))})


def test_non_finite_inside_a_set_is_rejected():
    with pytest.raises(ValueError, match="non-finite float"):
        json_dumps({"s": {float("nan")}}, deterministic_sets=True)


def test_non_finite_dict_key_is_rejected():
    with pytest.raises(ValueError, match="non-finite float"):
        json_dumps({float("inf"): 1}, allow_non_str_keys=True)


def test_non_finite_reached_through_the_default_hook_is_rejected():
    """A nested Element is expanded by default(); the check follows it."""
    with pytest.raises(ValueError, match="\\$\\.e\\.value"):
        json_dumps({"e": _Scored(value=float("inf"))})


def test_safe_fallback_does_not_suppress_the_error():
    with pytest.raises(ValueError, match="non-finite float"):
        json_dumps({"v": float("inf")}, safe_fallback=True)


def test_ndjson_stream_rejects_non_finite():
    stream = json_lines_iter([{"a": 1.0}, {"b": float("inf")}])
    assert next(stream) == b'{"a":1.0}\n'
    with pytest.raises(ValueError, match="non-finite float"):
        next(stream)


def test_compute_hash_rejects_non_finite():
    """Without this, inf and None hash identically."""
    with pytest.raises(ValueError, match="non-finite float"):
        compute_hash({"v": float("inf")})


@pytest.mark.parametrize("bad", NON_FINITE)
def test_element_to_json_rejects_non_finite(bad):
    with pytest.raises(ValueError, match="\\$\\.value"):
        _Scored(value=bad).to_json()


@pytest.mark.parametrize("mode", ["json", "db"])
def test_element_to_dict_rejects_non_finite(mode):
    with pytest.raises(ValueError, match="\\$\\.value"):
        _Scored(value=float("inf")).to_dict(mode=mode)


# --- the guard must not disturb anything that is legitimately serializable ---


def test_genuine_nulls_still_serialize():
    assert json_dumps({"a": None, "b": 1.5}) == '{"a":null,"b":1.5}'


def test_string_containing_null_still_serializes():
    assert json_dumps({"s": "null"}) == '{"s":"null"}'


def test_finite_float_key_still_serializes():
    assert json_dumps({1.5: 1, "z": None}, allow_non_str_keys=True) == '{"1.5":1,"z":null}'


def test_finite_element_round_trips():
    element = _Scored(value=1.5)
    restored = _Scored.from_json(element.to_json())
    assert restored.value == 1.5
    assert restored.id == element.id


def test_extreme_but_finite_floats_are_untouched():
    payload = {"big": 1.7976931348623157e308, "small": 5e-324, "neg": -0.0}
    restored = json_dumps(payload, as_loaded=True)
    assert restored["big"] == 1.7976931348623157e308
    assert restored["small"] == 5e-324
    assert math.copysign(1, restored["neg"]) == -1
