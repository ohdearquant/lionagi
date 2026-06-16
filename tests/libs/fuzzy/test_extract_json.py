"""Tests for lionagi/ln/fuzzy/_extract_json.py"""

from lionagi.ln.fuzzy._extract_json import extract_json


def test_extract_json_direct_parse():
    """Test direct JSON parsing"""
    result = extract_json('{"key": "value"}')
    assert result == {"key": "value"}


def test_extract_json_list_input():
    """Test with list input"""
    result = extract_json(['{"key": "value"}'])
    assert result == {"key": "value"}


def test_extract_json_markdown_single():
    """Test extraction from single markdown block"""
    input_str = '```json\n{"key": "value"}\n```'
    result = extract_json(input_str)
    assert result == {"key": "value"}


def test_extract_json_markdown_multiple():
    """Test extraction from multiple markdown blocks"""
    input_str = '```json\n{"key1": "value1"}\n```\n```json\n{"key2": "value2"}\n```'
    result = extract_json(input_str, return_one_if_single=False)
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0] == {"key1": "value1"}
    assert result[1] == {"key2": "value2"}


def test_extract_json_no_json_found():
    """Test when no JSON is found"""
    result = extract_json("no json here")
    assert result == []


def test_extract_json_invalid_direct():
    """Test invalid JSON in direct parse falls back to block extraction"""
    result = extract_json("{invalid}")
    assert result == []


def test_extract_json_fuzzy_parse_direct():
    """Test fuzzy parsing for direct JSON"""
    result = extract_json("{'key': 'value'}", fuzzy_parse=True)
    assert result == {"key": "value"}


def test_extract_json_fuzzy_parse_markdown():
    """Test fuzzy parsing in markdown blocks"""
    input_str = "```json\n{'key': 'value'}\n```"
    result = extract_json(input_str, fuzzy_parse=True)
    assert result == {"key": "value"}


def test_extract_json_invalid_markdown_block():
    """Test invalid JSON in markdown block returns empty list"""
    input_str = "```json\n{invalid json}\n```"
    result = extract_json(input_str)
    assert result == []


def test_extract_json_multiple_with_invalid():
    """Test multiple blocks where some are invalid - covers lines 70-72"""
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
    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0] == {"valid": "first"}
    assert result[1] == {"valid": "second"}


def test_extract_json_multiple_with_invalid_fuzzy():
    """Test multiple blocks with invalid JSON using fuzzy parse"""
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
    assert isinstance(result, list)
    assert len(result) >= 2


def test_extract_json_all_invalid_blocks():
    """Test when all blocks are invalid"""
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
    """Test return_one_if_single=False with single valid block"""
    input_str = '```json\n{"key": "value"}\n```'
    result = extract_json(input_str, return_one_if_single=False)
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0] == {"key": "value"}


def test_extract_json_list_of_strings():
    """Test with list of strings input"""
    input_list = ["```json", '{"key": "value"}', "```"]
    result = extract_json(input_list)
    assert result == {"key": "value"}


def test_extract_json_mixed_valid_invalid_comprehensive():
    """Comprehensive test for lines 70-72 with various invalid patterns"""
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
    assert isinstance(result, list)
    assert len(result) == 3
    assert {"block1": "valid"} in result
    assert ["valid", "array"] in result
    assert {"block2": "also valid"} in result
