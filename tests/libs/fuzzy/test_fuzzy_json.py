"""Tests for lionagi/ln/fuzzy/_fuzzy_json.py

Target: Cover lines 88-89 (escaped character handling in fix_json_string)
+ security/robustness enhancements (max_size, return type validation, state-machine cleaner)
"""

import pytest

from lionagi.ln.fuzzy._fuzzy_json import (
    MAX_JSON_INPUT_SIZE,
    _check_valid_str,
    _clean_json_string,
    _clean_json_string_safe,
    _validate_return_type,
    fix_json_string,
    fuzzy_json,
)

# ============================================================================
# Test fuzzy_json main function
# ============================================================================


def test_fuzzy_json_valid():
    result = fuzzy_json('{"key": "value"}')
    assert result == {"key": "value"}


def test_fuzzy_json_single_quotes():
    result = fuzzy_json("{'key': 'value'}")
    assert result == {"key": "value"}


def test_fuzzy_json_unquoted_keys():
    result = fuzzy_json("{key: 'value'}")
    assert result == {"key": "value"}


def test_fuzzy_json_trailing_commas():
    result = fuzzy_json('{"key": "value",}')
    assert result == {"key": "value"}


def test_fuzzy_json_missing_closing_bracket():
    result = fuzzy_json('{"key": "value"')
    assert result == {"key": "value"}


def test_fuzzy_json_invalid():
    with pytest.raises(ValueError, match="Invalid JSON string"):
        fuzzy_json("{completely broken")


def test_fuzzy_json_not_string():
    with pytest.raises(TypeError, match="Input must be a string"):
        fuzzy_json(123)


def test_fuzzy_json_empty():
    with pytest.raises(ValueError, match="Input string is empty"):
        fuzzy_json("")


def test_fuzzy_json_whitespace_only():
    with pytest.raises(ValueError, match="Input string is empty"):
        fuzzy_json("   ")


# ============================================================================
# Test _check_valid_str
# ============================================================================


def test_check_valid_str_valid():
    result = _check_valid_str("valid string")
    assert result is None


def test_check_valid_str_not_string():
    with pytest.raises(TypeError, match="Input must be a string"):
        _check_valid_str(123)


def test_check_valid_str_empty():
    with pytest.raises(ValueError, match="Input string is empty"):
        _check_valid_str("")


# ============================================================================
# Test _clean_json_string
# ============================================================================


def test_clean_json_string_single_quotes():
    result = _clean_json_string("{'key': 'value'}")
    assert '"' in result


def test_clean_json_string_trailing_comma():
    result = _clean_json_string('{"key": "value",}')
    assert result == '{"key": "value"}' or '","' not in result


def test_clean_json_string_whitespace():
    result = _clean_json_string('{"key":   "value"}')
    assert "  " not in result


def test_clean_json_string_unquoted_keys():
    result = _clean_json_string('{key: "value"}')
    assert '"key"' in result


# ============================================================================
# Test fix_json_string - Lines 88-89 (escaped chars)
# ============================================================================


def test_fix_json_string_escaped_backslash():
    # JSON string with escaped characters
    json_str = r'{"path": "C:\\Users\\file.txt"}'
    result = fix_json_string(json_str)
    # Should handle escaped backslashes properly
    assert result == json_str  # Should be unchanged


def test_fix_json_string_escaped_quote():
    json_str = r'{"text": "He said \"hello\""}'
    result = fix_json_string(json_str)
    # Should handle escaped quotes properly
    assert result == json_str


def test_fix_json_string_escaped_newline():
    json_str = r'{"text": "line1\nline2"}'
    result = fix_json_string(json_str)
    assert result == json_str


def test_fix_json_string_multiple_escapes():
    json_str = r'{"path": "C:\\folder\\file", "text": "quote: \"hi\"\nend"}'
    result = fix_json_string(json_str)
    # Should handle all escapes properly
    assert result == json_str


def test_fix_json_string_missing_bracket():
    json_str = '{"key": "value"'
    result = fix_json_string(json_str)
    assert result == '{"key": "value"}'


def test_fix_json_string_missing_multiple_brackets():
    json_str = '{"key": {"nested": "value"'
    result = fix_json_string(json_str)
    assert result == '{"key": {"nested": "value"}}'


def test_fix_json_string_missing_array_bracket():
    json_str = '["item1", "item2"'
    result = fix_json_string(json_str)
    assert result == '["item1", "item2"]'


def test_fix_json_string_extra_closing_bracket():
    json_str = '{"key": "value"}}'
    with pytest.raises(ValueError, match="Extra closing bracket"):
        fix_json_string(json_str)


def test_fix_json_string_mismatched_brackets():
    json_str = '{"key": "value"]'
    with pytest.raises(ValueError, match="Mismatched brackets"):
        fix_json_string(json_str)


def test_fix_json_string_empty():
    with pytest.raises(ValueError, match="Input string is empty"):
        fix_json_string("")


def test_fix_json_string_complex_with_escapes():
    json_str = r'{"data": {"path": "C:\\test\\", "text": "say \"hi\"", "newline": "a\nb"'
    result = fix_json_string(json_str)
    # Should add missing closing brackets while preserving escapes
    assert result.endswith("}}")
    assert "\\\\" in result or "\\test" in result  # Escapes preserved


def test_fix_json_string_escape_at_end():
    json_str = r'{"path": "folder\\"'
    result = fix_json_string(json_str)
    # Should handle backslash at end and add missing bracket
    assert result.endswith("}")


def test_fuzzy_json_with_escapes_comprehensive():
    # Test that fuzzy_json can handle JSON with escapes through the full pipeline
    json_str = r'{"file": "C:\\Users\\test.txt", "quote": "He said \"hello\""}'
    result = fuzzy_json(json_str)
    assert result["file"] == "C:\\Users\\test.txt"
    assert result["quote"] == 'He said "hello"'


def test_fix_json_string_escaped_chars_in_array():
    json_str = r'["item1", "C:\\path\\file", "text with \"quotes\""'
    result = fix_json_string(json_str)
    assert result.endswith("]")
    assert "\\\\" in result or "\\path" in result


def test_fix_json_string_nested_with_escapes():
    json_str = r'{"outer": {"inner": "path\\to\\file"'
    result = fix_json_string(json_str)
    # Should add two closing brackets and preserve escapes
    assert result.count("}") == 2
    assert "path" in result


# ============================================================================
# Test MAX_JSON_INPUT_SIZE and max_size enforcement
# ============================================================================


def test_max_json_input_size_constant():
    assert MAX_JSON_INPUT_SIZE == 10 * 1024 * 1024


def test_check_valid_str_exceeds_max_size():
    with pytest.raises(ValueError, match="exceeds maximum"):
        _check_valid_str("x" * 100, max_size=50)


def test_check_valid_str_at_max_size():
    result = _check_valid_str("x" * 50, max_size=50)
    assert result is None


def test_fuzzy_json_max_size_enforcement():
    with pytest.raises(ValueError, match="exceeds maximum"):
        fuzzy_json('{"key": "value"}', max_size=5)


def test_fuzzy_json_max_size_default_accepts_normal():
    result = fuzzy_json('{"key": "value"}')
    assert result == {"key": "value"}


# ============================================================================
# Test _validate_return_type
# ============================================================================


def test_validate_return_type_dict():
    result = _validate_return_type({"key": "value"})
    assert result == {"key": "value"}


def test_validate_return_type_list_of_dicts():
    data = [{"a": 1}, {"b": 2}]
    result = _validate_return_type(data)
    assert result == data


def test_validate_return_type_empty_list():
    result = _validate_return_type([])
    assert result == []


def test_validate_return_type_rejects_primitive_string():
    with pytest.raises(TypeError, match="primitive type: str"):
        _validate_return_type("hello")


def test_validate_return_type_rejects_primitive_int():
    with pytest.raises(TypeError, match="primitive type: int"):
        _validate_return_type(42)


def test_validate_return_type_rejects_primitive_none():
    with pytest.raises(TypeError, match="primitive type: NoneType"):
        _validate_return_type(None)


def test_validate_return_type_accepts_list_of_non_dicts():
    result = _validate_return_type([{"a": 1}, "not a dict"])
    assert result == [{"a": 1}, "not a dict"]


def test_validate_return_type_accepts_list_of_primitives():
    result = _validate_return_type([1, 2, 3])
    assert result == [1, 2, 3]


def test_fuzzy_json_rejects_primitive_json():
    with pytest.raises(TypeError, match="primitive type"):
        fuzzy_json('"just a string"')


def test_fuzzy_json_rejects_json_number():
    with pytest.raises(TypeError, match="primitive type"):
        fuzzy_json("42")


# ============================================================================
# Test _clean_json_string_safe (state-machine cleaner)
# ============================================================================


def test_clean_json_string_safe_single_quotes():
    result = _clean_json_string_safe("{'key': 'value'}")
    assert result == '{"key": "value"}'


def test_clean_json_string_safe_trailing_comma():
    result = _clean_json_string_safe('{"key": "value",}')
    assert result == '{"key": "value"}'


def test_clean_json_string_safe_unquoted_keys():
    result = _clean_json_string_safe('{key: "value"}')
    assert '"key"' in result


def test_clean_json_string_safe_preserves_double_quoted():
    result = _clean_json_string_safe('{"key": "value with spaces"}')
    assert result == '{"key": "value with spaces"}'


def test_clean_json_string_safe_escapes_inner_double_quotes():
    result = _clean_json_string_safe("{'key': 'value with \"quotes\"'}")
    assert '\\"quotes\\"' in result


def test_clean_json_string_safe_handles_escaped_single_quote():
    result = _clean_json_string_safe("{'key': 'it\\'s a test'}")
    assert "'s a test" in result


def test_clean_json_string_safe_preserves_escape_sequences():
    input_str = r'{"path": "C:\\Users\\file.txt"}'
    result = _clean_json_string_safe(input_str)
    assert result == input_str


def test_clean_json_string_safe_trailing_comma_in_array():
    result = _clean_json_string_safe('["a", "b",]')
    assert result == '["a", "b"]'


def test_clean_json_string_safe_mixed_fixes():
    result = _clean_json_string_safe("{key: 'value',}")
    # Should quote key, convert single quotes, remove trailing comma
    assert '"key"' in result
    assert "'" not in result or "'" in result  # Just check it parses
    # The ultimate test: can we parse it?
    import msgspec

    parsed = msgspec.json.decode(result.encode("utf-8"))
    assert parsed == {"key": "value"}


# ============================================================================
# Edge cases: spec group "libs"
# ============================================================================


def test_check_valid_str_exactly_at_max_size_boundary():
    """Input of exactly max_size characters should be accepted (not > max_size)."""
    # _check_valid_str raises when len > max_size; len == max_size must pass
    size = 100
    s = "x" * size
    # Should not raise
    _check_valid_str(s, max_size=size)


def test_check_valid_str_one_over_max_size_boundary():
    """Input of max_size + 1 characters should be rejected."""
    size = 100
    s = "x" * (size + 1)
    with pytest.raises(ValueError, match="exceeds maximum"):
        _check_valid_str(s, max_size=size)


def test_fuzzy_json_deeply_nested_valid():
    """Deeply nested JSON (within typical stack limits) should parse correctly."""
    depth = 20
    # Build: {"a": {"a": {"a": ... {"k": "v"} ...}}}
    inner = '{"k": "v"}'
    for _ in range(depth):
        inner = '{"a": ' + inner + "}"
    result = fuzzy_json(inner)
    assert isinstance(result, dict)


def test_fuzzy_json_unicode_escape_with_single_quote_value():
    """Unicode escape sequences inside a single-quoted value should round-trip."""
    s = "{'key': 'caf\\u00e9'}"
    result = fuzzy_json(s)
    assert isinstance(result, dict)
    assert "key" in result


def test_fuzzy_json_concurrent_calls_are_independent():
    """fuzzy_json must produce correct results when called from multiple threads
    concurrently (regex patterns and state must not bleed across threads)."""
    import threading

    inputs = [
        ("{'a': 1}", {"a": 1}),
        ('{"b": 2}', {"b": 2}),
        ("{c: 3}", {"c": 3}),
    ]
    results = [None] * len(inputs)
    errors = []

    def call(i, s, expected):
        try:
            r = fuzzy_json(s)
            results[i] = r
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=call, args=(i, s, e)) for i, (s, e) in enumerate(inputs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Concurrent calls raised: {errors}"
    for i, (_, expected) in enumerate(inputs):
        assert results[i] == expected


def test_fix_json_string_bracket_inside_string_value():
    """fix_json_string should not treat brackets inside string values as structural."""
    # The value '}' is inside a double-quoted string — it must not confuse the
    # bracket matcher into thinking the outer dict is already closed.
    json_str = '{"key": "}"'
    result = fix_json_string(json_str)
    # Should add the missing closing brace (the one in the string is not structural)
    assert result.endswith("}")
