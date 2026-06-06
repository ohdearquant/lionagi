# Copyright (c) 2023-2025, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from numbers import Complex
from typing import Any, Final

# Define constants for valid boolean string representations
TRUE_VALUES: Final[frozenset[str]] = frozenset(
    [
        "true",
        "1",
        "yes",
        "y",
        "on",
        "correct",
        "t",
        "enabled",
        "enable",
        "active",
        "activated",
    ]
)

FALSE_VALUES: Final[frozenset[str]] = frozenset(
    [
        "false",
        "0",
        "no",
        "n",
        "off",
        "incorrect",
        "f",
        "disabled",
        "disable",
        "inactive",
        "deactivated",
        "none",
        "null",
        "n/a",
        "na",
    ]
)


def validate_boolean(x: Any, /) -> bool:
    """
    Forcefully validate and convert the input into a boolean value.

    This function attempts to convert various input types to a boolean value.
    It recognizes common string representations of true and false, as well
    as numeric values. The conversion is case-insensitive.

    Args:
        x: The input to be converted to boolean. Can be:
           - Boolean: returned as-is
           - Number (including complex): converted using Python's bool rules
           - String: converted based on common boolean representations
           - None: raises TypeError
           - Other types: converted to string and then evaluated

    Returns:
        bool: The boolean representation of the input.

    Raises:
        ValueError: If the input cannot be unambiguously converted to a boolean value.
        TypeError: If the input type is unsupported or None.

    Examples:
        >>> validate_boolean(True)
        True
        >>> validate_boolean("yes")
        True
        >>> validate_boolean("OFF")
        False
        >>> validate_boolean(1)
        True
        >>> validate_boolean(0j)
        False
        >>> validate_boolean(1 + 1j)
        True

    Notes:
        - String matching is case-insensitive
        - Leading/trailing whitespace is stripped
        - Numeric values follow Python's bool() rules
        - Complex numbers: bool(0j) is False, bool(any other complex) is True
        - None values raise TypeError
        - Empty strings raise ValueError
    """
    if x is None:
        raise TypeError("Cannot convert None to boolean")

    if isinstance(x, bool):
        return x

    # Handle all numeric types (including complex) using Python's bool
    if isinstance(x, int | float | Complex):
        return bool(x)

    # Convert to string if not already a string
    if not isinstance(x, str):
        try:
            x = str(x)
        except Exception as e:
            raise TypeError(f"Cannot convert {type(x)} to boolean: {str(e)}") from e

    # Handle string inputs
    x_cleaned = str(x).strip().lower()

    if not x_cleaned:
        raise ValueError("Cannot convert empty string to boolean")

    if x_cleaned in TRUE_VALUES:
        return True

    if x_cleaned in FALSE_VALUES:
        return False

    # Try numeric conversion as a last resort
    try:
        # Try to evaluate as a literal if it looks like a complex number
        if "j" in x_cleaned:
            try:
                return bool(complex(x_cleaned))
            except ValueError:
                # Not a valid complex literal; fall through to float attempt
                pass
        return bool(float(x_cleaned))
    except ValueError:
        # Not a valid float either; fall through to the final ValueError below
        pass

    raise ValueError(
        f"Cannot convert '{x}' to boolean. Valid true values are: {sorted(TRUE_VALUES)}, "
        f"valid false values are: {sorted(FALSE_VALUES)}"
    )
