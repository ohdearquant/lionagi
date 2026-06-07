import json
from collections import OrderedDict

import pytest
from pydantic import BaseModel

# Import the functions to be tested
from lionagi.utils import to_dict


# Mock classes and functions for testing
class MockModelWithModelDump:
    def model_dump(self):
        return {"key": "value"}


class MockObjectWithToDict:
    def to_dict(self):
        return {"key": "value"}


class MockObjectWithDict:
    def dict(self):
        return {"key": "value"}


class MockObjectWithJson:
    def json(self):
        return '{"key": "value"}'


def mock_xml_parser(xml_string):
    return {"root": {"child": "value"}}


# Test cases
def test_to_dict_with_dict_input():
    input_dict = {"a": 1, "b": 2}
    assert to_dict(input_dict) == input_dict


def test_to_dict_with_none_input():
    assert to_dict(None) == {}


def test_to_dict_with_mapping():
    input_map = OrderedDict([("a", 1), ("b", 2)])
    assert to_dict(input_map) == {"a": 1, "b": 2}


@pytest.mark.parametrize(
    "input_str, expected",
    [
        ('{"a": 1, "b": 2}', {"a": 1, "b": 2}),
        ("", {}),
    ],
)
def test_to_dict_with_json_string(input_str, expected):
    assert to_dict(input_str) == expected


def test_to_dict_with_invalid_json_string():
    with pytest.raises(json.JSONDecodeError):
        to_dict("{invalid_json}")


def test_to_dict_with_set():
    input_set = {1, 2, 3}
    expected = {1: 1, 2: 2, 3: 3}
    assert to_dict(input_set) == expected


def test_to_dict_with_sequence():
    input_list = [1, 2, 3]
    expected = {0: 1, 1: 2, 2: 3}
    assert to_dict(input_list) == expected


def test_to_dict_with_model_dump():
    obj = MockModelWithModelDump()
    assert to_dict(obj, use_model_dump=True) == {"key": "value"}


@pytest.mark.parametrize(
    "obj_class",
    [
        MockObjectWithToDict,
        MockObjectWithDict,
        MockObjectWithJson,
    ],
)
def test_to_dict_with_custom_methods(obj_class):
    obj = obj_class()
    assert to_dict(obj) == {"key": "value"}


def test_to_dict_with_object_dict():
    class SimpleObject:
        def __init__(self):
            self.a = 1
            self.b = 2

    obj = SimpleObject()
    assert to_dict(obj) == {"a": 1, "b": 2}


def test_to_dict_with_pydantic_model():
    class PydanticModel(BaseModel):
        a: int
        b: str

    model = PydanticModel(a=1, b="test")
    assert to_dict(model) == {"a": 1, "b": "test"}


def test_to_dict_with_fuzzy_parse():
    fuzzy_json = "{'a': 1, 'b': 2}"  # Invalid JSON, but can be fuzzy parsed
    assert to_dict(fuzzy_json, fuzzy_parse=True) == {"a": 1, "b": 2}


def test_to_dict_with_custom_parser():
    def custom_parser(s):
        return {"parsed": s}

    assert to_dict("test", parser=custom_parser) == {"parsed": "test"}


def test_to_dict_with_suppress():
    assert to_dict("{invalid_json}", suppress=True) == {}


# Additional edge cases and error handling tests
def test_to_dict_with_empty_mapping():
    assert to_dict(OrderedDict()) == {}


def test_to_dict_with_nested_structures():
    nested = {"a": [1, 2, {"b": 3}], "c": {"d": 4}}
    assert to_dict(nested) == nested


def test_to_dict_with_custom_json_decoder():
    class CustomDecoder(json.JSONDecoder):
        def decode(self, s):
            result = super().decode(s)
            return {"custom": result}

    json_string = '{"a": 1}'
    assert to_dict(json_string, cls=CustomDecoder) == {"custom": {"a": 1}}


# Performance test (optional, depending on your needs)
@pytest.mark.performance
def test_to_dict_performance():
    import time

    large_dict = {str(i): i for i in range(10000)}
    start_time = time.time()
    result = to_dict(large_dict)
    end_time = time.time()
    assert (end_time - start_time) < 0.1  # Assuming it should take less than 0.1 seconds
    assert result == large_dict
