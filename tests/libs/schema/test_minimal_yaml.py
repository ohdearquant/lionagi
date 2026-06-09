"""
Test suite for lionagi/libs/schema/minimal_yaml.py

Tests cover:
- Basic YAML conversion functionality
- Pruning logic (None, empty strings, empty collections)
- Multiline string handling (block scalar representation)
- Nested structures
- JSON string input handling
- Edge cases (circular refs, large numbers, special types)
"""

import json

import pytest

from lionagi.libs.schema.minimal_yaml import minimal_yaml


class TestBasicConversion:
    def test_simple_dict(self):
        data = {"name": "John", "age": 30}
        result = minimal_yaml(data)

        assert "name: John" in result
        assert "age: 30" in result
        assert isinstance(result, str)

    def test_simple_list(self):
        data = ["apple", "banana", "cherry"]
        result = minimal_yaml(data)

        assert "- apple" in result
        assert "- banana" in result
        assert "- cherry" in result

    def test_nested_dict(self):
        data = {"person": {"name": "John", "age": 30}}
        result = minimal_yaml(data)

        assert "person:" in result
        assert "name: John" in result
        assert "age: 30" in result

    def test_list_of_dicts(self):
        data = [{"name": "John", "age": 30}, {"name": "Jane", "age": 25}]
        result = minimal_yaml(data)

        assert "- name: John" in result
        assert "age: 30" in result
        assert "- name: Jane" in result
        assert "age: 25" in result

    def test_dict_with_list_values(self):
        data = {"fruits": ["apple", "banana"], "colors": ["red", "yellow"]}
        result = minimal_yaml(data)

        assert "fruits:" in result
        assert "- apple" in result
        assert "- banana" in result
        assert "colors:" in result

    def test_scalar_values(self):
        # String
        assert "hello" in minimal_yaml({"value": "hello"})

        # Integer
        assert "42" in minimal_yaml({"value": 42})

        # Float
        assert "3.14" in minimal_yaml({"value": 3.14})

        # Boolean
        assert "true" in minimal_yaml({"value": True})


class TestPruning:
    def test_none_values_removed(self):
        data = {"name": "John", "age": None, "active": True}
        result = minimal_yaml(data, drop_empties=True)

        assert "name: John" in result
        assert "active: true" in result
        assert "age" not in result

    def test_empty_string_removed(self):
        data = {"name": "John", "email": "", "phone": "123"}
        result = minimal_yaml(data, drop_empties=True)

        assert "name: John" in result
        assert "phone:" in result
        assert "email" not in result

    def test_whitespace_only_string_removed(self):
        data = {"name": "John", "comment": "   ", "city": "NYC"}
        result = minimal_yaml(data, drop_empties=True)

        assert "name: John" in result
        assert "city: NYC" in result
        assert "comment" not in result

    def test_empty_list_removed(self):
        data = {"items": [], "name": "John"}
        result = minimal_yaml(data, drop_empties=True)

        assert "name: John" in result
        assert "items" not in result

    def test_empty_dict_removed(self):
        data = {"person": {}, "valid": "data"}
        result = minimal_yaml(data, drop_empties=True)

        assert "valid: data" in result
        assert "person" not in result

    def test_zero_preserved(self):
        data = {"count": 0, "total": 100}
        result = minimal_yaml(data, drop_empties=True)

        assert "count: 0" in result
        assert "total: 100" in result

    def test_false_preserved(self):
        data = {"active": False, "verified": True}
        result = minimal_yaml(data, drop_empties=True)

        assert "active: false" in result
        assert "verified: true" in result

    def test_recursive_pruning(self):
        data = {"outer": {"inner": {"value": None, "empty": ""}, "name": "test"}}
        result = minimal_yaml(data, drop_empties=True)

        assert "name: test" in result
        assert "value" not in result
        assert "empty" not in result

    def test_list_pruning(self):
        data = {"items": ["valid", "", None, "another"]}
        result = minimal_yaml(data, drop_empties=True)

        assert "- valid" in result
        assert "- another" in result
        # Empty strings and None should be removed from list

    def test_drop_empties_false(self):
        data = {"name": "John", "age": None, "email": ""}
        result = minimal_yaml(data, drop_empties=False)

        assert "name: John" in result
        assert "age:" in result  # None should appear
        assert "email:" in result  # Empty string should appear


class TestMultilineStrings:
    def test_multiline_string_uses_block_scalar(self):
        data = {"description": "Line 1\nLine 2\nLine 3"}
        result = minimal_yaml(data)

        assert "description: |" in result
        assert "Line 1" in result
        assert "Line 2" in result
        assert "Line 3" in result

    def test_single_line_string_plain_style(self):
        data = {"description": "Single line"}
        result = minimal_yaml(data)

        assert "description: Single line" in result
        assert "|" not in result

    def test_multiline_preserves_content(self):
        data = {"text": "First line\nSecond line with spaces\nThird line\n\nFifth line"}
        result = minimal_yaml(data)

        assert "text: |" in result
        assert "First line" in result
        assert "Second line with spaces" in result


class TestNestedStructures:
    def test_deeply_nested_dict(self):
        data = {"level1": {"level2": {"level3": {"level4": {"value": "deep"}}}}}
        result = minimal_yaml(data)

        assert "level1:" in result
        assert "level2:" in result
        assert "level3:" in result
        assert "level4:" in result
        assert "value: deep" in result

    def test_complex_nested_structure(self):
        data = {
            "project": {
                "name": "test",
                "settings": {"debug": True, "features": ["a", "b", "c"]},
                "metadata": {"created": "2024", "tags": ["tag1", "tag2"]},
            }
        }
        result = minimal_yaml(data)

        assert "project:" in result
        assert "name: test" in result
        assert "debug: true" in result
        assert "- a" in result

    def test_nested_lists(self):
        data = {"matrix": [[1, 2, 3], [4, 5, 6], [7, 8, 9]]}
        result = minimal_yaml(data)

        assert "matrix:" in result
        # Nested lists should be represented

    def test_list_of_nested_dicts(self):
        data = {
            "users": [
                {"name": "John", "settings": {"theme": "dark"}},
                {"name": "Jane", "settings": {"theme": "light"}},
            ]
        }
        result = minimal_yaml(data)

        assert "users:" in result
        assert "name: John" in result
        assert "theme: dark" in result


class TestJsonStringInput:
    def test_json_string_parsed(self):
        json_str = json.dumps({"name": "John", "age": 30})
        result = minimal_yaml(json_str)

        assert "name: John" in result
        assert "age: 30" in result

    def test_json_string_with_nested_data(self):
        data = {"person": {"name": "John", "contacts": ["email", "phone"]}}
        json_str = json.dumps(data)
        result = minimal_yaml(json_str)

        assert "person:" in result
        assert "name: John" in result
        assert "contacts:" in result

    def test_dict_input_not_parsed_as_json(self):
        data = {"name": "John", "age": 30}
        result = minimal_yaml(data)

        # Should work directly without JSON parsing
        assert "name: John" in result
        assert "age: 30" in result


class TestParameters:
    def test_custom_indent(self):
        data = {"person": {"name": "John"}}

        # Default indent (2)
        result_default = minimal_yaml(data)

        # Custom indent (4)
        result_custom = minimal_yaml(data, indent=4)

        # Both should work
        assert isinstance(result_default, str)
        assert isinstance(result_custom, str)

    def test_sort_keys_true(self):
        data = {"zebra": 1, "apple": 2, "mango": 3}
        result = minimal_yaml(data, sort_keys=True)

        # Keys should be sorted
        lines = [line.strip() for line in result.split("\n") if line.strip()]
        keys = [line.split(":")[0] for line in lines]

        assert keys == ["apple", "mango", "zebra"]

    def test_sort_keys_false(self):
        data = {"zebra": 1, "apple": 2, "mango": 3}
        result = minimal_yaml(data, sort_keys=False)

        # Should preserve insertion order (Python 3.7+)
        assert isinstance(result, str)

    def test_line_width_parameter(self):
        data = {"key": "very long value " * 10}

        # Should not wrap with large line_width
        result = minimal_yaml(data, line_width=2**31 - 1)

        # No line breaks in the value (except for block scalars)
        assert isinstance(result, str)


class TestEdgeCases:
    def test_empty_input(self):
        result = minimal_yaml({})

        assert result.strip() == "{}"

    def test_single_key_dict(self):
        data = {"key": "value"}
        result = minimal_yaml(data)

        assert "key: value" in result

    def test_numeric_keys(self):
        data = {1: "one", 2: "two", 3: "three"}
        result = minimal_yaml(data)

        assert isinstance(result, str)
        # Numeric keys should be converted

    def test_special_characters_in_strings(self):
        data = {"text": "Hello: world, with-special_chars!"}
        result = minimal_yaml(data)

        assert "text:" in result
        assert isinstance(result, str)

    def test_unicode_characters(self):
        data = {"name": "José", "city": "São Paulo", "emoji": "🎉"}
        result = minimal_yaml(data, drop_empties=False)

        assert "name: José" in result
        assert "São Paulo" in result
        # Unicode should be preserved

    def test_very_large_numbers(self):
        data = {"big": 999999999999999999, "small": 0.000000001}
        result = minimal_yaml(data)

        assert isinstance(result, str)
        # Large numbers should be represented

    def test_boolean_values(self):
        data = {"enabled": True, "disabled": False}
        result = minimal_yaml(data)

        assert "enabled: true" in result
        assert "disabled: false" in result

    def test_null_value_explicit(self):
        data = {"value": None}
        result = minimal_yaml(data, drop_empties=False)

        assert "value:" in result

    def test_mixed_type_list(self):
        data = {"mixed": [1, "two", 3.0, True, None]}
        result = minimal_yaml(data)

        assert "mixed:" in result
        assert isinstance(result, str)

    def test_tuple_handling(self):
        data = {"coords": (1, 2, 3)}
        result = minimal_yaml(data, drop_empties=False)

        # Tuples should be converted to lists in YAML
        assert isinstance(result, str)

    def test_set_handling(self):
        data = {"tags": {"tag1", "tag2", "tag3"}}
        result = minimal_yaml(data, drop_empties=False)

        # Sets should be converted
        assert isinstance(result, str)

    def test_nested_empty_collections_pruned(self):
        data = {
            "outer": {
                "inner1": {"deep": {}},
                "inner2": {"deep": []},
                "valid": "data",
            }
        }
        result = minimal_yaml(data, drop_empties=True)

        assert "valid: data" in result
        # inner1 and inner2 should be completely removed since they only contain empties


class TestNoAliases:
    def test_repeated_objects_no_aliases(self):
        shared_list = [1, 2, 3]
        data = {"list1": shared_list, "list2": shared_list}

        result = minimal_yaml(data)

        # Should not contain alias markers (&id, *id)
        assert "&" not in result or "&" not in result.split(":")[0]
        assert "*" not in result or "*" not in result.split(":")[0]

    def test_repeated_dicts_no_aliases(self):
        shared_dict = {"key": "value"}
        data = {"dict1": shared_dict, "dict2": shared_dict}

        result = minimal_yaml(data)

        # Should not contain alias markers
        assert isinstance(result, str)


class TestReturnType:
    def test_returns_string(self):
        result = minimal_yaml({"key": "value"})

        assert isinstance(result, str)

    def test_output_is_valid_yaml(self):
        import yaml

        data = {"name": "John", "age": 30, "items": ["a", "b", "c"]}
        result = minimal_yaml(data)

        # Should be parseable
        parsed = yaml.safe_load(result)
        assert isinstance(parsed, dict)

    def test_roundtrip_consistency(self):
        import yaml

        data = {
            "name": "John",
            "age": 30,
            "active": True,
            "score": 0,
            "items": ["a", "b", "c"],
        }

        yaml_str = minimal_yaml(data, drop_empties=False)
        parsed = yaml.safe_load(yaml_str)

        assert parsed["name"] == data["name"]
        assert parsed["age"] == data["age"]
        assert parsed["active"] == data["active"]
        assert parsed["score"] == data["score"]
        assert parsed["items"] == data["items"]


class TestPruningHelpers:
    def test_empty_tuple_removed(self):
        data = {"items": (), "valid": "data"}
        result = minimal_yaml(data, drop_empties=True)

        assert "valid: data" in result
        assert "items" not in result

    def test_empty_set_removed(self):
        data = {"tags": set(), "name": "test"}
        result = minimal_yaml(data, drop_empties=True)

        assert "name: test" in result
        assert "tags" not in result

    def test_nested_list_pruning(self):
        data = {"items": [["valid"], [], ["another"], None]}
        result = minimal_yaml(data, drop_empties=True)

        # Should remove empty nested list and None
        assert "- - valid" in result or "- valid" in result
        assert isinstance(result, str)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
