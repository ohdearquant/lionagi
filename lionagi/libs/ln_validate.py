"""
This module provides functions for validating and fixing field values based on their data types.

The module defines several functions for checking and fixing field values of different data types,
including numeric, boolean, string, and enum. It also provides a dictionary `validation_funcs` that
maps data types to their corresponding validation functions.
"""

from .ln_convert import to_str, is_same_dtype, to_list, to_dict, to_num, strip_lower
from .ln_parse import StringMatch, ParseUtil

from collections.abc import Iterable, Mapping, Generator


def validate_keys(keys):
    """
    choices can be provided from various sources:
    - mapping such as dict, their keys will be used as choices
    - iterables including list, tuple, set, generator, enum, etc.
    - strings, comma separated values
    """

    try:
        if isinstance(keys, Mapping):
            keys = list(keys.keys())
        
        elif isinstance(keys, (list, tuple, set, Generator)):
            keys = set(keys)
        
        elif isinstance(keys, str):
            if "," in keys:
                keys = list({i.strip() for i in keys.split(",")})
            else:
                keys = [keys.strip()]
                
        else:    
            keys = [i.value for i in keys]
    
    except Exception as e:
        raise ValueError(f"invalid choices {keys}") from e

    if not is_same_dtype(keys):
        raise ValueError(f"choices must be of the same type, got {keys}")

    return keys

def check_dict_field(x, keys: list[str] | dict, fix_=True, **kwargs):
    if isinstance(x, dict):
        return x
    if fix_:
        try:
            x = to_str(x)
            return StringMatch.force_validate_dict(x, keys=keys, **kwargs)
        except Exception as e:
            raise ValueError("Invalid dict field type.") from e
    raise ValueError(f"Default value for DICT must be a dict, got {type(x).__name__}")


def check_action_field(x, fix_=True, **kwargs):
    if (
        isinstance(x, list)
        and is_same_dtype(x, dict)
        and all(_has_action_keys(y) for y in x)
    ):
        return x
    try:
        x = _fix_action_field(x, fix_)
        return x
    except Exception as e:
        raise ValueError("Invalid action field type.") from e


def check_number_field(x, fix_=True, **kwargs):
    """
    Checks if the given value is a valid numeric field.

    Args:
        x: The value to check.
        fix_ (bool): Flag indicating whether to attempt fixing the value if it's invalid (default: True).
        **kwargs: Additional keyword arguments for fixing the value.

    Returns:
        The original value if it's valid, or the fixed value if `fix_` is True.

    Raises:
        ValueError: If the value is not a valid numeric field and cannot be fixed.
    """
    if not isinstance(x, (int, float)):
        if fix_:
            try:
                return _fix_number_field(x, **kwargs)
            except Exception as e:
                raise e

        raise ValueError(
            f"Default value for NUMERIC must be an int or float, got {type(x).__name__}"
        )
    return x


def check_bool_field(x, fix_=True):
    """
    Checks if the given value is a valid boolean field.

    Args:
        x: The value to check.
        fix_ (bool): Flag indicating whether to attempt fixing the value if it's invalid (default: True).

    Returns:
        The original value if it's valid, or the fixed value if `fix_` is True.

    Raises:
        ValueError: If the value is not a valid boolean field and cannot be fixed.
    """
    if not isinstance(x, bool):
        if fix_:
            try:
                return _fix_bool_field(x)
            except Exception as e:
                raise e

        raise ValueError(
            f"Default value for BOOLEAN must be a bool, got {type(x).__name__}"
        )
    return x


def check_str_field(x, *args, fix_=True, **kwargs):
    """
    Checks if the given value is a valid string field.

    Args:
        x: The value to check.
        *args: Additional positional arguments for fixing the value.
        fix_ (bool): Flag indicating whether to attempt fixing the value if it's invalid (default: True).
        **kwargs: Additional keyword arguments for fixing the value.

    Returns:
        The original value if it's valid, or the fixed value if `fix_` is True.

    Raises:
        ValueError: If the value is not a valid string field and cannot be fixed.
    """
    if not isinstance(x, str):
        if fix_:
            try:
                return _fix_str_field(x, *args, **kwargs)
            except Exception as e:
                raise e

        raise ValueError(
            f"Default value for STRING must be a str, got {type(x).__name__}"
        )
    return x


def check_enum_field(x, choices, fix_=True, **kwargs):
    """
    Checks if the given value is a valid enum field.

    Args:
        x: The value to check.
        choices: The list of valid choices for the enum field.
        fix_ (bool): Flag indicating whether to attempt fixing the value if it's invalid (default: True).
        **kwargs: Additional keyword arguments for fixing the value.

    Returns:
        The original value if it's valid, or the fixed value if `fix_` is True.

    Raises:
        ValueError: If the value is not a valid enum field and cannot be fixed.
    """
    same_dtype, dtype_ = is_same_dtype(choices, return_dtype=True)
    if not same_dtype:
        raise ValueError(
            f"Field type ENUM requires all choices to be of the same type, got {choices}"
        )

    if not isinstance(x, dtype_):
        raise ValueError(
            f"Default value for ENUM must be an instance of the {dtype_.__name__}, got {type(x).__name__}"
        )

    if x not in choices:
        if fix_:
            try:
                return _fix_enum_field(x, choices, **kwargs)
            except Exception as e:
                raise e
        raise ValueError(
            f"Default value for ENUM must be one of the {choices}, got {x}"
        )

    return x


def _has_action_keys(dict_):
    return list(dict_.keys()) >= ["function", "arguments"]


def _fix_action_field(x, discard_=True):
    corrected = []
    if isinstance(x, str):
        x = ParseUtil.fuzzy_parse_json(x)

    try:
        x = to_list(x)

        for i in x:
            i = to_dict(i)
            if _has_action_keys(i):
                corrected.append(i)
            elif not discard_:
                raise ValueError(f"Invalid action field: {i}")
    except Exception as e:
        raise ValueError(f"Invalid action field: {e}") from e

    return corrected


def _fix_number_field(x, *args, **kwargs):
    """
    Attempts to fix an invalid numeric field value.

    Args:
        x: The value to fix.
        *args: Additional positional arguments for fixing the value.
        **kwargs: Additional keyword arguments for fixing the value.

    Returns:
        The fixed numeric value.

    Raises:
        ValueError: If the value cannot be converted into a valid numeric value.
    """
    try:
        x = to_num(x, *args, **kwargs)
        if isinstance(x, (int, float)):
            return x
        raise ValueError(f"Failed to convert {x} into a numeric value")
    except Exception as e:
        raise ValueError(f"Failed to convert {x} into a numeric value") from e


def _fix_bool_field(x):
    """
    Attempts to fix an invalid boolean field value.

    Args:
        x: The value to fix.

    Returns:
        The fixed boolean value.

    Raises:
        ValueError: If the value cannot be converted into a valid boolean value.
    """
    try:
        x = strip_lower(to_str(x))
        if x in ["true", "1", "correct", "yes"]:
            return True

        elif x in ["false", "0", "incorrect", "no", "none", "n/a"]:
            return False

        raise ValueError(f"Failed to convert {x} into a boolean value")
    except Exception as e:
        raise ValueError(f"Failed to convert {x} into a boolean value") from e


def _fix_str_field(x):
    """
    Attempts to fix an invalid string field value.

    Args:
        x: The value to fix.

    Returns:
        The fixed string value.

    Raises:
        ValueError: If the value cannot be converted into a valid string value.
    """
    try:
        x = to_str(x)
        if isinstance(x, str):
            return x
        raise ValueError(f"Failed to convert {x} into a string value")
    except Exception as e:
        raise ValueError(f"Failed to convert {x} into a string value") from e


def _fix_enum_field(x, choices, **kwargs):
    """
    Attempts to fix an invalid enum field value.

    Args:
        x: The value to fix.
        choices: The list of valid choices for the enum field.
        **kwargs: Additional keyword arguments for fixing the value.

    Returns:
        The fixed enum value.

    Raises:
        ValueError: If the value cannot be converted into a valid enum value.
    """
    try:
        x = to_str(x)
        return StringMatch.choose_most_similar(x, choices, **kwargs)
    except Exception as e:
        raise ValueError(f"Failed to convert {x} into one of the choices") from e


validation_funcs = {
    "number": check_number_field,
    "bool": check_bool_field,
    "str": check_str_field,
    "enum": check_enum_field,
    "action": check_action_field,
    "dict": check_dict_field,
}
