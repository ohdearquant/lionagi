# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Non-finite floats fail serialization rather than turn into null, where asked."""

import dataclasses
import enum
import math

import orjson
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
        json_dumps({"v": bad}, check_non_finite=True)


@pytest.mark.parametrize("bad", NON_FINITE)
def test_json_dumpb_rejects_non_finite(bad):
    with pytest.raises(ValueError, match="non-finite float"):
        json_dumpb({"v": bad}, check_non_finite=True)


def test_error_reports_the_path_to_the_offending_value():
    with pytest.raises(ValueError, match="\\$\\.a\\[1\\]\\.b\\[0\\]"):
        json_dumps({"a": [1.0, {"b": [float("inf")]}]}, check_non_finite=True)


def test_non_finite_inside_a_tuple_is_rejected():
    with pytest.raises(ValueError, match="\\$\\.t\\[1\\]"):
        json_dumps({"t": (1.0, float("-inf"))}, check_non_finite=True)


def test_non_finite_inside_a_set_is_rejected():
    with pytest.raises(ValueError, match="non-finite float"):
        json_dumps({"s": {float("nan")}}, deterministic_sets=True, check_non_finite=True)


def test_non_finite_dict_key_is_rejected():
    with pytest.raises(ValueError, match="non-finite float"):
        json_dumps({float("inf"): 1}, allow_non_str_keys=True, check_non_finite=True)


def test_non_finite_reached_through_the_default_hook_is_rejected():
    """A nested Element is expanded by default(); the check follows it."""
    with pytest.raises(ValueError, match="\\$\\.e\\.value"):
        json_dumps({"e": _Scored(value=float("inf"))}, check_non_finite=True)


def test_safe_fallback_does_not_suppress_the_error():
    with pytest.raises(ValueError, match="non-finite float"):
        json_dumps({"v": float("inf")}, safe_fallback=True, check_non_finite=True)


def test_ndjson_stream_rejects_non_finite():
    stream = json_lines_iter([{"a": 1.0}, {"b": float("inf")}], check_non_finite=True)
    assert next(stream) == b'{"a":1.0}\n'
    with pytest.raises(ValueError, match="non-finite float"):
        next(stream)


def test_compute_hash_does_not_check_and_collides_on_non_finite():
    """Hashing does not pay for the check, so inf and None still hash alike.

    Recorded rather than fixed: compute_hash runs on every dict lookup that needs a
    stable key, and the check costs a full traversal of the payload. A caller that
    needs the distinction serializes with check_non_finite=True before hashing.
    """
    assert compute_hash({"v": float("inf")}) == compute_hash({"v": None})


@pytest.mark.parametrize("bad", NON_FINITE)
def test_element_to_json_rejects_non_finite(bad):
    with pytest.raises(ValueError, match="\\$\\.value"):
        _Scored(value=bad).to_json()


@pytest.mark.parametrize("mode", ["json", "db"])
def test_element_to_dict_rejects_non_finite(mode):
    with pytest.raises(ValueError, match="\\$\\.value"):
        _Scored(value=float("inf")).to_dict(mode=mode)


# what the check costs is only paid when it is asked for


@pytest.mark.parametrize("bad", NON_FINITE)
def test_check_is_off_by_default_and_orjson_behaviour_stands(bad):
    """Unasked, the dump keeps orjson's semantics: the value is written as null.

    This is the cost boundary. Running the check on every dump means traversing the
    whole payload whenever it holds any null at all, which ordinary payloads do.
    """
    assert json_dumps({"v": bad}) == '{"v":null}'
    assert json_dumpb({"v": bad}) == b'{"v":null}'


def test_ndjson_stream_does_not_check_by_default():
    stream = json_lines_iter([{"a": 1.0}, {"b": float("inf")}])
    assert next(stream) == b'{"a":1.0}\n'
    assert next(stream) == b'{"b":null}\n'


def test_db_json_column_rejects_non_finite():
    """The stored row is what cannot be repaired later, so this path checks."""
    from lionagi.state.db import _to_json_column

    assert _to_json_column({"v": 1.5}) == '{"v":1.5}'
    with pytest.raises(ValueError, match="non-finite float at \\$\\.v"):
        _to_json_column({"v": float("inf")})


# orjson.Fragment is an opaque boundary the check cannot see past


def test_fragment_contents_are_not_checked():
    """A Fragment is bytes orjson copies out verbatim, never calling default().

    Nothing on this side of the call can tell a null the caller wrote from a null a
    non-finite float would have produced, because by the time the Fragment exists
    there is no float left to inspect. Recorded so a later change does not mistake
    the silence for coverage.
    """
    payload = {"raw": orjson.Fragment(b'{"v":null}')}
    assert json_dumpb(payload, check_non_finite=True) == b'{"raw":{"v":null}}'

    # orjson does not parse Fragment bytes either, so it will emit a token that is
    # not valid JSON if the caller supplies one.
    invalid = {"raw": orjson.Fragment(b'{"v":NaN}')}
    assert json_dumpb(invalid, check_non_finite=True) == b'{"raw":{"v":NaN}}'

    # A non-finite float outside the Fragment is still found.
    with pytest.raises(ValueError, match="non-finite float at \\$\\.v"):
        json_dumpb({"raw": orjson.Fragment(b"{}"), "v": float("inf")}, check_non_finite=True)


# the guard must not disturb anything that is legitimately serializable


def test_genuine_nulls_still_serialize():
    assert json_dumps({"a": None, "b": 1.5}) == '{"a":null,"b":1.5}'
    assert json_dumps({"a": None, "b": 1.5}, check_non_finite=True) == '{"a":null,"b":1.5}'


def test_string_containing_null_still_serializes():
    """A `null` in a string value trips the cheap pre-scan; the walk must clear it."""
    assert json_dumps({"s": "null"}) == '{"s":"null"}'
    assert json_dumps({"s": "null"}, check_non_finite=True) == '{"s":"null"}'


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


# forms orjson encodes natively, which never reach the default() hook


@pytest.mark.parametrize("bad", NON_FINITE)
def test_dataclass_field_rejects_non_finite(bad):
    @dataclasses.dataclass
    class Measurement:
        value: float

    with pytest.raises(ValueError, match="non-finite float at \\$\\.value"):
        json_dumpb(Measurement(bad), check_non_finite=True)


def test_nested_dataclass_reports_its_path():
    @dataclasses.dataclass
    class Inner:
        value: float

    @dataclasses.dataclass
    class Outer:
        inner: Inner

    with pytest.raises(ValueError, match="\\$\\.readings\\[0\\]\\.inner\\.value"):
        json_dumpb({"readings": [Outer(Inner(float("nan")))]}, check_non_finite=True)


def test_finite_dataclass_still_serializes():
    @dataclasses.dataclass
    class Measurement:
        value: float
        note: str | None

    assert json_dumpb(Measurement(1.5, None)) == b'{"value":1.5,"note":null}'


@pytest.mark.parametrize("bad", NON_FINITE)
def test_enum_float_value_rejects_non_finite(bad):
    limit = enum.Enum("_Limit", {"EDGE": bad})

    with pytest.raises(ValueError, match="non-finite float at \\$\\.limit"):
        json_dumpb({"limit": limit.EDGE}, check_non_finite=True)


def test_numpy_array_rejects_non_finite():
    np = pytest.importorskip("numpy")

    with pytest.raises(ValueError, match="non-finite float at \\$\\[1\\]"):
        json_dumpb(
            np.array([1.0, float("nan")]),
            options=orjson.OPT_SERIALIZE_NUMPY,
            check_non_finite=True,
        )


def test_numpy_multidimensional_array_reports_its_index():
    np = pytest.importorskip("numpy")

    with pytest.raises(ValueError, match="\\$\\.grid\\[1\\]\\[1\\]"):
        json_dumpb(
            {"grid": np.array([[1.0, 2.0], [3.0, float("inf")]])},
            options=orjson.OPT_SERIALIZE_NUMPY,
            check_non_finite=True,
        )


@pytest.mark.parametrize("dtype_name", ["float16", "float32", "float64"])
def test_numpy_scalar_rejects_non_finite(dtype_name):
    np = pytest.importorskip("numpy")
    scalar = np.dtype(dtype_name).type(float("inf"))

    with pytest.raises(ValueError, match="non-finite float at \\$\\.v"):
        json_dumpb({"v": scalar}, options=orjson.OPT_SERIALIZE_NUMPY, check_non_finite=True)


def test_finite_numpy_arrays_still_serialize():
    np = pytest.importorskip("numpy")
    opt = orjson.OPT_SERIALIZE_NUMPY

    assert json_dumpb(np.array([1.0, 2.5]), options=opt) == b"[1.0,2.5]"
    # Integer and boolean dtypes cannot be non-finite and must not be probed.
    assert json_dumpb(np.array([1, 2]), options=opt) == b"[1,2]"
    assert json_dumpb(np.array([True, False]), options=opt) == b"[true,false]"


def test_dataclass_under_passthrough_follows_the_default_hook():
    """OPT_PASSTHROUGH_DATACLASS routes dataclasses to default(), so the walk must too."""

    @dataclasses.dataclass
    class Measurement:
        value: float

    with pytest.raises(ValueError, match="non-finite float at \\$\\.value"):
        json_dumpb(
            Measurement(float("inf")),
            default=lambda o: {"value": o.value},
            options=orjson.OPT_PASSTHROUGH_DATACLASS,
            check_non_finite=True,
        )
