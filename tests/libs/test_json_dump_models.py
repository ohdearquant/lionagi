"""Tests for lionagi/ln/_json_dump.py JSON serialization utilities."""

from __future__ import annotations

import datetime as dt
from enum import Enum
from pathlib import Path

import orjson

from lionagi.ln._json_dump import (
    get_orjson_default,
    json_dumpb,
    json_dumps,
    json_lines_iter,
    make_options,
)


# Test fixtures and helpers
class Color(Enum):
    RED = 1
    GREEN = 2
    BLUE = 3


class DummyModel:
    """Test model with model_dump method."""

    def model_dump(self):
        return {"field": "value"}


class DummyDict:
    """Test model with dict method."""

    def dict(self):
        return {"data": "test"}


class FailingModel:
    """Model that raises exception on model_dump."""

    def model_dump(self):
        raise ValueError("Intentional failure")


class ComplexObject:
    """Non-serializable object for testing safe fallback."""

    def __init__(self, value):
        self.value = value

    def __repr__(self):
        return f"ComplexObject(value={self.value})"


def test_make_options_default():
    """Test make_options with defaults."""
    opt = make_options()
    assert opt == 0


def test_make_options_pretty():
    """Test make_options with pretty printing."""
    opt = make_options(pretty=True)
    assert opt & orjson.OPT_INDENT_2


def test_make_options_sort_keys():
    """Test make_options with sorted keys."""
    opt = make_options(sort_keys=True)
    assert opt & orjson.OPT_SORT_KEYS


def test_make_options_append_newline():
    """Test make_options with append newline."""
    opt = make_options(append_newline=True)
    assert opt & orjson.OPT_APPEND_NEWLINE


def test_make_options_naive_utc():
    """Test make_options with naive UTC."""
    opt = make_options(naive_utc=True)
    assert opt & orjson.OPT_NAIVE_UTC


def test_make_options_utc_z():
    """Test make_options with UTC Z."""
    opt = make_options(utc_z=True)
    assert opt & orjson.OPT_UTC_Z


def test_make_options_passthrough_datetime():
    """Test make_options with passthrough datetime."""
    opt = make_options(passthrough_datetime=True)
    assert opt & orjson.OPT_PASSTHROUGH_DATETIME


def test_make_options_allow_non_str_keys():
    """Test make_options with non-string keys."""
    opt = make_options(allow_non_str_keys=True)
    assert opt & orjson.OPT_NON_STR_KEYS


def test_make_options_combined():
    """Test make_options with multiple flags."""
    opt = make_options(pretty=True, sort_keys=True, append_newline=True)
    assert opt & orjson.OPT_INDENT_2
    assert opt & orjson.OPT_SORT_KEYS
    assert opt & orjson.OPT_APPEND_NEWLINE


def test_custom_default_function():
    """Test providing custom default function."""

    def custom_default(obj):
        if isinstance(obj, ComplexObject):
            return {"custom": obj.value}
        raise TypeError("Not serializable")

    obj = ComplexObject("test")
    result = json_dumps(obj, default=custom_default)
    data = orjson.loads(result)
    assert data == {"custom": "test"}


def test_custom_options():
    """Test providing custom options."""
    opt = orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2
    result = json_dumpb({"b": 2, "a": 1}, options=opt)

    result_str = result.decode("utf-8")
    assert result_str.index('"a"') < result_str.index('"b"')
    assert "\n" in result_str


def test_get_orjson_default_with_order():
    """Test get_orjson_default with custom type order."""

    class CustomType:
        pass

    default = get_orjson_default(
        order=[CustomType],
        additional={CustomType: lambda x: "custom"},
    )

    obj = CustomType()
    result = default(obj)
    assert result == "custom"


def test_get_orjson_default_extend_default():
    """Test get_orjson_default with extend_default."""

    class CustomType:
        pass

    default = get_orjson_default(
        order=[CustomType],
        additional={CustomType: lambda x: "custom"},
        extend_default=True,
    )

    path = Path("/tmp/test")
    result = default(path)
    assert result == "/tmp/test"


def test_get_orjson_default_no_extend():
    """Test get_orjson_default without extend_default."""

    class CustomType:
        pass

    default = get_orjson_default(
        order=[CustomType],
        additional={CustomType: lambda x: "custom"},
        extend_default=False,
    )

    obj = CustomType()
    result = default(obj)
    assert result == "custom"


def test_passthrough_datetime_option():
    """Test passthrough_datetime option."""
    dt_val = dt.datetime(2024, 1, 1, 12, 0, 0)

    result = json_dumps(dt_val, passthrough_datetime=True)
    data = orjson.loads(result)
    assert isinstance(data, str)
    assert "2024-01-01" in data


def test_json_lines_iter_basic():
    """Test json_lines_iter with basic data."""
    data = [{"a": 1}, {"b": 2}, {"c": 3}]
    lines = list(json_lines_iter(data))

    assert len(lines) == 3
    for line in lines:
        assert isinstance(line, bytes)
        assert line.endswith(b"\n")


def test_json_lines_iter_with_sets():
    """Test json_lines_iter with sets."""
    data = [{"values": {1, 2, 3}}, {"values": {4, 5}}]
    lines = list(json_lines_iter(data, deterministic_sets=True))

    assert len(lines) == 2
    for line in lines:
        obj = orjson.loads(line)
        assert isinstance(obj["values"], list)


def test_json_lines_iter_with_custom_default():
    """Test json_lines_iter with custom default function."""

    def custom_default(obj):
        if isinstance(obj, ComplexObject):
            return obj.value
        raise TypeError("Not serializable")

    data = [ComplexObject(1), ComplexObject(2)]
    lines = list(json_lines_iter(data, default=custom_default))

    assert len(lines) == 2
    assert orjson.loads(lines[0]) == 1
    assert orjson.loads(lines[1]) == 2


def test_json_lines_iter_with_options():
    """Test json_lines_iter with custom options."""
    opt = orjson.OPT_SORT_KEYS
    data = [{"b": 2, "a": 1}, {"d": 4, "c": 3}]
    lines = list(json_lines_iter(data, options=opt))

    assert len(lines) == 2
    for line in lines:
        assert line.endswith(b"\n")


def test_json_lines_iter_empty():
    """Test json_lines_iter with empty iterable."""
    lines = list(json_lines_iter([]))
    assert len(lines) == 0
