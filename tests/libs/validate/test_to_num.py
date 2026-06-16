import math
from decimal import Decimal

import pytest

from lionagi.libs.validate.to_num import (
    apply_bounds,
    apply_precision,
    convert_complex,
    convert_percentage,
    convert_special,
    convert_type,
    extract_numbers,
    infer_type,
    parse_number,
    to_num,
    validate_num_type,
)


def test_to_num_rejects_sequence():
    with pytest.raises(TypeError):
        to_num([1, 2])


@pytest.mark.parametrize(
    "value, num_type, expected",
    [
        (True, int, 1),
        (False, float, 0.0),
    ],
)
def test_to_num_converts_bool_with_requested_type(value, num_type, expected):
    result = to_num(value, num_type=num_type)
    assert result == expected
    assert isinstance(result, num_type)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


class TestToNumDirectNumeric:
    def test_int_input(self):
        assert to_num(42) == 42.0

    def test_float_input(self):
        assert to_num(3.14) == pytest.approx(3.14)

    def test_complex_input_with_float_target(self):
        result = to_num(2 + 3j)
        assert result == 2 + 3j

    def test_decimal_input(self):
        result = to_num(Decimal("2.5"))
        assert result == pytest.approx(2.5)

    def test_numeric_with_precision(self):
        result = to_num(3.14159, precision=2)
        assert result == 3.14

    def test_numeric_upper_bound_exceeded(self):
        with pytest.raises(ValueError, match="exceeds upper bound"):
            to_num(100.0, upper_bound=50.0)

    def test_numeric_lower_bound_violated(self):
        with pytest.raises(ValueError, match="below lower bound"):
            to_num(1.0, lower_bound=5.0)


class TestToNumFromString:
    def test_string_no_numbers_raises(self):
        with pytest.raises(ValueError, match="No valid numbers found"):
            to_num("no numbers here")

    def test_string_integer(self):
        assert to_num("42") == 42.0

    def test_string_float(self):
        assert to_num("3.14") == pytest.approx(3.14)

    def test_string_percentage(self):
        assert to_num("50%") == pytest.approx(0.5)

    def test_string_fraction(self):
        assert to_num("1/2") == pytest.approx(0.5)

    def test_string_complex(self):
        result = to_num("1+2j", num_type=complex)
        assert result == complex(1, 2)

    def test_string_scientific(self):
        assert to_num("1e3") == 1000.0

    def test_string_special_inf(self):
        assert to_num("inf") == float("inf")

    def test_num_count_limits_results(self):
        result = to_num("1 2 3", num_count=1)
        assert result == 1.0

    def test_num_count_multiple_returns_list(self):
        result = to_num("1.0 2.0", num_count=2)
        assert result == pytest.approx([1.0, 2.0])

    def test_string_with_num_type_int(self):
        result = to_num("42", num_type="int")
        assert result == 42
        assert isinstance(result, int)


class TestExtractNumbers:
    def test_extracts_decimal(self):
        matches = extract_numbers("value is 3.14")
        assert any(v == "3.14" for _, v in matches)

    def test_extracts_percentage(self):
        matches = extract_numbers("50% done")
        assert any(t == "percentage" for t, _ in matches)

    def test_extracts_fraction(self):
        matches = extract_numbers("ratio is 1/3")
        assert any(t == "fraction" for t, _ in matches)

    def test_extracts_scientific(self):
        matches = extract_numbers("1e10 atoms")
        assert any(t == "scientific" for t, _ in matches)

    def test_no_numbers_returns_empty(self):
        assert extract_numbers("no digits here") == []

    def test_extracts_complex(self):
        matches = extract_numbers("z = 1+2j")
        assert any(t in ("complex", "complex_sci", "pure_imaginary") for t, _ in matches)


class TestValidateNumType:
    def test_invalid_string_raises(self):
        with pytest.raises(ValueError, match="Invalid number type"):
            validate_num_type("str")

    def test_invalid_type_raises(self):
        with pytest.raises(ValueError, match="Invalid number type"):
            validate_num_type(str)

    def test_valid_string_int(self):
        assert validate_num_type("int") is int

    def test_valid_string_float(self):
        assert validate_num_type("float") is float

    def test_valid_string_complex(self):
        assert validate_num_type("complex") is complex

    def test_valid_type_objects(self):
        assert validate_num_type(int) is int
        assert validate_num_type(float) is float
        assert validate_num_type(complex) is complex


class TestInferType:
    def test_complex_pattern_returns_complex(self):
        assert infer_type(("complex", "1+2j")) is complex
        assert infer_type(("complex_sci", "1e2+3e1j")) is complex
        assert infer_type(("pure_imaginary", "2j")) is complex

    def test_other_pattern_returns_float(self):
        assert infer_type(("decimal", "3.14")) is float
        assert infer_type(("fraction", "1/2")) is float


class TestConvertSpecial:
    def test_inf(self):
        assert convert_special("inf") == float("inf")

    def test_negative_inf(self):
        assert convert_special("-inf") == float("-inf")

    def test_infinity_word(self):
        assert convert_special("infinity") == float("inf")

    def test_nan(self):
        assert math.isnan(convert_special("nan"))


class TestConvertPercentage:
    def test_valid_percentage(self):
        assert convert_percentage("50%") == pytest.approx(0.5)

    def test_invalid_percentage_raises(self):
        with pytest.raises(ValueError, match="Invalid percentage"):
            convert_percentage("abc%")


class TestConvertComplex:
    def test_pure_j(self):
        assert convert_complex("j") == complex(0, 1)

    def test_plus_j(self):
        assert convert_complex("+j") == complex(0, 1)

    def test_minus_j(self):
        assert convert_complex("-j") == complex(0, -1)

    def test_pure_imaginary_number(self):
        result = convert_complex("5j")
        assert result == complex(0, 5)

    def test_standard_complex(self):
        assert convert_complex("1+2j") == complex(1, 2)

    def test_invalid_complex_raises(self):
        with pytest.raises(ValueError, match="Invalid complex"):
            convert_complex("xyz")


class TestConvertType:
    def test_float_target_complex_inferred_returns_value(self):
        result = convert_type(2 + 3j, float, complex)
        assert result == 2 + 3j

    def test_int_target_complex_value_raises(self):
        with pytest.raises(TypeError, match="Cannot convert"):
            convert_type(2 + 3j, int, complex)

    def test_int_target_float_value(self):
        assert convert_type(3.9, int, float) == 3

    def test_incompatible_conversion_raises_type_error(self):
        with pytest.raises(TypeError):
            convert_type("abc", int, float)


class TestApplyBounds:
    def test_complex_value_skips_bounds(self):
        v = 1 + 2j
        assert apply_bounds(v, upper_bound=0) is v

    def test_upper_bound_exceeded(self):
        with pytest.raises(ValueError, match="exceeds upper bound"):
            apply_bounds(10.0, upper_bound=5.0)

    def test_lower_bound_violated(self):
        with pytest.raises(ValueError, match="below lower bound"):
            apply_bounds(1.0, lower_bound=5.0)

    def test_within_bounds_returned(self):
        assert apply_bounds(5.0, upper_bound=10.0, lower_bound=0.0) == 5.0


class TestApplyPrecision:
    def test_float_rounded(self):
        assert apply_precision(3.14159, 2) == pytest.approx(3.14)

    def test_int_returned_unchanged(self):
        result = apply_precision(42, 2)
        assert result == 42
        assert isinstance(result, int)

    def test_none_precision_returns_unchanged(self):
        assert apply_precision(3.14, None) == 3.14

    def test_complex_skips_rounding(self):
        v = 1 + 2j
        assert apply_precision(v, 2) is v


class TestParseNumber:
    def test_special_inf(self):
        assert parse_number(("special", "inf")) == float("inf")

    def test_special_nan(self):
        assert math.isnan(parse_number(("special", "nan")))

    def test_percentage(self):
        assert parse_number(("percentage", "50%")) == pytest.approx(0.5)

    def test_fraction_valid(self):
        assert parse_number(("fraction", "1/2")) == pytest.approx(0.5)

    def test_fraction_zero_denominator_raises(self):
        with pytest.raises(ValueError, match="Division by zero"):
            parse_number(("fraction", "1/0"))

    def test_fraction_missing_slash_raises(self):
        with pytest.raises(ValueError):
            parse_number(("fraction", "42"))

    def test_fraction_non_digit_raises(self):
        with pytest.raises(ValueError):
            parse_number(("fraction", "a/b"))

    def test_complex_pattern(self):
        result = parse_number(("complex", "1+2j"))
        assert result == complex(1, 2)

    def test_pure_imaginary_pattern(self):
        result = parse_number(("pure_imaginary", "3j"))
        assert result == complex(0, 3)

    def test_scientific_valid(self):
        assert parse_number(("scientific", "1e3")) == 1000.0

    def test_scientific_no_e_raises(self):
        with pytest.raises(ValueError):
            parse_number(("scientific", "1000"))

    def test_decimal(self):
        assert parse_number(("decimal", "3.14")) == pytest.approx(3.14)

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown number type"):
            parse_number(("unknown_type", "3.14"))

    def test_fraction_multiple_slashes_raises(self):
        with pytest.raises(ValueError):
            parse_number(("fraction", "1/2/3"))

    def test_scientific_multiple_e_raises(self):
        with pytest.raises(ValueError):
            parse_number(("scientific", "1e2e3"))

    def test_scientific_non_digit_exponent_raises(self):
        with pytest.raises(ValueError):
            parse_number(("scientific", "1exyz"))


class TestToNumProcessingErrorContext:
    def test_complex_to_int_raises_with_context(self):
        with pytest.raises((TypeError, ValueError), match="Error processing"):
            to_num("1+2j", num_type=int)

    def test_percentage_exceeds_bound_raises_with_context(self):
        with pytest.raises(ValueError, match="Error processing"):
            to_num("50%", upper_bound=0.3)
