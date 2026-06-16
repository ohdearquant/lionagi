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
    """Coerce bool/number/string to bool; recognizes TRUE_VALUES/FALSE_VALUES; raises TypeError on None."""
    if x is None:
        raise TypeError("Cannot convert None to boolean")

    if isinstance(x, bool):
        return x

    # Handle all numeric types (including complex) using Python's bool
    if isinstance(x, (int, float, Complex)):
        return bool(x)

    if not isinstance(x, str):
        try:
            x = str(x)
        except Exception as e:
            raise TypeError(f"Cannot convert {type(x)} to boolean: {str(e)}") from e

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
