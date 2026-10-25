"""Tests for lionagi/ln/fuzzy/_extract_json.py"""

import json

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


def test_a_fence_inside_a_json_string_does_not_end_the_block():
    """A JSON string holds its newlines as escapes, so a fence it carries never starts a line."""
    payload = {"content": "# notes\n```python\nprint(1)\n```\n", "n": 1}
    for body in (json.dumps(payload), json.dumps(payload, indent=2)):
        text = "Plan:\n```json\n" + body + "\n```\nDone."
        assert extract_json(text) == payload
        assert extract_json(text, fuzzy_parse=True) == payload


def test_a_closing_fence_may_be_indented_longer_or_followed_by_spaces():
    assert extract_json('````json\n{"a": 1}\n```` \n') == {"a": 1}
    assert extract_json('  ```json\n{"a": 1}\n  ```') == {"a": 1}
    assert extract_json('```json\r\n{"a": 1}\r\n```\r\n') == {"a": 1}


def test_an_indented_block_beside_a_top_level_block_is_read_with_it():
    # A block in a list item is indented four spaces or more; each block is read.
    text = '1. first\n\n    ```json\n    {"a": 1}\n    ```\n\nthen\n```json\n{"b": 2}\n```'
    assert extract_json(text, return_one_if_single=False) == [{"a": 1}, {"b": 2}]
    assert extract_json('\t```json\n{"a": 1}\n\t```') == {"a": 1}


def test_a_block_on_one_line_beside_a_fenced_block_is_read_in_its_place():
    text = 'first\n```json\n{"a": 1}\n```\nthen ```json {"b": 2}``` and\n```json\n{"c": 3}\n```'
    assert extract_json(text, return_one_if_single=False) == [{"a": 1}, {"b": 2}, {"c": 3}]
    assert extract_json('```json\n{"a": 1}\n```\nand ```json {"b": 2}```') == [{"a": 1}, {"b": 2}]


def test_a_span_beside_a_fenced_block_that_does_not_parse_leaves_the_single_result():
    text = 'first\n```json\n{"a": 1}\n```\nthen ```json {not json} ``` and ```json```'
    for fuzzy in (False, True):
        assert extract_json(text, fuzzy_parse=fuzzy) == {"a": 1}
    assert extract_json(text, return_one_if_single=False) == [{"a": 1}]


def test_a_fenced_block_that_does_not_parse_still_counts_toward_the_result():
    # The blocks on lines of their own count as found whether or not they parse, as do all
    # spans when the text has no such block.
    assert extract_json('```json\n{not json}\n```\nthen ```json {"b": 2} ```') == [{"b": 2}]
    assert extract_json('```json\n{"a": 1}\n```\n\n```json\n{not json}\n```') == [{"a": 1}]
    assert extract_json('```json {not json} ``` and ```json {"b": 2} ```') == [{"b": 2}]


def test_a_reply_with_a_stray_json_span_beside_its_block_validates():
    from pydantic import BaseModel

    from lionagi.ln.fuzzy._fuzzy_validate import fuzzy_validate_pydantic

    class Count(BaseModel):
        n: int

    text = 'Result:\n```json\n{"n": 3}\n```\nwrapped in ```json``` as asked'
    assert fuzzy_validate_pydantic(text, Count) == Count(n=3)


def test_a_block_whose_fences_share_its_lines_is_still_read():
    assert extract_json('```json {"a": 1} ```') == {"a": 1}
    assert extract_json('see ```json\n{"a": 1}```') == {"a": 1}


def test_each_block_of_several_is_read_with_its_own_inner_fences():
    first = {"content": "```sh\nls\n```"}
    second = {"k": "v"}
    text = "```json\n" + json.dumps(first) + "\n```\nthen\n```json\n" + json.dumps(second) + "\n```"
    assert extract_json(text, return_one_if_single=False) == [first, second]
