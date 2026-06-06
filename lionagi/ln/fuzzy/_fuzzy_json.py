import contextlib
import re
from typing import Any

import orjson

# Security limit: Maximum input string size for JSON parsing.
# Large inputs can cause memory exhaustion during parsing and regex operations.
# Default: 10MB - generous for most use cases while preventing DoS.
MAX_JSON_INPUT_SIZE = 10 * 1024 * 1024  # 10 MB


def fuzzy_json(
    str_to_parse: str,
    /,
    *,
    max_size: int = MAX_JSON_INPUT_SIZE,
) -> dict[str, Any] | list:
    """Parse JSON string with fuzzy error correction (quotes, spacing, brackets).

    Security Note:
        Input size is limited to prevent memory exhaustion attacks.

    Limitation:
        This is designed for "nearly-valid" JSON from LLMs (single quotes, trailing
        commas, unquoted keys). It is NOT a security boundary for adversarial input.

    Steps:
    1. Parse directly with orjson.
    2. State-machine cleaning (safe quote/key/comma fixing).
    3. Regex-based cleaning fallback.
    4. Attempt to fix unmatched brackets using fix_json_string.
    5. If all fail, raise ValueError.

    Args:
        str_to_parse: The JSON string to parse.
        max_size: Maximum allowed input size in bytes (default: 10MB).

    Returns:
        A dict or list. Will NOT return bare primitive types
        (int, float, str, bool, None).

    Raises:
        TypeError: If input is not a string or parsed JSON is a primitive type.
        ValueError: If input is empty, exceeds size limit, or parsing fails.
    """
    _check_valid_str(str_to_parse, max_size=max_size)

    # 1. Direct attempt
    with contextlib.suppress(orjson.JSONDecodeError):
        return _validate_return_type(orjson.loads(str_to_parse))

    # 2. State-machine cleaning (preserves string content)
    cleaned = _clean_json_string_safe(str_to_parse)
    with contextlib.suppress(orjson.JSONDecodeError):
        return _validate_return_type(orjson.loads(cleaned))

    # 3. Regex-based cleaning fallback
    cleaned_regex = _clean_json_string(str_to_parse.replace("'", '"'))
    with contextlib.suppress(orjson.JSONDecodeError):
        return _validate_return_type(orjson.loads(cleaned_regex))

    # 4. Try fixing brackets (on best cleaned result)
    for candidate in (cleaned, cleaned_regex):
        fixed = fix_json_string(candidate)
        with contextlib.suppress(orjson.JSONDecodeError):
            return _validate_return_type(orjson.loads(fixed))

    # If all attempts fail
    raise ValueError("Invalid JSON string")


def _check_valid_str(
    str_to_parse: str,
    /,
    *,
    max_size: int = MAX_JSON_INPUT_SIZE,
) -> None:
    """Validate input string for JSON parsing.

    Args:
        str_to_parse: Input string to validate
        max_size: Maximum allowed size in bytes

    Raises:
        TypeError: If input is not a string
        ValueError: If input is empty or exceeds size limit
    """
    if not isinstance(str_to_parse, str):
        raise TypeError("Input must be a string")
    if not str_to_parse.strip():
        raise ValueError("Input string is empty")
    if len(str_to_parse) > max_size:
        msg = (
            f"Input size ({len(str_to_parse)} bytes) exceeds maximum "
            f"({max_size} bytes). This limit prevents memory exhaustion."
        )
        raise ValueError(msg)


def _validate_return_type(
    result: Any,
) -> dict[str, Any] | list:
    """Validate that parsed JSON is a structured type (dict or list).

    Rejects bare primitives (int, float, str, bool, None) since fuzzy_json
    is intended for structured data parsing.

    Args:
        result: The result from JSON decoding.

    Returns:
        The validated result (dict or list).

    Raises:
        TypeError: If result is a bare primitive type.
    """
    if isinstance(result, (dict, list)):
        return result

    raise TypeError(f"fuzzy_json returns dict or list, got primitive type: {type(result).__name__}")


def _clean_json_string_safe(s: str) -> str:
    """State-machine JSON cleaner that preserves string content.

    Fixes common LLM output issues without corrupting quoted content:
    - Single quotes -> double quotes (with escaping)
    - Trailing commas before ] or }
    - Unquoted object keys
    """
    result: list[str] = []
    pos = 0
    length = len(s)

    while pos < length:
        char = s[pos]

        if char == "'":  # Convert single-quoted string to double-quoted
            result.append('"')
            pos += 1
            while pos < length:
                inner_char = s[pos]
                if inner_char == "\\":  # Escape sequence
                    if pos + 1 < length:
                        next_char = s[pos + 1]
                        if next_char == "'":  # \' -> apostrophe
                            result.append("'")
                            pos += 2
                            continue
                        result.append(inner_char)
                        result.append(next_char)
                        pos += 2
                        continue
                    result.append(inner_char)
                    pos += 1
                    continue
                if inner_char == "'":  # End single-quoted string
                    result.append('"')
                    pos += 1
                    break
                if inner_char == '"':  # Escape inner double quotes
                    result.append('\\"')
                    pos += 1
                    continue
                result.append(inner_char)
                pos += 1
            continue

        if char == '"':  # Pass through double-quoted strings
            result.append(char)
            pos += 1
            while pos < length:
                inner_char = s[pos]
                if inner_char == "\\":  # Copy escape sequences
                    result.append(inner_char)
                    if pos + 1 < length:
                        pos += 1
                        result.append(s[pos])
                    pos += 1
                    continue
                result.append(inner_char)
                if inner_char == '"':
                    pos += 1
                    break
                pos += 1
            continue

        if char in "{,":  # Handle unquoted keys and trailing commas
            if char == ",":  # Check for trailing comma
                lookahead = pos + 1
                while lookahead < length and s[lookahead] in " \t\n\r":
                    lookahead += 1
                if lookahead < length and s[lookahead] in "]}":
                    pos += 1  # Skip trailing comma
                    continue

            result.append(char)
            pos += 1

            # Skip whitespace
            while pos < length and s[pos] in " \t\n\r":
                result.append(s[pos])
                pos += 1

            if pos < length and s[pos] not in "\"'{[":  # Unquoted key
                key_start = pos
                while pos < length and (s[pos].isalnum() or s[pos] == "_"):
                    pos += 1
                if pos < length and key_start < pos:
                    key_end = pos
                    while pos < length and s[pos] in " \t\n\r":
                        pos += 1
                    if pos < length and s[pos] == ":":
                        key = s[key_start:key_end]
                        result.append(f'"{key}"')
                        continue
                    else:
                        pos = key_start  # Not a key, restore
            continue

        result.append(char)
        pos += 1

    return "".join(result).strip()


def _clean_json_string(s: str) -> str:
    """Basic normalization: replace unescaped single quotes, trim spaces, ensure keys are quoted."""
    # Replace unescaped single quotes with double quotes
    # '(?<!\\)'" means a single quote not preceded by a backslash
    s = re.sub(r"(?<!\\)'", '"', s)
    # Collapse multiple whitespaces
    s = re.sub(r"\s+", " ", s)
    # Remove trailing commas before closing brackets/braces
    s = re.sub(r",\s*([}\]])", r"\1", s)
    # Ensure keys are quoted
    # This attempts to find patterns like { key: value } and turn them into {"key": value}
    s = re.sub(r'([{,])\s*([^"\s]+)\s*:', r'\1"\2":', s)
    return s.strip()


def fix_json_string(str_to_parse: str, /) -> str:
    """Try to fix JSON string by ensuring brackets are matched properly."""
    if not str_to_parse:
        raise ValueError("Input string is empty")

    brackets = {"{": "}", "[": "]"}
    open_brackets = []
    pos = 0
    length = len(str_to_parse)

    while pos < length:
        char = str_to_parse[pos]

        if char == "\\":
            pos += 2  # Skip escaped chars
            continue

        if char == '"':
            pos += 1
            # skip string content
            while pos < length:
                if str_to_parse[pos] == "\\":
                    pos += 2
                    continue
                if str_to_parse[pos] == '"':
                    pos += 1
                    break
                pos += 1
            continue

        if char in brackets:
            open_brackets.append(brackets[char])
        elif char in brackets.values():
            if not open_brackets:
                # Extra closing bracket
                # Better to raise error than guess
                raise ValueError("Extra closing bracket found.")
            if open_brackets[-1] != char:
                # Mismatched bracket
                raise ValueError("Mismatched brackets.")
            open_brackets.pop()

        pos += 1

    # Add missing closing brackets if any
    if open_brackets:
        str_to_parse += "".join(reversed(open_brackets))

    return str_to_parse
