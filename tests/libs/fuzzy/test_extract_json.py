"""Tests for lionagi/ln/fuzzy/_extract_json.py

Target: Cover lines 70-72 (exception handling for invalid JSON in multiple blocks)
"""

from lionagi.ln.fuzzy._extract_json import extract_json

# ============================================================================
# Test extract_json basic functionality
# ============================================================================


def test_extract_json_direct_parse():
    result = extract_json('{"key": "value"}')
    assert result == {"key": "value"}


def test_extract_json_list_input():
    result = extract_json(['{"key": "value"}'])
    assert result == {"key": "value"}


def test_extract_json_markdown_single():
    input_str = '```json\n{"key": "value"}\n```'
    result = extract_json(input_str)
    assert result == {"key": "value"}


def test_extract_json_markdown_multiple():
    input_str = '```json\n{"key1": "value1"}\n```\n```json\n{"key2": "value2"}\n```'
    result = extract_json(input_str, return_one_if_single=False)
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0] == {"key1": "value1"}
    assert result[1] == {"key2": "value2"}


def test_extract_json_no_json_found():
    result = extract_json("no json here")
    assert result == []


def test_extract_json_invalid_direct():
    result = extract_json("{invalid}")
    assert result == []


def test_extract_json_fuzzy_parse_direct():
    result = extract_json("{'key': 'value'}", fuzzy_parse=True)
    assert result == {"key": "value"}


def test_extract_json_fuzzy_parse_markdown():
    input_str = "```json\n{'key': 'value'}\n```"
    result = extract_json(input_str, fuzzy_parse=True)
    assert result == {"key": "value"}


def test_extract_json_invalid_markdown_block():
    input_str = "```json\n{invalid json}\n```"
    result = extract_json(input_str)
    assert result == []


def test_extract_json_multiple_with_invalid():
    # This will test the exception handling in the loop (lines 70-72)
    input_str = """
```json
{"valid": "first"}
```
```json
{invalid json here}
```
```json
{"valid": "second"}
```
"""
    result = extract_json(input_str, return_one_if_single=False)
    # Should skip the invalid block and return only valid ones
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0] == {"valid": "first"}
    assert result[1] == {"valid": "second"}


def test_extract_json_multiple_with_invalid_fuzzy():
    input_str = """
```json
{'valid': 'first'}
```
```json
{completely broken
```
```json
{'valid': 'second'}
```
"""
    result = extract_json(input_str, fuzzy_parse=True, return_one_if_single=False)
    # Fuzzy parse might handle some, but completely broken should be skipped
    assert isinstance(result, list)
    # Should have at least the valid ones
    assert len(result) >= 2


def test_extract_json_all_invalid_blocks():
    input_str = """
```json
{invalid1}
```
```json
{invalid2}
```
```json
{invalid3}
```
"""
    result = extract_json(input_str, return_one_if_single=False)
    assert result == []


def test_extract_json_return_one_if_single_false():
    input_str = '```json\n{"key": "value"}\n```'
    result = extract_json(input_str, return_one_if_single=False)
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0] == {"key": "value"}


def test_extract_json_list_of_strings():
    input_list = ["```json", '{"key": "value"}', "```"]
    result = extract_json(input_list)
    assert result == {"key": "value"}


def test_extract_json_mixed_valid_invalid_comprehensive():
    input_str = """
```json
{"block1": "valid"}
```

Some text here

```json
{this is: not valid, json: at all}
```

More text

```json
["valid", "array"]
```

```json
{
  "incomplete":
}
```

```json
{"block2": "also valid"}
```
"""
    result = extract_json(input_str, return_one_if_single=False)
    # Should extract only the valid blocks
    assert isinstance(result, list)
    # We expect at least 3 valid blocks: block1, array, block2
    assert len(result) == 3
    assert {"block1": "valid"} in result
    assert ["valid", "array"] in result
    assert {"block2": "also valid"} in result
