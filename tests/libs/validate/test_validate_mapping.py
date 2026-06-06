# File: tests/test_validate/test_validate_mapping.py


import pytest
from pydantic import BaseModel

from lionagi.ln.fuzzy._fuzzy_validate import fuzzy_validate_mapping


class TestValidateMapping:
    """Tests for validate_mapping function."""

    def test_basic_dictionary_input(self):
        """Test basic dictionary input."""
        test_dict = {"name": "John", "age": 30}
        result = fuzzy_validate_mapping(test_dict, ["name", "age"])
        assert result == test_dict

    def test_string_inputs(self):
        """Test string input parsing."""
        # JSON string
        json_input = '{"name": "John", "age": 30}'
        result = fuzzy_validate_mapping(json_input, ["name", "age"])
        assert result == {"name": "John", "age": 30}

        # JSON with single quotes
        single_quote = "{'name': 'John', 'age': 30}"
        result = fuzzy_validate_mapping(single_quote, ["name", "age"])
        assert result == {"name": "John", "age": 30}

        # JSON in code block
        code_block = """```json
        {"name": "John", "age": 30}
        ```"""
        result = fuzzy_validate_mapping(code_block, ["name", "age"])
        assert result == {"name": "John", "age": 30}

    def test_type_conversion(self):
        """Test model conversion."""

        class UserModel(BaseModel):
            name: str
            age: int

        user = UserModel(name="John", age=30)
        result = fuzzy_validate_mapping(user, ["name", "age"])
        assert result == {"name": "John", "age": 30}

    @pytest.mark.parametrize(
        "handle_unmatched,expected_keys",
        [
            ("ignore", {"name", "extra"}),
            ("remove", {"name"}),
            ("fill", {"name", "age", "extra"}),
            ("force", {"name", "age"}),
        ],
    )
    def test_handle_unmatched_modes(self, handle_unmatched, expected_keys):
        """Test different handle_unmatched modes."""
        input_dict = {"name": "John", "extra": "value"}
        result = fuzzy_validate_mapping(
            input_dict,
            ["name", "age"],
            handle_unmatched=handle_unmatched,
            fill_value=None,
        )
        assert set(result.keys()) == expected_keys

    def test_handle_unmatched_raise(self):
        """Test raise mode for unmatched keys."""
        with pytest.raises(ValueError):
            fuzzy_validate_mapping(
                {"name": "John", "extra": "value"},
                ["name"],
                handle_unmatched="raise",
            )

    def test_fill_values(self):
        """Test fill value functionality."""
        input_dict = {"name": "John"}
        fill_mapping = {"age": 30, "email": "test@example.com"}

        result = fuzzy_validate_mapping(
            input_dict,
            ["name", "age", "email"],
            handle_unmatched="fill",
            fill_mapping=fill_mapping,
        )
        assert result == {
            "name": "John",
            "age": 30,
            "email": "test@example.com",
        }

    @pytest.mark.parametrize(
        "threshold,expected_match",
        [
            (0.95, None),  # High threshold - won't match
            (0.6, "username"),  # Low threshold - will match
        ],
    )
    def test_similarity_thresholds(self, threshold, expected_match):
        """Test different similarity thresholds."""
        result = fuzzy_validate_mapping(
            {"user_name": "John"},
            ["username"],
            similarity_threshold=threshold,
            handle_unmatched="remove",
        )
        if expected_match:
            assert expected_match in result
        else:
            assert "username" in result

    def test_error_cases(self):
        """Test error handling."""

        # None input
        with pytest.raises(TypeError):
            fuzzy_validate_mapping(None, ["key"])

        # Empty input with strict mode
        with pytest.raises(ValueError):
            fuzzy_validate_mapping({}, ["required_key"], strict=True)

    def test_strict_mode(self):
        """Test strict mode validation."""
        with pytest.raises(ValueError):
            fuzzy_validate_mapping({"name": "John"}, ["name", "required_field"], strict=True)

    def test_custom_similarity(self):
        """Test custom similarity function."""

        def case_insensitive_match(s1: str, s2: str) -> float:
            return 1.0 if s1.lower() == s2.lower() else 0.0

        result = fuzzy_validate_mapping(
            {"USER_NAME": "John"},
            ["username"],
            similarity_algo=case_insensitive_match,
            handle_unmatched="remove",
        )
        assert "username" not in result

    def test_suppress_conversion_errors(self):
        """Test error suppression."""
        # Should return empty dict with suppress_conversion_errors
        result = fuzzy_validate_mapping(
            object(),
            ["key"],
            suppress_conversion_errors=True,
            handle_unmatched="fill",
            fill_value=None,
        )
        assert result == {"key": None}
