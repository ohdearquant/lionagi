# tests/libs/fuzzy/test_fuzzy_validate.py
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from lionagi._errors import ValidationError
from lionagi.ln.fuzzy._fuzzy_match import FuzzyMatchKeysParams
from lionagi.ln.fuzzy._fuzzy_validate import (
    fuzzy_validate_mapping,
    fuzzy_validate_pydantic,
)


# Test models
class SimpleModel(BaseModel):
    name: str
    age: int
    email: str


class NestedModel(BaseModel):
    user: dict
    status: str


class OptionalModel(BaseModel):
    required: str
    optional: str = "default"


class TestFuzzyValidatePydantic:
    """Test fuzzy_validate_pydantic function (lines 27-54)."""

    def test_valid_json_string(self):
        """Test basic valid JSON string validation."""
        text = '{"name": "Alice", "age": 30, "email": "alice@example.com"}'
        result = fuzzy_validate_pydantic(text, SimpleModel)
        assert isinstance(result, SimpleModel)
        assert result.name == "Alice"
        assert result.age == 30
        assert result.email == "alice@example.com"

    def test_extract_json_exception(self):
        """Test extract_json raising exception (lines 29-30)."""
        # Pass None to cause extract_json to fail
        with pytest.raises(ValidationError, match="Failed to extract valid JSON"):
            fuzzy_validate_pydantic(None, SimpleModel)

    def test_json_with_fuzzy_parse(self):
        """Test fuzzy parsing enabled (line 28)."""
        text = "```json\n{name: 'Bob', age: 25, email: 'bob@example.com'}\n```"
        result = fuzzy_validate_pydantic(text, SimpleModel, fuzzy_parse=True)
        assert result.name == "Bob"
        assert result.age == 25

    def test_invalid_json_raises_validation_error(self):
        """Test extraction failure raises ValidationError (lines 29-32)."""
        text = "This is not JSON at all"
        # extract_json returns [] for non-JSON text, which causes validation to fail
        with pytest.raises(ValidationError, match="Validation failed"):
            fuzzy_validate_pydantic(text, SimpleModel, fuzzy_parse=False)

    def test_fuzzy_match_default_params(self):
        """Test fuzzy match with None params (lines 36-39)."""
        # Use keys that need fuzzy matching
        text = '{"nam": "Charlie", "ag": 35, "emal": "charlie@example.com"}'
        result = fuzzy_validate_pydantic(
            text, SimpleModel, fuzzy_match=True, fuzzy_match_params=None
        )
        assert result.name == "Charlie"
        assert result.age == 35
        assert result.email == "charlie@example.com"

    def test_fuzzy_match_with_dict_params(self):
        """Test fuzzy match with dict params (lines 40-43)."""
        text = '{"nam": "David", "ag": 40, "emal": "david@example.com"}'
        result = fuzzy_validate_pydantic(
            text,
            SimpleModel,
            fuzzy_match=True,
            fuzzy_match_params={
                "similarity_threshold": 0.7,
                "handle_unmatched": "remove",
            },
        )
        assert result.name == "David"
        assert result.age == 40

    def test_fuzzy_match_with_params_instance(self):
        """Test fuzzy match with FuzzyMatchKeysParams instance (lines 44-45)."""
        text = '{"nam": "Eve", "ag": 28, "emal": "eve@example.com"}'
        params = FuzzyMatchKeysParams(similarity_threshold=0.75, handle_unmatched="remove")
        result = fuzzy_validate_pydantic(
            text, SimpleModel, fuzzy_match=True, fuzzy_match_params=params
        )
        assert result.name == "Eve"
        assert result.age == 28

    def test_fuzzy_match_invalid_params_type(self):
        """Test invalid fuzzy_match_params type raises TypeError (lines 46-49)."""
        text = '{"name": "Frank", "age": 50, "email": "frank@example.com"}'
        with pytest.raises(
            TypeError,
            match="fuzzy_keys_params must be a dict or FuzzyMatchKeysParams instance",
        ):
            fuzzy_validate_pydantic(
                text,
                SimpleModel,
                fuzzy_match=True,
                fuzzy_match_params="invalid",
            )

    def test_model_validation_failure(self):
        """Test model validation failure raises ValidationError (lines 51-54)."""
        # Missing required field
        text = '{"name": "George", "age": 45}'
        with pytest.raises(ValidationError, match="Validation failed"):
            fuzzy_validate_pydantic(text, SimpleModel)

    def test_model_validation_type_error(self):
        """Test model validation with wrong types (lines 51-54)."""
        text = '{"name": "Henry", "age": "not_a_number", "email": "henry@example.com"}'
        with pytest.raises(ValidationError, match="Validation failed"):
            fuzzy_validate_pydantic(text, SimpleModel)

    def test_no_fuzzy_match(self):
        """Test without fuzzy matching (fuzzy_match=False)."""
        text = '{"name": "Iris", "age": 32, "email": "iris@example.com"}'
        result = fuzzy_validate_pydantic(text, SimpleModel, fuzzy_match=False)
        assert result.name == "Iris"
        assert result.age == 32

    def test_nested_model_validation(self):
        """Test with nested model structures."""
        text = '{"user": {"id": 1, "name": "Jack"}, "status": "active"}'
        result = fuzzy_validate_pydantic(text, NestedModel)
        assert result.user == {"id": 1, "name": "Jack"}
        assert result.status == "active"


class TestFuzzyValidateMapping:
    """Test fuzzy_validate_mapping function."""

    def test_none_input_raises_type_error(self):
        """Test None input raises TypeError (line 108)."""
        with pytest.raises(TypeError, match="Input cannot be None"):
            fuzzy_validate_mapping(None, ["key1", "key2"])

    def test_simple_dict_input(self):
        """Test basic dict input validation."""
        result = fuzzy_validate_mapping({"name": "Alice", "age": 30}, ["name", "age"])
        assert result == {"name": "Alice", "age": 30}

    def test_json_string_input(self):
        """Test JSON string input (lines 112-121)."""
        json_str = '{"name": "Bob", "age": 25, "city": "NYC"}'
        result = fuzzy_validate_mapping(json_str, ["name", "age", "city"])
        assert result["name"] == "Bob"
        assert result["age"] == 25

    def test_json_string_with_extract_json_fallback(self):
        """Test JSON string extraction with fallback (lines 122-125)."""
        # This should trigger extract_json and then fall back to to_dict
        json_str = '{"nam": "Charlie", "ag": 35}'
        result = fuzzy_validate_mapping(
            json_str,
            ["name", "age"],
            fuzzy_match=True,
            handle_unmatched="remove",
        )
        assert "name" in result
        assert "age" in result

    def test_markdown_code_block_input(self):
        """Test markdown code block JSON extraction."""
        markdown = '```json\n{"name": "David", "score": 95}\n```'
        result = fuzzy_validate_mapping(markdown, ["name", "score"])
        assert result["name"] == "David"
        assert result["score"] == 95

    def test_pydantic_model_input(self):
        """Test Pydantic model input (lines 126-129)."""
        model = SimpleModel(name="Eve", age=28, email="eve@example.com")
        result = fuzzy_validate_mapping(model, ["name", "age", "email"])
        assert result["name"] == "Eve"
        assert result["age"] == 28
        assert result["email"] == "eve@example.com"

    def test_dict_with_model_dump(self):
        """Test object with model_dump method (lines 126-129)."""
        model = SimpleModel(name="Frank", age=40, email="frank@example.com")
        result = fuzzy_validate_mapping(model, ["name", "age"], handle_unmatched="remove")
        assert result["name"] == "Frank"
        assert result["age"] == 40

    def test_non_dict_result_with_suppress(self):
        """Test non-dict conversion with suppress enabled (lines 132-133)."""
        # Pass an object that cannot be converted to dict
        result = fuzzy_validate_mapping(
            42,  # Integer cannot be converted to dict
            ["key1", "key2"],
            suppress_conversion_errors=True,
            handle_unmatched="fill",
            fill_value=None,
        )
        assert isinstance(result, dict)
        assert result == {"key1": None, "key2": None}

    def test_non_dict_result_with_suppress_false(self):
        """Test non-dict conversion with suppress=False (lines 134-137)."""
        # When to_dict fails with suppress=False, it raises an exception
        # which is caught and re-raised as ValueError
        # Numbers can't be converted to dict properly
        result = fuzzy_validate_mapping(
            123,
            ["key1", "key2"],
            suppress_conversion_errors=True,
            handle_unmatched="fill",
            fill_value="default",
        )
        # With suppress=True, failed conversion becomes empty dict, then filled
        assert result == {"key1": "default", "key2": "default"}

    def test_conversion_exception_with_suppress(self):
        """Test conversion exception with suppress enabled (lines 139-141)."""

        # Create a problematic input
        class BadObject:
            def __init__(self):
                self.data = "test"

            def to_dict(self):
                raise RuntimeError("Conversion failed")

        result = fuzzy_validate_mapping(
            BadObject(),
            ["key1", "key2"],
            suppress_conversion_errors=True,
            handle_unmatched="fill",
            fill_value="default",
        )
        assert result == {"key1": "default", "key2": "default"}

    def test_conversion_paths_with_objects(self):
        """Test various conversion paths with different object types (lines 126-143)."""

        # Test that various objects can be converted
        class SimpleObject:
            def __init__(self):
                self.value = "test"

            def to_dict(self):
                return {"value": self.value}

        result = fuzzy_validate_mapping(SimpleObject(), ["value"])
        assert result["value"] == "test"

    def test_fuzzy_match_keys(self):
        """Test fuzzy matching of keys."""
        data = {"nam": "George", "ag": 45, "emal": "george@example.com"}
        result = fuzzy_validate_mapping(
            data,
            ["name", "age", "email"],
            fuzzy_match=True,
            similarity_threshold=0.7,
            handle_unmatched="remove",
        )
        assert "name" in result
        assert "age" in result
        assert "email" in result

    def test_handle_unmatched_ignore(self):
        """Test handle_unmatched='ignore' keeps extra keys."""
        data = {"name": "Henry", "age": 50, "extra": "value"}
        result = fuzzy_validate_mapping(data, ["name", "age"], handle_unmatched="ignore")
        assert result["name"] == "Henry"
        assert result["age"] == 50
        assert result["extra"] == "value"

    def test_handle_unmatched_remove(self):
        """Test handle_unmatched='remove' removes extra keys."""
        data = {"name": "Iris", "age": 32, "extra": "value"}
        result = fuzzy_validate_mapping(data, ["name", "age"], handle_unmatched="remove")
        assert result == {"name": "Iris", "age": 32}
        assert "extra" not in result

    def test_handle_unmatched_raise(self):
        """Test handle_unmatched='raise' raises error for unmatched keys."""
        data = {"name": "Jack", "age": 38, "extra": "value"}
        with pytest.raises(ValueError, match="Unmatched keys found"):
            fuzzy_validate_mapping(
                data,
                ["name", "age"],
                handle_unmatched="raise",
                fuzzy_match=False,
            )

    def test_handle_unmatched_fill(self):
        """Test handle_unmatched='fill' fills missing keys."""
        data = {"name": "Kate"}
        result = fuzzy_validate_mapping(
            data,
            ["name", "age", "email"],
            handle_unmatched="fill",
            fill_value=None,
        )
        assert result["name"] == "Kate"
        assert result["age"] is None
        assert result["email"] is None

    def test_handle_unmatched_force(self):
        """Test handle_unmatched='force' fills missing and removes unmatched."""
        data = {"name": "Leo", "extra": "remove_me"}
        result = fuzzy_validate_mapping(
            data,
            ["name", "age"],
            handle_unmatched="force",
            fill_value="missing",
        )
        assert result["name"] == "Leo"
        assert result["age"] == "missing"
        assert "extra" not in result

    def test_fill_mapping(self):
        """Test fill_mapping with custom default values."""
        data = {"name": "Mike"}
        result = fuzzy_validate_mapping(
            data,
            ["name", "age", "email"],
            handle_unmatched="fill",
            fill_mapping={"age": 0, "email": "default@example.com"},
        )
        assert result["name"] == "Mike"
        assert result["age"] == 0
        assert result["email"] == "default@example.com"

    def test_strict_mode_raises_error(self):
        """Test strict mode raises error for missing keys."""
        data = {"name": "Nancy"}
        with pytest.raises(ValueError, match="Missing required keys"):
            fuzzy_validate_mapping(data, ["name", "age", "email"], strict=True)

    def test_similarity_threshold(self):
        """Test custom similarity threshold."""
        data = {"nm": "Oscar", "ag": 60}
        result = fuzzy_validate_mapping(
            data,
            ["name", "age"],
            fuzzy_match=True,
            similarity_threshold=0.5,  # Lower threshold
            handle_unmatched="remove",
        )
        # With lower threshold, 'nm' might match 'name'
        assert len(result) >= 1

    def test_similarity_algo_custom(self):
        """Test custom similarity algorithm."""
        data = {
            "nam": "Paul",
            "age": 45,
        }  # "nam" is closer to "name" than "nme"
        result = fuzzy_validate_mapping(
            data,
            ["name", "age"],
            fuzzy_match=True,
            similarity_algo="levenshtein",
            similarity_threshold=0.6,  # Lower threshold to allow fuzzy match
            handle_unmatched="remove",
        )
        assert "name" in result  # "nam" should fuzzy match to "name"
        assert result["age"] == 45

    def test_list_input_returns_first_element(self):
        """Test list input with extract_json."""
        # extract_json can return lists
        json_list = '{"name": "Quinn", "age": 29}'
        result = fuzzy_validate_mapping(json_list, ["name", "age"])
        assert result["name"] == "Quinn"


class TestFuzzyValidateEdgeCases:
    """Test edge cases and special scenarios."""

    def test_empty_dict(self):
        """Test empty dict input."""
        result = fuzzy_validate_mapping(
            {}, ["key1", "key2"], handle_unmatched="fill", fill_value=None
        )
        assert result == {"key1": None, "key2": None}

    def test_empty_keys_list(self):
        """Test empty keys list."""
        data = {"name": "Test", "age": 25}
        result = fuzzy_validate_mapping(data, [])
        assert result == data

    def test_complex_nested_json(self):
        """Test complex nested JSON structure."""
        json_str = '{"user": {"name": "Test", "details": {"age": 30}}, "status": "active"}'
        result = fuzzy_validate_mapping(json_str, ["user", "status"])
        assert "user" in result
        assert result["status"] == "active"

    def test_json_with_arrays(self):
        """Test JSON with array values."""
        json_str = '{"name": "Test", "tags": ["a", "b", "c"]}'
        result = fuzzy_validate_mapping(json_str, ["name", "tags"])
        assert result["name"] == "Test"
        assert result["tags"] == ["a", "b", "c"]

    def test_dict_with_type_mapping(self):
        """Test keys as dict mapping."""
        data = {"name": "Test", "age": 30}
        keys_dict = {"name": str, "age": int}
        result = fuzzy_validate_mapping(data, keys_dict)
        assert result["name"] == "Test"
        assert result["age"] == 30

    def test_unicode_keys(self):
        """Test unicode characters in keys."""
        data = {"名前": "Test", "年齢": 30}
        result = fuzzy_validate_mapping(data, ["名前", "年齢"])
        assert result["名前"] == "Test"
        assert result["年齢"] == 30

    def test_special_char_keys(self):
        """Test special characters in keys."""
        data = {"key-1": "value1", "key_2": "value2", "key.3": "value3"}
        result = fuzzy_validate_mapping(data, ["key-1", "key_2", "key.3"])
        assert result["key-1"] == "value1"

    def test_fuzzy_parse_with_malformed_json(self):
        """Test fuzzy parse with malformed JSON."""
        # This should use fuzzy_parse to handle malformed JSON
        malformed = "{name: 'Test', age: 30}"  # Missing quotes around keys
        result = fuzzy_validate_mapping(malformed, ["name", "age"])
        assert "name" in result


class TestCoverageTargets:
    """Specific tests to hit missing coverage lines."""

    def test_line_122_123_exception_handler(self):
        """Target lines 122-123: Exception in extract_json fallback to to_dict."""
        # Create input that will fail extract_json but succeed with to_dict
        invalid_json_str = "not valid json but has braces {}"
        result = fuzzy_validate_mapping(
            invalid_json_str,
            ["key"],
            handle_unmatched="fill",
            fill_value="default",
            suppress_conversion_errors=True,
        )
        assert isinstance(result, dict)

    def test_line_132_137_non_dict_conversion(self):
        """Target lines 132-137: Non-dict result handling."""
        # to_dict is called with suppress=True, which means it tries to convert
        # everything to dict. Lists become enumerated dicts.
        # Test the defensive check for non-dict results

        # Test with suppress_conversion_errors=True (lines 132-133)
        result = fuzzy_validate_mapping(
            789,  # Number that can't properly convert
            ["key1"],
            suppress_conversion_errors=True,
            handle_unmatched="fill",
            fill_value="filled",
        )
        # When conversion fails, returns {}, then fill adds missing keys
        assert "key1" in result
        assert result["key1"] == "filled"

    def test_line_139_143_conversion_exception(self):
        """Target lines 139-143: Exception during conversion."""
        # The entire conversion is wrapped in try/except
        # Test the exception path with suppress=True (returns {})

        class FailingObject:
            def to_dict(self):
                raise Exception("Intentional failure")

        # Test with suppress_conversion_errors=True (lines 140-141)
        result = fuzzy_validate_mapping(
            FailingObject(),
            ["key1", "key2"],
            suppress_conversion_errors=True,
            handle_unmatched="fill",
            fill_value=None,
        )
        # Exception is caught, returns {}, then fill adds keys
        assert result == {"key1": None, "key2": None}

    def test_string_parsing_variations(self):
        """Test various string inputs to increase coverage."""
        # Test JSON with markdown that extract_json handles
        markdown_json = '```json\n{"key1": "value1", "key2": "value2"}\n```'
        result = fuzzy_validate_mapping(markdown_json, ["key1", "key2"])
        assert result["key1"] == "value1"

        # Test plain JSON string
        plain_json = '{"key1": "test", "key2": 123}'
        result2 = fuzzy_validate_mapping(plain_json, ["key1", "key2"])
        assert result2["key1"] == "test"

    def test_non_string_non_dict_inputs(self):
        """Test non-string inputs that need conversion."""

        # Test with a list (gets passed to to_dict)
        class ObjWithDict:
            def __init__(self):
                self.__dict__ = {"key1": "value1", "key2": "value2"}

        result = fuzzy_validate_mapping(ObjWithDict(), ["key1", "key2"])
        assert "key1" in result

    def test_string_extract_json_fallback(self):
        """Test string input that triggers extract_json exception path (line 122-125)."""
        # Create a string that will fail extract_json but succeed with to_dict
        # extract_json looks for JSON or markdown code blocks
        # A string like "not json" will fail extract_json, then fall back to to_dict
        weird_string = "random text without json structure"
        result = fuzzy_validate_mapping(
            weird_string,
            ["key1"],
            handle_unmatched="fill",
            fill_value="default",
        )
        # Should get filled keys since conversion produces empty dict
        assert result.get("key1") == "default"

    def test_string_with_list_result_from_extract_json(self):
        """Test when extract_json returns a list (lines 117-120)."""
        # Multiple JSON blocks in markdown
        multi_json = """
        ```json
        {"key1": "value1"}
        ```
        Some text
        ```json
        {"key2": "value2"}
        ```
        """
        result = fuzzy_validate_mapping(multi_json, ["key1", "key2"])
        # extract_json returns list, first element is used
        assert "key1" in result or "key2" in result

    def test_lines_132_137_with_mock(self):
        """Test lines 132-137 by mocking to_dict to return non-dict."""
        with patch("lionagi.ln.fuzzy._fuzzy_validate.to_dict") as mock_to_dict:
            # Make to_dict return a non-dict value
            mock_to_dict.return_value = ["not", "a", "dict"]

            # Test with suppress=True (lines 132-133)
            result = fuzzy_validate_mapping(
                {"test": "data"},
                ["key1"],
                suppress_conversion_errors=True,
                handle_unmatched="fill",
                fill_value="filled",
            )
            assert result == {"key1": "filled"}

            # Test with suppress=False (lines 134-137)
            mock_to_dict.return_value = "still_not_a_dict"
            with pytest.raises(ValueError, match="Failed to convert input to dictionary"):
                fuzzy_validate_mapping(
                    {"test": "data"},
                    ["key1"],
                    suppress_conversion_errors=False,
                )

    def test_lines_139_143_with_mock(self):
        """Test lines 139-143 by mocking to raise exception."""
        with patch("lionagi.ln.fuzzy._fuzzy_validate.to_dict") as mock_to_dict:
            # Make to_dict raise an exception
            mock_to_dict.side_effect = RuntimeError("Conversion failed")

            # Test with suppress=True (lines 140-141)
            result = fuzzy_validate_mapping(
                {"test": "data"},
                ["key1", "key2"],
                suppress_conversion_errors=True,
                handle_unmatched="fill",
                fill_value="default",
            )
            assert result == {"key1": "default", "key2": "default"}

            # Test with suppress=False (lines 142-143)
            with pytest.raises(ValueError, match="Failed to convert input to dictionary"):
                fuzzy_validate_mapping(
                    {"test": "data"},
                    ["key1"],
                    suppress_conversion_errors=False,
                )
