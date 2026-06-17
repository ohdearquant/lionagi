"""Tests for readable JSON conversion utilities."""

import json

import pytest

from lionagi.libs.schema.as_readable import as_readable


def test_as_readable_list():
    data = [{"a": 1}, {"b": 2}]
    result = as_readable(data)

    # Each item should be on a new line with blank line between
    items = result.strip().split("\n\n")
    assert len(items) == len(data)

    # Each item should be valid JSON matching original
    for item_str, expected in zip(items, data, strict=False):
        parsed = json.loads(item_str)
        assert parsed == expected


def test_as_readable_empty():
    # Empty dict returns empty object
    assert as_readable({}) == "{}"
    # Empty list returns empty string
    assert as_readable([]) == ""
    # None returns empty object
    assert as_readable(None) == "{}"


if __name__ == "__main__":
    pytest.main([__file__])
