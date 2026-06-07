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
# Coverage gap tests for to_num.py
# Missing: 64-116, 128-140, 156-158, 161, 174-177, 189-192, 207-210,
#          225-241, 262-272, 293-300, 316-320, 335-374
# ---------------------------------------------------------------------------


# --- to_num() with direct numeric inputs (lines 64-71) ---


class TestToNumDirectNumeric:
    def test_int_input(self):
        """Lines 64-71: int input → convert path."""
        assert to_num(42) == 42.0

    def test_float_input(self):
        """Lines 64-71: float input."""
        assert to_num(3.14) == pytest.approx(3.14)

    def test_complex_input_with_float_target(self):
        """Lines 64-71: complex input, target float → returned as-is (convert_type)."""
        result = to_num(2 + 3j)
        assert result == 2 + 3j

    def test_decimal_input(self):
        """Lines 66-67: Decimal → inferred_type = float."""
        result = to_num(Decimal("2.5"))
        assert result == pytest.approx(2.5)

    def test_numeric_with_precision(self):
        """Lines 70-71: apply_precision on numeric input."""
        result = to_num(3.14159, precision=2)
        assert result == 3.14

    def test_numeric_upper_bound_exceeded(self):
        """Line 69: apply_bounds raises for out-of-range."""
        with pytest.raises(ValueError, match="exceeds upper bound"):
            to_num(100.0, upper_bound=50.0)

    def test_numeric_lower_bound_violated(self):
        """Line 69: apply_bounds raises for below lower bound."""
        with pytest.raises(ValueError, match="below lower bound"):
            to_num(1.0, lower_bound=5.0)


# --- to_num() from string (lines 73-116) ---


class TestToNumFromString:
    def test_string_no_numbers_raises(self):
        """Line 78: no numbers found → ValueError."""
        with pytest.raises(ValueError, match="No valid numbers found"):
            to_num("no numbers here")

    def test_string_integer(self):
        """Lines 73-116: string '42' → 42.0."""
        assert to_num("42") == 42.0

    def test_string_float(self):
        """Lines 73-116: '3.14' → 3.14."""
        assert to_num("3.14") == pytest.approx(3.14)

    def test_string_percentage(self):
        """Lines 73-116: '50%' → 0.5."""
        assert to_num("50%") == pytest.approx(0.5)

    def test_string_fraction(self):
        """Lines 73-116: '1/2' → 0.5."""
        assert to_num("1/2") == pytest.approx(0.5)

    def test_string_complex(self):
        """Lines 73-116: '1+2j' → complex."""
        result = to_num("1+2j", num_type=complex)
        assert result == complex(1, 2)

    def test_string_scientific(self):
        """Lines 73-116: '1e3' → 1000.0."""
        assert to_num("1e3") == 1000.0

    def test_string_special_inf(self):
        """Lines 73-116: 'inf' → float('inf')."""
        assert to_num("inf") == float("inf")

    def test_num_count_limits_results(self):
        """Lines 84-88: num_count=1 takes only first match."""
        result = to_num("1 2 3", num_count=1)
        assert result == 1.0

    def test_num_count_multiple_returns_list(self):
        """Lines 114-116: num_count>1 → list."""
        result = to_num("1.0 2.0", num_count=2)
        assert result == pytest.approx([1.0, 2.0])

    def test_string_with_num_type_int(self):
        """Lines 73-116: num_type='int' from string."""
        result = to_num("42", num_type="int")
        assert result == 42
        assert isinstance(result, int)


# --- extract_numbers (lines 128-140) ---


class TestExtractNumbers:
    def test_extracts_decimal(self):
        """Lines 128-140: decimal pattern matched."""
        matches = extract_numbers("value is 3.14")
        assert any(v == "3.14" for _, v in matches)

    def test_extracts_percentage(self):
        """Lines 128-140: percentage pattern matched."""
        matches = extract_numbers("50% done")
        assert any(t == "percentage" for t, _ in matches)

    def test_extracts_fraction(self):
        """Lines 128-140: fraction pattern matched."""
        matches = extract_numbers("ratio is 1/3")
        assert any(t == "fraction" for t, _ in matches)

    def test_extracts_scientific(self):
        """Lines 128-140: scientific notation matched."""
        matches = extract_numbers("1e10 atoms")
        assert any(t == "scientific" for t, _ in matches)

    def test_no_numbers_returns_empty(self):
        """Lines 128-140: no numbers → empty list."""
        assert extract_numbers("no digits here") == []

    def test_extracts_complex(self):
        """Lines 128-140: complex number matched."""
        matches = extract_numbers("z = 1+2j")
        assert any(t in ("complex", "complex_sci", "pure_imaginary") for t, _ in matches)


# --- validate_num_type (lines 156-161) ---


class TestValidateNumType:
    def test_invalid_string_raises(self):
        """Lines 156-158: invalid str type → ValueError."""
        with pytest.raises(ValueError, match="Invalid number type"):
            validate_num_type("str")

    def test_invalid_type_raises(self):
        """Line 161: non-int/float/complex type → ValueError."""
        with pytest.raises(ValueError, match="Invalid number type"):
            validate_num_type(str)

    def test_valid_string_int(self):
        """Lines 155-158: 'int' → int type."""
        assert validate_num_type("int") is int

    def test_valid_string_float(self):
        assert validate_num_type("float") is float

    def test_valid_string_complex(self):
        assert validate_num_type("complex") is complex

    def test_valid_type_objects(self):
        """Line 160: type objects pass through."""
        assert validate_num_type(int) is int
        assert validate_num_type(float) is float
        assert validate_num_type(complex) is complex


# --- infer_type (lines 174-177) ---


class TestInferType:
    def test_complex_pattern_returns_complex(self):
        """Lines 174-176: complex/complex_sci/pure_imaginary → complex."""
        assert infer_type(("complex", "1+2j")) is complex
        assert infer_type(("complex_sci", "1e2+3e1j")) is complex
        assert infer_type(("pure_imaginary", "2j")) is complex

    def test_other_pattern_returns_float(self):
        """Line 177: decimal/fraction/etc → float."""
        assert infer_type(("decimal", "3.14")) is float
        assert infer_type(("fraction", "1/2")) is float


# --- convert_special (lines 189-192) ---


class TestConvertSpecial:
    def test_inf(self):
        """Lines 189-191: 'inf' → float('inf')."""
        assert convert_special("inf") == float("inf")

    def test_negative_inf(self):
        """Lines 189-191: '-inf' → float('-inf')."""
        assert convert_special("-inf") == float("-inf")

    def test_infinity_word(self):
        """Lines 189-191: 'infinity' → float('inf')."""
        assert convert_special("infinity") == float("inf")

    def test_nan(self):
        """Line 192: 'nan' → float('nan')."""
        assert math.isnan(convert_special("nan"))


# --- convert_percentage (lines 207-210) ---


class TestConvertPercentage:
    def test_valid_percentage(self):
        """Lines 207-208: '50%' → 0.5."""
        assert convert_percentage("50%") == pytest.approx(0.5)

    def test_invalid_percentage_raises(self):
        """Lines 209-210: non-numeric before % → ValueError."""
        with pytest.raises(ValueError, match="Invalid percentage"):
            convert_percentage("abc%")


# --- convert_complex (lines 225-241) ---


class TestConvertComplex:
    def test_pure_j(self):
        """Line 228-229: 'j' → complex(0, 1)."""
        assert convert_complex("j") == complex(0, 1)

    def test_plus_j(self):
        """Lines 230-231: '+j' → complex(0, 1)."""
        assert convert_complex("+j") == complex(0, 1)

    def test_minus_j(self):
        """Lines 232-233: '-j' → complex(0, -1)."""
        assert convert_complex("-j") == complex(0, -1)

    def test_pure_imaginary_number(self):
        """Lines 234-237: '5j' → complex(0, 5)."""
        result = convert_complex("5j")
        assert result == complex(0, 5)

    def test_standard_complex(self):
        """Line 239: '1+2j' → complex(1, 2)."""
        assert convert_complex("1+2j") == complex(1, 2)

    def test_invalid_complex_raises(self):
        """Lines 240-241: invalid complex string → ValueError."""
        with pytest.raises(ValueError, match="Invalid complex"):
            convert_complex("xyz")


# --- convert_type (lines 262-272) ---


class TestConvertType:
    def test_float_target_complex_inferred_returns_value(self):
        """Lines 264-265: target=float, inferred=complex → return as-is."""
        result = convert_type(2 + 3j, float, complex)
        assert result == 2 + 3j

    def test_int_target_complex_value_raises(self):
        """Lines 268-269: target=int, complex value → TypeError."""
        with pytest.raises(TypeError, match="Cannot convert"):
            convert_type(2 + 3j, int, complex)

    def test_int_target_float_value(self):
        """Line 270: target=int, float value → truncated int."""
        assert convert_type(3.9, int, float) == 3

    def test_incompatible_conversion_raises_type_error(self):
        """Lines 271-272: ValueError from conversion → re-raised as TypeError."""
        with pytest.raises(TypeError):
            convert_type("abc", int, float)


# --- apply_bounds (lines 293-300) ---


class TestApplyBounds:
    def test_complex_value_skips_bounds(self):
        """Lines 293-294: complex → returned without bounds check."""
        v = 1 + 2j
        assert apply_bounds(v, upper_bound=0) is v

    def test_upper_bound_exceeded(self):
        """Lines 296-297: value > upper_bound → ValueError."""
        with pytest.raises(ValueError, match="exceeds upper bound"):
            apply_bounds(10.0, upper_bound=5.0)

    def test_lower_bound_violated(self):
        """Lines 298-299: value < lower_bound → ValueError."""
        with pytest.raises(ValueError, match="below lower bound"):
            apply_bounds(1.0, lower_bound=5.0)

    def test_within_bounds_returned(self):
        """Line 300: in-bounds value returned unchanged."""
        assert apply_bounds(5.0, upper_bound=10.0, lower_bound=0.0) == 5.0


# --- apply_precision (lines 316-320) ---


class TestApplyPrecision:
    def test_float_rounded(self):
        """Lines 318-319: float → round(value, precision)."""
        assert apply_precision(3.14159, 2) == pytest.approx(3.14)

    def test_int_returned_unchanged(self):
        """Line 320: int value → returned as-is (not float)."""
        result = apply_precision(42, 2)
        assert result == 42
        assert isinstance(result, int)

    def test_none_precision_returns_unchanged(self):
        """Line 316: precision=None → return value."""
        assert apply_precision(3.14, None) == 3.14

    def test_complex_skips_rounding(self):
        """Line 316: complex → return as-is."""
        v = 1 + 2j
        assert apply_precision(v, 2) is v


# --- parse_number (lines 335-374) ---


class TestParseNumber:
    def test_special_inf(self):
        """Lines 339-340: 'special' type → convert_special."""
        assert parse_number(("special", "inf")) == float("inf")

    def test_special_nan(self):
        """Lines 339-340: 'special' nan."""
        assert math.isnan(parse_number(("special", "nan")))

    def test_percentage(self):
        """Lines 342-343: 'percentage' → convert_percentage."""
        assert parse_number(("percentage", "50%")) == pytest.approx(0.5)

    def test_fraction_valid(self):
        """Lines 345-356: '1/2' → 0.5."""
        assert parse_number(("fraction", "1/2")) == pytest.approx(0.5)

    def test_fraction_zero_denominator_raises(self):
        """Lines 353-355: '1/0' → ValueError."""
        with pytest.raises(ValueError, match="Division by zero"):
            parse_number(("fraction", "1/0"))

    def test_fraction_missing_slash_raises(self):
        """Lines 346-348: no '/' in fraction → ValueError."""
        with pytest.raises(ValueError):
            parse_number(("fraction", "42"))

    def test_fraction_non_digit_raises(self):
        """Lines 351-352: non-digit parts → ValueError."""
        with pytest.raises(ValueError):
            parse_number(("fraction", "a/b"))

    def test_complex_pattern(self):
        """Lines 357-358: 'complex' → convert_complex."""
        result = parse_number(("complex", "1+2j"))
        assert result == complex(1, 2)

    def test_pure_imaginary_pattern(self):
        """Lines 357-358: 'pure_imaginary' → convert_complex."""
        result = parse_number(("pure_imaginary", "3j"))
        assert result == complex(0, 3)

    def test_scientific_valid(self):
        """Lines 359-367: '1e3' → 1000.0."""
        assert parse_number(("scientific", "1e3")) == 1000.0

    def test_scientific_no_e_raises(self):
        """Lines 360-361: scientific without 'e' → ValueError."""
        with pytest.raises(ValueError):
            parse_number(("scientific", "1000"))

    def test_decimal(self):
        """Lines 368-369: 'decimal' → float."""
        assert parse_number(("decimal", "3.14")) == pytest.approx(3.14)

    def test_unknown_type_raises(self):
        """Line 371: unknown type → ValueError."""
        with pytest.raises(ValueError, match="Unknown number type"):
            parse_number(("unknown_type", "3.14"))

    def test_fraction_multiple_slashes_raises(self):
        """Line 349: '1/2/3' → ValueError."""
        with pytest.raises(ValueError):
            parse_number(("fraction", "1/2/3"))

    def test_scientific_multiple_e_raises(self):
        """Line 364: '1e2e3' → ValueError (len(parts) != 2)."""
        with pytest.raises(ValueError):
            parse_number(("scientific", "1e2e3"))

    def test_scientific_non_digit_exponent_raises(self):
        """Line 366: non-digit exponent → ValueError."""
        with pytest.raises(ValueError):
            parse_number(("scientific", "1exyz"))


# --- to_num() processing error wrapping (lines 109-111) ---


class TestToNumProcessingErrorContext:
    def test_complex_to_int_raises_with_context(self):
        """Lines 109-111: processing error wraps with value context string."""
        with pytest.raises((TypeError, ValueError), match="Error processing"):
            to_num("1+2j", num_type=int)

    def test_percentage_exceeds_bound_raises_with_context(self):
        """Lines 109-111: parsed value out of bounds → wrapped error."""
        with pytest.raises(ValueError, match="Error processing"):
            to_num("50%", upper_bound=0.3)
