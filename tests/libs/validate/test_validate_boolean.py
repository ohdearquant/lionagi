# File: tests/test_validate/test_validate_boolean.py

from typing import Any

import pytest

from lionagi.libs.validate.validate_boolean import (
    FALSE_VALUES,
    TRUE_VALUES,
    validate_boolean,
)


class TestValidateBoolean:
    """Tests for validate_boolean function."""

    @pytest.mark.parametrize("value", [True, False])
    def test_boolean_inputs(self, value: bool):
        """Test that boolean inputs are returned unchanged."""
        assert validate_boolean(value) is value

    @pytest.mark.parametrize("value", sorted(TRUE_VALUES))
    def test_true_string_values(self, value: str):
        """Test string values that should convert to True."""
        assert validate_boolean(value) is True
        assert validate_boolean(value.upper()) is True
        assert validate_boolean(f" {value} ") is True

    @pytest.mark.parametrize("value", sorted(FALSE_VALUES))
    def test_false_string_values(self, value: str):
        """Test string values that should convert to False."""
        assert validate_boolean(value) is False
        assert validate_boolean(value.upper()) is False
        assert validate_boolean(f" {value} ") is False

    @pytest.mark.parametrize(
        "value",
        [
            0,
            0.0,
            -0,
            0j,  # Should be False
            1,
            -1,
            1.5,
            42,
            float("inf"),
            complex(1, 1),  # Should be True
        ],
    )
    def test_numeric_values(self, value: Any):
        """Test numeric values conversion."""
        expected = bool(value)
        assert validate_boolean(value) is expected

    def test_complex_numbers(self):
        """Test complex number handling specifically."""
        assert validate_boolean(0j) is False
        assert validate_boolean(complex(0)) is False
        assert validate_boolean(complex(0, 0)) is False

        assert validate_boolean(1j) is True
        assert validate_boolean(complex(1, 1)) is True
        assert validate_boolean(complex(0, 1)) is True
        assert validate_boolean(complex(1, 0)) is True

    def test_error_cases(self):
        """Test cases that should raise errors."""
        with pytest.raises(TypeError, match="Cannot convert None to boolean"):
            validate_boolean(None)

        with pytest.raises(ValueError, match="Cannot convert empty string to boolean"):
            validate_boolean("")
        with pytest.raises(ValueError, match="Cannot convert empty string to boolean"):
            validate_boolean("   ")

        with pytest.raises(ValueError):
            validate_boolean("invalid")
        with pytest.raises(ValueError):
            validate_boolean("truthy")
        with pytest.raises(ValueError):
            validate_boolean("falsey")

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("1.0", True),
            ("-1", True),
            ("0.0", False),
            ("0j", False),  # Complex number strings
            ("1j", True),
            ("1+1j", True),
            ("TRUE", True),
            ("False", False),
            ("YeS", True),
            ("nO", False),
            ("  true  ", True),
            ("  false  ", False),
            ("enable", True),
            ("disable", False),
            ("activated", True),
            ("deactivated", False),
        ],
    )
    def test_variations(self, value: Any, expected: bool):
        """Test various input variations."""
        assert validate_boolean(value) is expected

    def test_object_conversion(self):
        """Test conversion of objects with __str__ method."""

        class TrueObject:
            def __str__(self):
                return "true"

        class FalseObject:
            def __str__(self):
                return "false"

        assert validate_boolean(TrueObject()) is True
        assert validate_boolean(FalseObject()) is False

    def test_invalid_objects(self):
        """Test objects that cannot be converted."""

        class BadObject:
            def __str__(self):
                raise Exception("Cannot convert to string")

        with pytest.raises(TypeError):
            validate_boolean(BadObject())

    @pytest.mark.parametrize(
        "value",
        [
            object(),  # Generic object
            lambda x: x,  # Function
            ...,  # Ellipsis
            range(5),  # Range object
        ],
    )
    def test_unsupported_types(self, value: Any):
        """Test handling of unsupported types."""
        with pytest.raises((ValueError, TypeError)):
            validate_boolean(value)
