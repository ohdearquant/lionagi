# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Strict-loader migration gates; these do not approve stdlib call-site changes."""

from __future__ import annotations

import json
import math

import orjson
import pytest

from lionagi import ln


def test_json_decode_error_keeps_the_native_class_and_stdlib_compatibility():
    assert ln.JSONDecodeError is orjson.JSONDecodeError
    assert issubclass(ln.JSONDecodeError, json.JSONDecodeError)
    assert issubclass(ln.JSONDecodeError, ValueError)


@pytest.mark.parametrize("input_type", (str, bytes, bytearray, memoryview))
def test_json_loads_accepts_native_utf8_inputs(input_type):
    text = '{"text":"café","zero":0,"flag":false,"items":[null,"海"]}'
    data = text if input_type is str else input_type(text.encode("utf-8"))

    result = ln.json_loads(data)
    assert result == {
        "text": "café",
        "zero": 0,
        "flag": False,
        "items": [None, "海"],
    }
    assert result["flag"] is False
    assert type(result["zero"]) is int
    assert result["items"][0] is None


@pytest.mark.parametrize(
    "data",
    ("NaN", "Infinity", "-Infinity", "[NaN]", "1e999", '{"x": }', "```json\n{}\n```"),
)
def test_json_loads_refuses_non_json_and_non_finite_input(data):
    with pytest.raises(ln.JSONDecodeError):
        ln.json_loads(data)


@pytest.mark.parametrize("encoding", ("utf-16", "utf-32", "invalid-utf8"))
def test_json_loads_refuses_non_utf8_bytes(encoding):
    data = b'"\xff"' if encoding == "invalid-utf8" else '{"x":1}'.encode(encoding)

    with pytest.raises(ln.JSONDecodeError):
        ln.json_loads(data)


@pytest.mark.parametrize("data", ('{"x": }', '\n{"é": }', b'{"x":]'))
def test_json_loads_keeps_native_error_fields(data):
    with pytest.raises(orjson.JSONDecodeError) as native:
        orjson.loads(data)
    with pytest.raises(ln.JSONDecodeError) as shared:
        ln.json_loads(data)

    assert type(shared.value) is type(native.value)
    assert shared.value.args == native.value.args
    for field in ("msg", "doc", "pos", "lineno", "colno"):
        assert getattr(shared.value, field) == getattr(native.value, field)
    assert isinstance(shared.value.msg, str)
    assert isinstance(shared.value.doc, str)
    assert isinstance(shared.value.pos, int)


@pytest.mark.parametrize(
    "data",
    (
        "18446744073709551615",
        "18446744073709551616",
        "-9223372036854775808",
        "-9223372036854775809",
        "0.1",
        "-0.0",
        "123456789012345678901234567890",
    ),
)
def test_json_loads_keeps_native_numeric_results(data):
    try:
        native = orjson.loads(data)
    except orjson.JSONDecodeError as native_error:
        with pytest.raises(ln.JSONDecodeError) as shared_error:
            ln.json_loads(data)
        assert type(shared_error.value) is type(native_error)
        assert shared_error.value.args == native_error.args
        return
    shared = ln.json_loads(data)

    assert type(shared) is type(native)
    assert shared == native
    if isinstance(native, float):
        assert math.copysign(1.0, shared) == math.copysign(1.0, native)


def test_json_loads_argument_is_positional_only():
    with pytest.raises(TypeError):
        ln.json_loads(data="{}")


@pytest.mark.parametrize(
    "case",
    (
        "nan",
        "infinity",
        "negative-infinity",
        "utf-16-bytes",
        "utf-32-bytes",
        "beyond-u64",
        "u64-maximum",
        "float",
        "negative-zero",
    ),
)
def test_native_encoder_fixtures_with_the_shared_decoder(case):
    # These pin the native codec's boundaries for the issue's migration gate;
    # they do not authorize changing any stdlib writer to these semantics.
    value = {
        "nan": float("nan"),
        "infinity": float("inf"),
        "negative-infinity": float("-inf"),
        "utf-16-bytes": '{"x":1}'.encode("utf-16"),
        "utf-32-bytes": '{"x":1}'.encode("utf-32"),
        "beyond-u64": 18446744073709551616,
        "u64-maximum": 18446744073709551615,
        "float": 1.25,
        "negative-zero": -0.0,
    }[case]
    if case in ("utf-16-bytes", "utf-32-bytes", "beyond-u64"):
        with pytest.raises(orjson.JSONEncodeError):
            orjson.dumps(value)
        return

    encoded = orjson.dumps(value)
    decoded = ln.json_loads(encoded)
    if case in ("nan", "infinity", "negative-infinity"):
        assert encoded == b"null"
        assert decoded is None
    else:
        assert (
            encoded
            == {
                "u64-maximum": b"18446744073709551615",
                "float": b"1.25",
                "negative-zero": b"-0.0",
            }[case]
        )
        assert type(decoded) is type(value)
        assert decoded == value
        if case == "negative-zero":
            assert math.copysign(1.0, decoded) == -1.0
