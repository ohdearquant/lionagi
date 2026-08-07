# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import pytest
import yaml

from lionagi.libs.frontmatter import parse_frontmatter, parse_frontmatter_strict

VALID = "---\nname: x\ndescription: y\n---\nBody.\n"
BROKEN = "---\nname: [unterminated\n---\nBody.\n"
NON_MAPPING = "---\n- one\n- two\n---\nBody.\n"
NO_FRONTMATTER = "Just a body, no frontmatter block.\n"
NO_CLOSING_DELIMITER = "---\nname: valid\nBody without closing delimiter"
NULL_FRONTMATTER = "---\nnull\n---\n"


def test_parse_frontmatter_swallows_broken_yaml():
    fm, body = parse_frontmatter(BROKEN)
    assert fm == {}
    assert body == "Body."


def test_parse_frontmatter_strict_raises_on_broken_yaml():
    with pytest.raises(yaml.YAMLError):
        parse_frontmatter_strict(BROKEN)


def test_parse_frontmatter_strict_raises_on_non_mapping():
    with pytest.raises(yaml.YAMLError):
        parse_frontmatter_strict(NON_MAPPING)


def test_parse_frontmatter_strict_matches_tolerant_reader_for_valid_input():
    fm, body = parse_frontmatter_strict(VALID)
    assert fm == {"name": "x", "description": "y"}
    assert body == "Body."


def test_parse_frontmatter_strict_returns_empty_for_no_frontmatter_block():
    fm, body = parse_frontmatter_strict(NO_FRONTMATTER)
    assert fm == {}
    assert body == NO_FRONTMATTER.strip()


def test_parse_frontmatter_tolerates_no_closing_delimiter():
    """The loose reader's contract is unchanged: an opener with no matching
    closer is treated the same as no frontmatter block at all."""
    fm, body = parse_frontmatter(NO_CLOSING_DELIMITER)
    assert fm == {}
    assert body == NO_CLOSING_DELIMITER.strip()


def test_parse_frontmatter_strict_raises_on_missing_closing_delimiter():
    """An opening ``---`` with no closing ``---`` is malformed, not merely
    absent -- the strict reader must reject it instead of silently treating
    the whole input as body text with empty metadata."""
    with pytest.raises(yaml.YAMLError):
        parse_frontmatter_strict(NO_CLOSING_DELIMITER)


def test_parse_frontmatter_tolerates_null_frontmatter():
    """The loose reader's contract is unchanged: an explicit YAML ``null``
    document still coerces to an empty metadata dict."""
    fm, body = parse_frontmatter(NULL_FRONTMATTER)
    assert fm == {}


def test_parse_frontmatter_strict_raises_on_null_frontmatter():
    """An explicit YAML ``null`` document is not a mapping -- the strict
    reader must reject it instead of coercing it to an empty metadata dict
    that then reads as successfully-parsed, empty frontmatter."""
    with pytest.raises(yaml.YAMLError):
        parse_frontmatter_strict(NULL_FRONTMATTER)
