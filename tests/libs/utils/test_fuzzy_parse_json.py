"""Test suite for JSON utilities (fuzzy_json, extract_json) - TDD Specification Implementation."""

import pytest

from lionagi.ln.fuzzy._extract_json import extract_json
from lionagi.ln.fuzzy._fuzzy_json import fix_json_string, fuzzy_json


class TestFuzzyJsonParsing:
    """TestSuite: FuzzyJsonParsing (Msgspec Integration) - Resilience, malformed input handling."""

    def test_valid_json(self):
        """Test: ValidJson

        GIVEN input='{"a": 1}' THEN returns {"a": 1}.
        """
        assert fuzzy_json('{"a": 1}') == {"a": 1}, "Valid JSON must parse correctly"
        assert fuzzy_json("[1, 2, 3]") == [
            1,
            2,
            3,
        ], "Valid JSON array must parse correctly"
        assert fuzzy_json('{"nested": {"key": "value"}}') == {"nested": {"key": "value"}}, (
            "Nested JSON must parse"
        )

    def test_normalization(self):
        """Test: Normalization (Single Quote, Whitespace, Unquoted Keys)

        GIVEN input=" {'a': 1, b: '2'} " THEN returns {"a": 1, "b": "2"}.
        """
        # Single quotes
        assert fuzzy_json("{'a': 1}") == {"a": 1}, (
            "Single quotes must be normalized to double quotes"
        )

        # Extra whitespace
        assert fuzzy_json('  {  "a"  :  1  }  ') == {"a": 1}, "Extra whitespace must be handled"

        # Unquoted keys
        assert fuzzy_json("{a: 1, b: 2}") == {
            "a": 1,
            "b": 2,
        }, "Unquoted keys must be quoted"

        # Combined
        assert fuzzy_json(" {'a': 1, b: '2'} ") == {
            "a": 1,
            "b": "2",
        }, "Combined normalization must work"

        # Complex case with nested objects
        assert fuzzy_json("{outer: {'inner': 1}}") == {"outer": {"inner": 1}}, (
            "Nested normalization must work"
        )

    def test_unmatched_brackets_fixing(self):
        """Test: UnmatchedBracketsFixing (CRITICAL)

        GIVEN input='[{"a": 1}, {"b": 2}' (Missing }] )
        WHEN fuzzy_json(input) is called
        THEN returns [{"a": 1}, {"b": 2}].
        """
        # Missing closing bracket
        assert fuzzy_json('[{"a": 1}, {"b": 2}') == [
            {"a": 1},
            {"b": 2},
        ], "Missing closing brackets must be fixed"

        # Missing closing brace
        assert fuzzy_json('{"a": 1') == {"a": 1}, "Missing closing brace must be fixed"

        # Multiple missing brackets
        assert fuzzy_json('[{"a": [1, 2') == [{"a": [1, 2]}], (
            "Multiple missing brackets must be fixed"
        )

        # Nested missing brackets
        assert fuzzy_json('{"outer": {"inner": "value"') == {"outer": {"inner": "value"}}, (
            "Nested missing brackets must be fixed"
        )

    def test_irreparable_input(self):
        """Test: IrreparableInput

        GIVEN input="Not JSON" or ""
        WHEN fuzzy_json(input) is called
        THEN raise ValueError.
        """
        with pytest.raises(ValueError, match="Invalid JSON string"):
            fuzzy_json("Not JSON at all")

        with pytest.raises(ValueError, match="Input string is empty"):
            fuzzy_json("")

        with pytest.raises(ValueError, match="Input string is empty"):
            fuzzy_json("   ")  # Only whitespace

        with pytest.raises(TypeError, match="Input must be a string"):
            fuzzy_json(123)  # Not a string

        with pytest.raises(TypeError, match="Input must be a string"):
            fuzzy_json(None)

    def test_complex_malformed_json(self):
        """Test handling of complex malformed JSON scenarios."""
        # Mixed quotes
        assert fuzzy_json("{\"a\": 'value'}") == {"a": "value"}, "Mixed quotes must be handled"

        # Trailing commas (valid in some parsers)
        assert fuzzy_json('{"a": 1,}') == {"a": 1}, "Trailing commas should be handled"
        assert fuzzy_json("[1, 2, 3,]") == [
            1,
            2,
            3,
        ], "Trailing commas in arrays should be handled"

    def test_escaped_characters(self):
        """Test handling of escaped characters."""
        # Escaped quotes
        assert fuzzy_json('{"key": "\\"value\\""}') == {"key": '"value"'}, (
            "Escaped quotes must be preserved"
        )

        # Escaped backslashes
        assert fuzzy_json('{"path": "C:\\\\Users\\\\file"}') == {"path": "C:\\Users\\file"}, (
            "Escaped backslashes must be handled"
        )

    def test_msgspec_decoder_efficiency(self):
        """Test that msgspec decoder is used efficiently."""
        # Large JSON should still parse efficiently
        large_json = '{"items": [' + ",".join(f'{{"id": {i}}}' for i in range(100)) + "]}"
        result = fuzzy_json(large_json)
        assert len(result["items"]) == 100, "Large JSON must parse correctly"
        assert result["items"][0] == {"id": 0}
        assert result["items"][99] == {"id": 99}


class TestExtractJson:
    """TestSuite: ExtractJson - Markdown extraction, multiple blocks, fuzzy integration."""

    def test_direct_json_parsing(self):
        """Test direct JSON parsing without markdown blocks."""
        assert extract_json('{"a": 1}') == {"a": 1}, "Direct JSON must parse"
        assert extract_json("[1, 2, 3]") == [
            1,
            2,
            3,
        ], "Direct JSON array must parse"

    def test_markdown_code_block_extraction(self):
        """Test: MarkdownCodeBlockExtraction

        GIVEN input='Text\n```json\n{"a": 1}\n```'
        WHEN extract_json(input) is called
        THEN returns {"a": 1}.
        """
        input_text = """Some text before
```json
{"a": 1}
```
Some text after"""

        assert extract_json(input_text) == {"a": 1}, "JSON from markdown block must be extracted"

        # Test with more whitespace
        input_with_space = """
        Text before
        
        ```json
        {"key": "value"}
        ```
        
        Text after
        """
        assert extract_json(input_with_space) == {"key": "value"}, (
            "JSON with whitespace must be extracted"
        )

    def test_multiple_code_blocks(self):
        """Test: MultipleCodeBlocks

        GIVEN input='```json\n{"a": 1}\n```\n```json\n{"b": 2}\n```'
        WHEN extract_json(input) is called
        THEN returns [{"a": 1}, {"b": 2}].
        """
        input_text = """First block:
```json
{"a": 1}
```
Second block:
```json
{"b": 2}
```"""

        # With return_one_if_single=False, should return list
        result = extract_json(input_text, return_one_if_single=False)
        assert result == [
            {"a": 1},
            {"b": 2},
        ], "Multiple blocks must return list"

        # Test with three blocks
        three_blocks = """
```json
{"first": 1}
```
```json
{"second": 2}
```
```json
{"third": 3}
```"""
        result = extract_json(three_blocks, return_one_if_single=False)
        assert result == [
            {"first": 1},
            {"second": 2},
            {"third": 3},
        ], "Three blocks must all be extracted"

    def test_fuzzy_integration(self):
        """Test: FuzzyIntegration

        GIVEN input="```json\n{'a': 1}\n```"
        WHEN extract_json(input, fuzzy_parse=True) is called
        THEN returns {"a": 1}.
        """
        # Single quotes in markdown block
        input_text = """```json
{'a': 1, 'b': 2}
```"""
        assert extract_json(input_text, fuzzy_parse=True) == {
            "a": 1,
            "b": 2,
        }, "Fuzzy parse must work with markdown"

        # Unquoted keys in markdown block
        input_unquoted = """```json
{a: 1, b: 2}
```"""
        assert extract_json(input_unquoted, fuzzy_parse=True) == {
            "a": 1,
            "b": 2,
        }, "Unquoted keys must work with fuzzy"

        # Missing bracket in markdown
        input_broken = """```json
{"items": [1, 2, 3
```"""
        assert extract_json(input_broken, fuzzy_parse=True) == {"items": [1, 2, 3]}, (
            "Broken JSON in markdown must be fixed"
        )

    def test_return_one_if_single_parameter(self):
        """Test the return_one_if_single parameter behavior."""
        single_block = """```json
{"single": "value"}
```"""

        # Default behavior (return_one_if_single=True)
        assert extract_json(single_block) == {"single": "value"}, (
            "Single block returns dict by default"
        )
        assert extract_json(single_block, return_one_if_single=True) == {"single": "value"}, (
            "Explicit True returns dict"
        )

        # Force list return
        assert extract_json(single_block, return_one_if_single=False) == [{"single": "value"}], (
            "False returns list"
        )

    def test_list_input(self):
        """Test that list of strings is properly joined and processed."""
        lines = [
            "Some text",
            "```json",
            '{"key": "value"}',
            "```",
            "More text",
        ]

        assert extract_json(lines) == {"key": "value"}, "List input must be joined and processed"

        # Multiple blocks in list form
        lines_multi = [
            "```json",
            '{"a": 1}',
            "```",
            "Middle",
            "```json",
            '{"b": 2}',
            "```",
        ]

        result = extract_json(lines_multi, return_one_if_single=False)
        assert result == [
            {"a": 1},
            {"b": 2},
        ], "Multiple blocks from list input"

    def test_no_json_found(self):
        """Test behavior when no JSON is found."""
        assert extract_json("No JSON here") == [], "No JSON should return empty list"
        assert extract_json("```python\nprint('hello')\n```") == [], (
            "Non-JSON code block returns empty"
        )
        assert extract_json("") == [], "Empty string returns empty list"

        # Invalid JSON that can't be parsed even with fuzzy
        assert extract_json("```json\ncompletely invalid\n```", fuzzy_parse=False) == [], (
            "Invalid JSON returns empty"
        )

    def test_nested_json_in_markdown(self):
        """Test extraction of complex nested JSON from markdown."""
        complex_md = """Here's a complex response:
        
```json
{
    "users": [
        {"id": 1, "name": "Alice"},
        {"id": 2, "name": "Bob"}
    ],
    "metadata": {
        "total": 2,
        "page": 1
    }
}
```

And another one:

```json
[
    {"type": "error", "message": "Not found"},
    {"type": "success", "data": [1, 2, 3]}
]
```"""

        results = extract_json(complex_md, return_one_if_single=False)
        assert len(results) == 2, "Two complex blocks must be extracted"
        assert results[0]["users"][0]["name"] == "Alice"
        assert results[1][1]["type"] == "success"


class TestFixJsonString:
    """Test the fix_json_string utility function directly."""

    def test_fix_missing_closing_brackets(self):
        """Test fixing various missing closing brackets."""
        assert fix_json_string('{"a": 1') == '{"a": 1}'
        assert fix_json_string("[1, 2, 3") == "[1, 2, 3]"
        assert fix_json_string('{"a": [1, 2') == '{"a": [1, 2]}'
        assert fix_json_string('[{"a": 1') == '[{"a": 1}]'

    def test_nested_missing_brackets(self):
        """Test fixing deeply nested missing brackets."""
        assert fix_json_string('{"a": {"b": {"c": 1') == '{"a": {"b": {"c": 1}}}'
        assert fix_json_string("[[[1, 2") == "[[[1, 2]]]"

    def test_mixed_bracket_types(self):
        """Test fixing mixed bracket types."""
        assert (
            fix_json_string('{"array": [1, 2, {"nested": 3') == '{"array": [1, 2, {"nested": 3}]}'
        )

    def test_error_conditions(self):
        """Test error conditions for fix_json_string."""
        with pytest.raises(ValueError, match="Extra closing bracket"):
            fix_json_string('{"a": 1}}')

        with pytest.raises(ValueError, match="Mismatched brackets"):
            fix_json_string('{"a": 1]')

        with pytest.raises(ValueError, match="Input string is empty"):
            fix_json_string("")

    def test_string_preservation(self):
        """Test that strings containing brackets are preserved."""
        # Brackets inside strings should not affect bracket counting
        result = fix_json_string('{"text": "Use [brackets] and {braces} in text"}')
        assert result == '{"text": "Use [brackets] and {braces} in text"}'

        # Escaped quotes in strings
        result = fix_json_string('{"text": "Say \\"hello\\""}')
        assert result == '{"text": "Say \\"hello\\""}'
