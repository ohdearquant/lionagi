import math
from decimal import Decimal

import pytest

from lionagi.libs.validate.to_num import to_num


@pytest.mark.parametrize(
    "input_val, expected",
    [
        # Integers
        ("42", 42.0),
        ("-42", -42.0),
        ("+42", 42.0),
        ("0", 0.0),
        # Floats
        ("3.14159", 3.14159),
        ("-3.14159", -3.14159),
        ("+3.14159", 3.14159),
        ("0.0", 0.0),
        (".5", 0.5),
        ("5.", 5.0),
        # Scientific notation
        ("1e5", 1e5),
        ("1E5", 1e5),
        ("1.23e-4", 1.23e-4),
        ("1.23E-4", 1.23e-4),
        ("-1.23e4", -1.23e4),
        ("+1.23e4", 1.23e4),
        # Percentages
        ("50%", 0.5),
        ("100%", 1.0),
        ("0%", 0.0),
        ("25.5%", 0.255),
        # Fractions
        ("1/2", 0.5),
        ("3/4", 0.75),
        ("-1/2", -0.5),
        ("5/2", 2.5),
        # Boolean
        (True, 1.0),
        (False, 0.0),
    ],
)
def test_basic_conversion(input_val, expected):
    assert to_num(input_val) == pytest.approx(expected)


@pytest.mark.parametrize(
    "input_val, num_type, expected",
    [
        # String type specifications
        ("42", "int", 42),
        ("3.14", "float", 3.14),
        ("1+2j", "complex", 1 + 2j),
        # Type object specifications
        ("42", int, 42),
        ("3.14", float, 3.14),
        ("1+2j", complex, 1 + 2j),
        # Type conversions
        ("3.14", int, 3),
        ("42", float, 42.0),
        ("3", complex, 3 + 0j),
    ],
)
def test_type_conversion(input_val, num_type, expected):
    assert to_num(input_val, num_type=num_type) == expected


@pytest.mark.parametrize(
    "input_val, precision, expected",
    [
        ("3.14159", 2, 3.14),
        ("3.14159", 4, 3.1416),
        ("3.14159", 0, 3.0),
        ("3.14159", None, 3.14159),
        ("100.5555", 2, 100.56),
        ("-3.14159", 3, -3.142),
    ],
)
def test_precision(input_val, precision, expected):
    assert to_num(input_val, precision=precision) == pytest.approx(expected)


@pytest.mark.parametrize(
    "input_val, bounds, should_pass",
    [
        # Upper bound tests
        ("50", {"upper_bound": 100}, True),
        ("150", {"upper_bound": 100}, False),
        ("3.14", {"upper_bound": 3.0}, False),
        # Lower bound tests
        ("50", {"lower_bound": 0}, True),
        ("-50", {"lower_bound": 0}, False),
        ("2.5", {"lower_bound": 3.0}, False),
        # Both bounds
        ("50", {"lower_bound": 0, "upper_bound": 100}, True),
        ("-50", {"lower_bound": 0, "upper_bound": 100}, False),
        ("150", {"lower_bound": 0, "upper_bound": 100}, False),
    ],
)
def test_bounds(input_val, bounds, should_pass):
    if should_pass:
        assert to_num(input_val, **bounds) is not None
    else:
        with pytest.raises(ValueError):
            to_num(input_val, **bounds)


@pytest.mark.parametrize(
    "input_val, num_count, expected",
    [
        # Basic multiple numbers
        ("1 2 3", 3, [1.0, 2.0, 3.0]),
        ("1,2,3", 3, [1.0, 2.0, 3.0]),
        ("1;2;3", 3, [1.0, 2.0, 3.0]),
        # Mixed formats
        ("1 2.5 3e2", 3, [1.0, 2.5, 300.0]),
        ("50% 1/2 .75", 3, [0.5, 0.5, 0.75]),
        # Partial extraction
        ("1 2 3 4 5", 3, [1.0, 2.0, 3.0]),
        # Complex numbers
        ("1+2j 3-4j 5+0j", 3, [1 + 2j, 3 - 4j, 5 + 0j]),
    ],
)
def test_multiple_numbers(input_val, num_count, expected):
    result = to_num(input_val, num_count=num_count)
    if isinstance(result[0], complex):
        assert result == expected
    else:
        assert result == pytest.approx(expected)


@pytest.mark.parametrize(
    "input_val, expected",
    [
        # Special floats
        ("inf", float("inf")),
        ("-inf", float("-inf")),
        ("infinity", float("inf")),
        ("-infinity", float("-inf")),
        # Very large/small numbers
        ("1e308", 1e308),
        ("1e-308", 1e-308),
        ("9" * 20, float("9" * 20)),
    ],
)
def test_edge_cases(input_val, expected):
    result = to_num(input_val)
    if math.isnan(expected):
        assert math.isnan(result)
    else:
        assert result == expected


@pytest.mark.parametrize(
    "input_val, error_type, error_match",
    [
        # Invalid input types
        ([1, 2, 3], TypeError, "Input cannot be a sequence"),
        # Invalid numeric strings
        ("not a number", ValueError, "No valid numbers found"),
        ("", ValueError, "No valid numbers found"),
        ("   ", ValueError, "No valid numbers found"),
        # Invalid type specifications
        (42, ValueError, "Invalid number type"),
        (42, ValueError, "Invalid number type"),
        (42, ValueError, "Invalid number type"),
        # Invalid fractions
        ("1/0", ValueError, "Division by zero"),
        # Invalid complex numbers
        ("i", ValueError, "No valid numbers found"),
        # Invalid scientific notation
    ],
)
def test_error_cases(input_val, error_type, error_match):
    with pytest.raises(error_type, match=error_match):
        if isinstance(error_type, type) and issubclass(error_type, Exception):
            # Handle type specification errors
            if input_val == 42:
                to_num(input_val, num_type=str)  # Test with invalid type
            else:
                to_num(input_val)
        else:
            to_num(input_val)


@pytest.mark.parametrize(
    "params",
    [
        # Type + Precision
        (("3.14159",), {"num_type": float, "precision": 2}),
        # Type + Bounds
        (("50",), {"num_type": int, "upper_bound": 100, "lower_bound": 0}),
        # Type + Multiple numbers
        (("1 2 3",), {"num_type": int, "num_count": 3}),
        # All parameters
        (
            ("3.14159 2.71828",),
            {
                "num_type": float,
                "precision": 3,
                "upper_bound": 4,
                "lower_bound": 2,
                "num_count": 2,
            },
        ),
    ],
)
def test_parameter_combinations(params):
    args, kwargs = params
    result = to_num(*args, **kwargs)
    assert result is not None


@pytest.mark.parametrize(
    "input_val, expected",
    [
        ("  42  ", 42.0),
        ("\t3.14\n", 3.14),
        ("   50%   ", 0.5),
        ("  1/2  ", 0.5),
    ],
)
def test_whitespace_handling(input_val, expected):
    assert to_num(input_val) == expected


@pytest.mark.parametrize(
    "input_val, expected",
    [
        (42, 42.0),
        (3.14159, 3.14159),
        (1 + 2j, 1 + 2j),
        (Decimal("3.14"), 3.14),
    ],
)
def test_non_string_input(input_val, expected):
    assert to_num(input_val) == expected


@pytest.mark.parametrize(
    "input_val, num_count, expected",
    [
        (
            "int: 42, float: 3.14, scientific: 1e3, percent: 50%, fraction: 1/2",
            5,
            [42.0, 3.14, 1000.0, 0.5, 0.5],
        ),
        ("Starting with 100, adding 50%, equals 150", 3, [100.0, 0.5, 150.0]),
        ("Complex equation: 1+2j + 2-3j = 3-1j", 3, [1 + 2j, 2 - 3j, 3 - 1j]),
    ],
)
def test_mixed_format_extraction(input_val, num_count, expected):
    result = to_num(input_val, num_count=num_count)
    if isinstance(result[0], complex):
        assert result == expected
    else:
        assert result == pytest.approx(expected)
