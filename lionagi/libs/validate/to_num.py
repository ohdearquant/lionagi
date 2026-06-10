from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Literal, TypeVar

NUM_TYPE_LITERAL = Literal["int", "float", "complex"]
NUM_TYPES = type[int] | type[float] | type[complex] | NUM_TYPE_LITERAL
NumericType = TypeVar("NumericType", int, float, complex)

TYPE_MAP = {"int": int, "float": float, "complex": complex}

PATTERNS = {
    "scientific": r"[-+]?(?:\d*\.)?\d+[eE][-+]?\d+",
    "complex_sci": r"[-+]?(?:\d*\.)?\d+(?:[eE][-+]?\d+)?[-+](?:\d*\.)?\d+(?:[eE][-+]?\d+)?[jJ]",
    "complex": r"[-+]?(?:\d*\.)?\d+[-+](?:\d*\.)?\d+[jJ]",
    "pure_imaginary": r"[-+]?(?:\d*\.)?\d*[jJ]",
    "percentage": r"[-+]?(?:\d*\.)?\d+%",
    "fraction": r"[-+]?\d+/\d+",
    "decimal": r"[-+]?(?:\d*\.)?\d+",
    "special": r"[-+]?(?:inf|infinity|nan)",
}


def to_num(
    input_: Any,
    /,
    *,
    upper_bound: int | float | None = None,
    lower_bound: int | float | None = None,
    num_type: NUM_TYPES = float,
    precision: int | None = None,
    num_count: int = 1,
) -> int | float | complex | list[int | float | complex]:
    """Convert input to numeric type(s) with validation and bounds checking."""
    if isinstance(input_, list | tuple):
        raise TypeError("Input cannot be a sequence")

    if isinstance(input_, bool):
        return validate_num_type(num_type)(input_)

    if isinstance(input_, int | float | complex | Decimal):
        inferred_type = type(input_)
        if isinstance(input_, Decimal):
            inferred_type = float
        value = float(input_) if not isinstance(input_, complex) else input_
        value = apply_bounds(value, upper_bound, lower_bound)
        value = apply_precision(value, precision)
        return convert_type(value, validate_num_type(num_type), inferred_type)

    input_str = str(input_)
    number_matches = extract_numbers(input_str)

    if not number_matches:
        raise ValueError(f"No valid numbers found in: {input_str}")

    results = []
    target_type = validate_num_type(num_type)

    number_matches = (
        number_matches[:num_count] if num_count < len(number_matches) else number_matches
    )

    for type_and_value in number_matches:
        try:
            inferred_type = infer_type(type_and_value)
            value = parse_number(type_and_value)
            value = apply_bounds(value, upper_bound, lower_bound)
            value = apply_precision(value, precision)
            value = convert_type(value, target_type, inferred_type)

            results.append(value)

        except Exception as e:
            if len(type_and_value) == 2:
                raise type(e)(f"Error processing {type_and_value[1]}: {str(e)}") from e
            raise type(e)(f"Error processing {type_and_value}: {str(e)}") from e

    if results and num_count == 1:
        return results[0]
    return results


def extract_numbers(text: str) -> list[tuple[str, str]]:
    combined_pattern = "|".join(PATTERNS.values())
    matches = re.finditer(combined_pattern, text, re.IGNORECASE)
    numbers = []

    for match in matches:
        value = match.group()
        for pattern_name, pattern in PATTERNS.items():
            if re.fullmatch(pattern, value, re.IGNORECASE):
                numbers.append((pattern_name, value))
                break

    return numbers


def validate_num_type(num_type: NUM_TYPES) -> type:
    if isinstance(num_type, str):
        if num_type not in TYPE_MAP:
            raise ValueError(f"Invalid number type: {num_type}")
        return TYPE_MAP[num_type]

    if num_type not in (int, float, complex):
        raise ValueError(f"Invalid number type: {num_type}")
    return num_type


def infer_type(value: tuple[str, str]) -> type:
    pattern_type, _ = value
    if pattern_type in ("complex", "complex_sci", "pure_imaginary"):
        return complex
    return float


def convert_special(value: str) -> float:
    value = value.lower()
    if "infinity" in value or "inf" in value:
        return float("-inf") if value.startswith("-") else float("inf")
    return float("nan")


def convert_percentage(value: str) -> float:
    try:
        return float(value.rstrip("%")) / 100
    except ValueError as e:
        raise ValueError(f"Invalid percentage value: {value}") from e


def convert_complex(value: str) -> complex:
    try:
        if value.endswith("j") or value.endswith("J"):
            if value in ("j", "J"):
                return complex(0, 1)
            if value in ("+j", "+J"):
                return complex(0, 1)
            if value in ("-j", "-J"):
                return complex(0, -1)
            if "+" not in value and "-" not in value[1:]:
                imag = float(value[:-1] or "1")
                return complex(0, imag)

        return complex(value.replace(" ", ""))
    except ValueError as e:
        raise ValueError(f"Invalid complex number: {value}") from e


def convert_type(
    value: float | complex,
    target_type: type,
    inferred_type: type,
) -> int | float | complex:
    try:
        if target_type is float and inferred_type is complex:
            return value

        if target_type is int and isinstance(value, complex):
            raise TypeError("Cannot convert complex number to int")
        return target_type(value)
    except (ValueError, TypeError) as e:
        raise TypeError(f"Cannot convert {value} to {target_type.__name__}") from e


def apply_bounds(
    value: float | complex,
    upper_bound: float | None = None,
    lower_bound: float | None = None,
) -> float | complex:
    if isinstance(value, complex):
        return value

    if upper_bound is not None and value > upper_bound:
        raise ValueError(f"Value {value} exceeds upper bound {upper_bound}")
    if lower_bound is not None and value < lower_bound:
        raise ValueError(f"Value {value} below lower bound {lower_bound}")
    return value


def apply_precision(
    value: float | complex,
    precision: int | None,
) -> float | complex:
    if precision is None or isinstance(value, complex):
        return value
    if isinstance(value, float):
        return round(value, precision)
    return value


def parse_number(type_and_value: tuple[str, str]) -> float | complex:
    num_type, value = type_and_value
    value = value.strip()

    try:
        if num_type == "special":
            return convert_special(value)

        if num_type == "percentage":
            return convert_percentage(value)

        if num_type == "fraction":
            if "/" not in value:
                raise ValueError(f"Invalid fraction: {value}")
            if value.count("/") > 1:
                raise ValueError(f"Invalid fraction: {value}")
            num, denom = value.split("/")
            if not (num.strip("-").isdigit() and denom.isdigit()):
                raise ValueError(f"Invalid fraction: {value}")
            denom_val = float(denom)
            if denom_val == 0:
                raise ValueError("Division by zero")
            return float(num) / denom_val
        if num_type in ("complex", "complex_sci", "pure_imaginary"):
            return convert_complex(value)
        if num_type == "scientific":
            if "e" not in value.lower():
                raise ValueError(f"Invalid scientific notation: {value}")
            parts = value.lower().split("e")
            if len(parts) != 2:
                raise ValueError(f"Invalid scientific notation: {value}")
            if not (parts[1].lstrip("+-").isdigit()):
                raise ValueError(f"Invalid scientific notation: {value}")
            return float(value)
        if num_type == "decimal":
            return float(value)

        raise ValueError(f"Unknown number type: {num_type}")
    except Exception as e:
        raise type(e)(f"Failed to parse {value} as {num_type}: {str(e)}") from e
