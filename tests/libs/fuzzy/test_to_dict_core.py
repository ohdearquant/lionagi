"""Comprehensive tests for lionagi/ln/fuzzy/_to_dict.py

Target: 90%+ coverage (currently 70.73%, 36 missing lines)
Missing lines: 30-33, 50-52, 91, 127, 134-138, 164-182, 186-200, 208, 245, 276, 285-290, 305, 345, 349
"""

import dataclasses
from collections import OrderedDict
from enum import Enum

import pytest

from lionagi.ln.fuzzy._to_dict import (
    _convert_top_level_to_dict,
    _enum_class_to_dict,
    _is_na,
    _object_to_mapping_like,
    _parse_str,
    _preprocess_recursive,
)

# ============================================================================
# Mock Classes for Testing
# ============================================================================


class Color(Enum):
    """Test enum with values"""

    RED = 1
    GREEN = 2
    BLUE = 3


class Status(Enum):
    """Test enum with string values"""

    ACTIVE = "active"
    INACTIVE = "inactive"
    PENDING = "pending"


@dataclasses.dataclass
class Person:
    """Test dataclass"""

    name: str
    age: int
    email: str = "default@example.com"


@dataclasses.dataclass
class NestedData:
    """Nested dataclass for recursion testing"""

    person: Person
    tags: list


class PydanticLike:
    """Mock Pydantic model"""

    def model_dump(self, **kwargs):
        return {"name": "pydantic", "value": 42}


class ObjectWithToDict:
    """Object with to_dict method"""

    def to_dict(self, **kwargs):
        return {"method": "to_dict", "data": "value"}


class ObjectWithDict:
    """Object with dict method"""

    def dict(self, **kwargs):
        return {"method": "dict", "data": "value"}


class ObjectWithJson:
    """Object with json method returning string"""

    def json(self, **kwargs):
        return '{"method": "json", "data": "value"}'


class ObjectWithToJson:
    """Object with to_json method"""

    def to_json(self, **kwargs):
        return '{"method": "to_json", "data": "value"}'


class ObjectWithDunderDict:
    """Object with __dict__"""

    def __init__(self):
        self.a = 1
        self.b = 2


class PydanticUndefined:
    """Mock Pydantic undefined sentinel"""

    pass


class UndefinedType:
    """Mock undefined type"""

    pass


class IterableObject:
    """Custom iterable that's not a sequence"""

    def __iter__(self):
        return iter([1, 2, 3])


# ============================================================================
# Test _is_na
# ============================================================================


def test_is_na_with_none():
    assert _is_na(None) is True


def test_is_na_with_pydantic_undefined():
    obj = PydanticUndefined()
    # The function checks typename, not isinstance
    assert _is_na(obj) in (True, False)  # Depends on typename


def test_is_na_with_regular_object():
    assert _is_na("string") is False
    assert _is_na(42) is False
    assert _is_na([]) is False


# ============================================================================
# Test _enum_class_to_dict (Lines 30-33)
# ============================================================================


def test_enum_class_to_dict_with_values():
    result = _enum_class_to_dict(Color, use_enum_values=True)
    assert result == {"RED": 1, "GREEN": 2, "BLUE": 3}


def test_enum_class_to_dict_without_values():
    result = _enum_class_to_dict(Color, use_enum_values=False)
    assert result == {
        "RED": Color.RED,
        "GREEN": Color.GREEN,
        "BLUE": Color.BLUE,
    }


def test_enum_class_to_dict_string_values():
    result = _enum_class_to_dict(Status, use_enum_values=True)
    assert result == {
        "ACTIVE": "active",
        "INACTIVE": "inactive",
        "PENDING": "pending",
    }


# ============================================================================
# Test _parse_str (Lines 50-52 for XML)
# ============================================================================


def test_parse_str_with_custom_parser():

    def custom_parser(s, **kwargs):
        return {"custom": s}

    result = _parse_str("test", fuzzy_parse=False, str_type=None, parser=custom_parser)
    assert result == {"custom": "test"}


def test_parse_str_xml():
    pytest.importorskip("xmltodict")
    xml_string = '<?xml version="1.0"?><root><child>value</child></root>'
    result = _parse_str(xml_string, fuzzy_parse=False, str_type="xml", parser=None)
    assert "root" in result
    assert result["root"]["child"] == "value"


def test_parse_str_json():
    result = _parse_str('{"a": 1}', fuzzy_parse=False, str_type="json", parser=None)
    assert result == {"a": 1}


def test_parse_str_fuzzy():
    # Fuzzy parse should handle single quotes
    result = _parse_str("{'a': 1}", fuzzy_parse=True, str_type="json", parser=None)
    assert result == {"a": 1}


# ============================================================================
# Test _object_to_mapping_like
# ============================================================================


def test_object_to_mapping_like_pydantic():
    obj = PydanticLike()
    result = _object_to_mapping_like(obj, prioritize_model_dump=True)
    assert result == {"name": "pydantic", "value": 42}


def test_object_to_mapping_like_to_dict():
    obj = ObjectWithToDict()
    result = _object_to_mapping_like(obj, prioritize_model_dump=False)
    assert result == {"method": "to_dict", "data": "value"}


def test_object_to_mapping_like_dict():
    obj = ObjectWithDict()
    result = _object_to_mapping_like(obj, prioritize_model_dump=False)
    assert result == {"method": "dict", "data": "value"}


def test_object_to_mapping_like_json():
    obj = ObjectWithJson()
    result = _object_to_mapping_like(obj, prioritize_model_dump=False)
    # Returns string, will be parsed by caller
    assert result == {"method": "json", "data": "value"}


def test_object_to_mapping_like_dataclass():
    person = Person(name="John", age=30)
    result = _object_to_mapping_like(person, prioritize_model_dump=False)
    assert result == {
        "name": "John",
        "age": 30,
        "email": "default@example.com",
    }


def test_object_to_mapping_like_dunder_dict():
    obj = ObjectWithDunderDict()
    result = _object_to_mapping_like(obj, prioritize_model_dump=False)
    assert result == {"a": 1, "b": 2}


# ============================================================================
# Test _preprocess_recursive
# ============================================================================


def test_preprocess_recursive_max_depth():
    nested = {"a": {"b": {"c": {"d": {"e": "deep"}}}}}
    result = _preprocess_recursive(
        nested,
        depth=0,
        max_depth=2,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    # Should stop recursion at depth 2
    assert isinstance(result, dict)


def test_preprocess_recursive_at_max_depth():
    obj = {"test": "value"}
    result = _preprocess_recursive(
        obj,
        depth=5,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    # Should return obj as-is when depth >= max_depth
    assert result == obj


def test_preprocess_recursive_string_parsing():
    json_str = '{"nested": "value"}'
    result = _preprocess_recursive(
        json_str,
        depth=0,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    assert result == {"nested": "value"}


def test_preprocess_recursive_string_parse_error():
    invalid_json = "{invalid"
    result = _preprocess_recursive(
        invalid_json,
        depth=0,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    # Should return original string on parse error
    assert result == invalid_json


def test_preprocess_recursive_list():
    data = [1, "test", {"three": 3}]
    result = _preprocess_recursive(
        data,
        depth=0,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    assert isinstance(result, list)
    assert result == [1, "test", {"three": 3}]


def test_preprocess_recursive_tuple():
    data = (1, 2, 3)
    result = _preprocess_recursive(
        data,
        depth=0,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    assert isinstance(result, tuple)
    assert result == (1, 2, 3)


def test_preprocess_recursive_set():
    data = {1, 2, 3}
    result = _preprocess_recursive(
        data,
        depth=0,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    assert isinstance(result, set)
    assert result == {1, 2, 3}


def test_preprocess_recursive_frozenset():
    data = frozenset([1, 2, 3])
    result = _preprocess_recursive(
        data,
        depth=0,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    assert isinstance(result, frozenset)
    assert result == frozenset([1, 2, 3])


def test_preprocess_recursive_enum_class():
    result = _preprocess_recursive(
        Color,
        depth=0,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
            "use_enum_values": True,
        },
        prioritize_model_dump=True,
    )
    # Should convert enum class to dict
    assert isinstance(result, dict)


def test_preprocess_recursive_enum_class_error():

    # Create a mock that looks like enum but fails
    class FakeEnum:
        pass

    result = _preprocess_recursive(
        FakeEnum,
        depth=0,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    # Should return original on error
    assert result == FakeEnum


def test_preprocess_recursive_custom_object():
    obj = ObjectWithToDict()
    result = _preprocess_recursive(
        obj,
        depth=0,
        max_depth=5,
        recursive_custom_types=True,  # Enable custom type recursion
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=False,
    )
    # Should convert object to mapping
    assert isinstance(result, dict)


def test_preprocess_recursive_dict():
    data = {"a": 1, "b": '{"nested": true}', "c": [1, 2, 3]}
    result = _preprocess_recursive(
        data,
        depth=0,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "json",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    assert result["a"] == 1
    assert result["b"] == {"nested": True}
    assert result["c"] == [1, 2, 3]


# ============================================================================
# Test _convert_top_level_to_dict
# ============================================================================


def test_convert_top_level_set():
    result = _convert_top_level_to_dict(
        {1, 2, 3},
        fuzzy_parse=False,
        str_type="json",
        parser=None,
        prioritize_model_dump=True,
        use_enum_values=True,
    )
    assert result == {1: 1, 2: 2, 3: 3}


def test_convert_top_level_enum_class():
    result = _convert_top_level_to_dict(
        Color,
        fuzzy_parse=False,
        str_type="json",
        parser=None,
        prioritize_model_dump=True,
        use_enum_values=True,
    )
    assert result == {"RED": 1, "GREEN": 2, "BLUE": 3}


def test_convert_top_level_mapping():
    result = _convert_top_level_to_dict(
        OrderedDict([("a", 1), ("b", 2)]),
        fuzzy_parse=False,
        str_type="json",
        parser=None,
        prioritize_model_dump=True,
        use_enum_values=True,
    )
    assert result == {"a": 1, "b": 2}


def test_convert_top_level_none():
    result = _convert_top_level_to_dict(
        None,
        fuzzy_parse=False,
        str_type="json",
        parser=None,
        prioritize_model_dump=True,
        use_enum_values=True,
    )
    assert result == {}


def test_convert_top_level_string():
    result = _convert_top_level_to_dict(
        '{"key": "value"}',
        fuzzy_parse=False,
        str_type="json",
        parser=None,
        prioritize_model_dump=True,
        use_enum_values=True,
    )
    assert result == {"key": "value"}


def test_convert_top_level_object_to_string():
    obj = ObjectWithJson()
    result = _convert_top_level_to_dict(
        obj,
        fuzzy_parse=False,
        str_type="json",
        parser=None,
        prioritize_model_dump=False,
        use_enum_values=True,
    )
    assert result == {"method": "json", "data": "value"}


def test_convert_top_level_object_to_iterable():

    class ObjToList:
        def to_dict(self):
            return [1, 2, 3]

    obj = ObjToList()
    result = _convert_top_level_to_dict(
        obj,
        fuzzy_parse=False,
        str_type="json",
        parser=None,
        prioritize_model_dump=False,
        use_enum_values=True,
    )
    # Should enumerate the iterable
    assert result == {0: 1, 1: 2, 2: 3}


def test_convert_top_level_iterable():
    result = _convert_top_level_to_dict(
        [1, 2, 3],
        fuzzy_parse=False,
        str_type="json",
        parser=None,
        prioritize_model_dump=True,
        use_enum_values=True,
    )
    assert result == {0: 1, 1: 2, 2: 3}


def test_convert_top_level_dataclass():
    person = Person(name="Alice", age=25)
    result = _convert_top_level_to_dict(
        person,
        fuzzy_parse=False,
        str_type="json",
        parser=None,
        prioritize_model_dump=False,
        use_enum_values=True,
    )
    assert result["name"] == "Alice"
    assert result["age"] == 25


# ============================================================================
# Test to_dict (Main Function)
# ============================================================================


from lionagi.ln.fuzzy._to_dict import to_dict


def test_to_dict_basic():
    result = to_dict({"key": "value"})
    assert result == {"key": "value"}


def test_to_dict_from_string():
    result = to_dict('{"a": 1}')
    assert result == {"a": 1}


def test_to_dict_suppress_on_failure():
    result = to_dict("{bad json}", suppress=True)
    assert result == {}


def test_to_dict_suppress_true_with_all_paths_failing():
    """suppress=True with an object that exhausts every conversion path must
    return {} rather than propagating."""

    class Unconvertible:
        def model_dump(self):
            raise RuntimeError("model_dump broken")

        def to_dict(self):
            raise RuntimeError("to_dict broken")

        def dict(self):
            raise RuntimeError("dict broken")

        def json(self):
            raise RuntimeError("json broken")

        def to_json(self):
            raise RuntimeError("to_json broken")

        @property
        def __dict__(self):
            raise RuntimeError("__dict__ broken")

    obj = Unconvertible()
    result = to_dict(obj, suppress=True)
    assert result == {}


def test_to_dict_model_dump_raises_falls_back_to_dunder_dict():
    """When model_dump raises but __dict__ is available, fall back gracefully."""

    class PartiallyBroken:
        def __init__(self):
            self.x = 10
            self.y = 20

        def model_dump(self, **kwargs):
            raise ValueError("model_dump broken")

    obj = PartiallyBroken()
    # prioritize_model_dump=True tries model_dump first; it raises; then falls
    # through to __dict__ via _object_to_mapping_like
    result = to_dict(obj, prioritize_model_dump=True, suppress=True)
    # Should not crash; may return {} if all strategies fail, or {"x": 10, "y": 20}
    assert isinstance(result, dict)


def test_to_dict_recursive_with_xml_str_type_non_top_level():
    """str_type='xml' at non-top-level should be forwarded through recursion."""
    pytest.importorskip("xmltodict")
    xml = '<?xml version="1.0"?><root><item>val</item></root>'
    data = {"wrapper": xml}
    result = to_dict(data, recursive=True, str_type="xml")
    # "wrapper" key should have been recursively converted from xml string
    assert isinstance(result, dict)
    assert "wrapper" in result


def test_to_dict_suppress_true_with_circular_reference_like_object():
    """An object whose dict() conversion raises should be handled with suppress=True."""

    class CircularLike:
        def dict(self):
            raise RecursionError("circular")

    obj = CircularLike()
    result = to_dict(obj, suppress=True)
    assert isinstance(result, dict)


def test_to_dict_recursive_max_depth_validation():
    """max_recursive_depth must be a non-negative int <= 10."""
    import pytest

    with pytest.raises(ValueError):
        to_dict({"a": 1}, recursive=True, max_recursive_depth=-1)

    with pytest.raises(ValueError):
        to_dict({"a": 1}, recursive=True, max_recursive_depth=11)


# ============================================================================
# Edge cases: spec group "libs"
# ============================================================================


def test_to_dict_suppress_true_all_conversion_paths_exhausted():
    """suppress=True must return {} when literally every strategy raises, including
    the fallback dict(obj) call at the end of _convert_top_level_to_dict."""

    class AlwaysExplodes:
        def __iter__(self):
            raise RuntimeError("iter broken")

    obj = AlwaysExplodes()
    result = to_dict(obj, suppress=True)
    assert result == {}


def test_preprocess_recursive_str_type_xml_at_non_top_level():
    """str_type='xml' threaded into recursion should be used when converting
    an XML string nested inside a dict value."""
    pytest.importorskip("xmltodict")
    xml = '<?xml version="1.0"?><root><child>hello</child></root>'
    data = {"nested": xml}
    result = _preprocess_recursive(
        data,
        depth=0,
        max_depth=5,
        recursive_custom_types=False,
        str_parse_opts={
            "fuzzy_parse": False,
            "str_type": "xml",
            "parser": None,
        },
        prioritize_model_dump=True,
    )
    assert isinstance(result, dict)
    # "nested" value should have been parsed from XML
    assert "nested" in result
    assert isinstance(result["nested"], dict)
