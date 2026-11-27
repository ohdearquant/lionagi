# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from datetime import date

import pytest
import yaml

from lionhub.orchestration.frontmatter import (
    _FM_OPENER,
    _FM_SPLIT,
    _frontmatter_parts,
    parse_frontmatter,
    parse_frontmatter_strict,
)

VALID = "---\nname: x\ndescription: y\n---\nBody.\n"
BROKEN = "---\nname: [unterminated\n---\nBody.\n"
INVALID_DATE = "---\nDeadline: 2026-02-30\n---\nBody.\n"
NON_MAPPING = "---\n- one\n- two\n---\nBody.\n"
NO_FRONTMATTER = "Just a body, no frontmatter block.\n"
NO_CLOSING_DELIMITER = "---\nname: valid\nBody without closing delimiter"
NULL_FRONTMATTER = "---\nnull\n---\n"


@pytest.fixture(params=["", "\ufeff"], ids=["bare", "bom"])
def prefix(request):
    return request.param


@pytest.mark.parametrize("document", [BROKEN, INVALID_DATE], ids=["syntax", "invalid-date"])
def test_parse_frontmatter_swallows_broken_yaml(prefix, document):
    fm, body = parse_frontmatter(prefix + document)
    assert fm == {}
    assert body == "Body."


def test_parse_frontmatter_strict_raises_on_broken_yaml(prefix):
    with pytest.raises(yaml.YAMLError):
        parse_frontmatter_strict(prefix + BROKEN)


def test_parse_frontmatter_strict_chains_invalid_date_error(prefix):
    with pytest.raises(yaml.YAMLError) as caught:
        parse_frontmatter_strict(prefix + INVALID_DATE)

    assert isinstance(caught.value.__cause__, ValueError)
    assert str(caught.value.__cause__) in str(caught.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2026-02-28", date(2026, 2, 28)), ('"2026-02-30"', "2026-02-30")],
    ids=["valid-date", "quoted-date"],
)
def test_frontmatter_readers_preserve_valid_date_values(prefix, value, expected):
    document = f"{prefix}---\nDeadline: {value}\n---\nBody.\n"
    assert parse_frontmatter(document) == ({"Deadline": expected}, "Body.")
    assert parse_frontmatter_strict(document) == ({"Deadline": expected}, "Body.")


def test_parse_frontmatter_strict_raises_on_non_mapping(prefix):
    with pytest.raises(yaml.YAMLError):
        parse_frontmatter_strict(prefix + NON_MAPPING)


def test_parse_frontmatter_strict_matches_tolerant_reader_for_valid_input(prefix):
    expected = ({"name": "x", "description": "y"}, "Body.")
    assert parse_frontmatter(prefix + VALID) == expected
    assert parse_frontmatter_strict(prefix + VALID) == expected


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_frontmatter_readers_keep_leading_whitespace_semantics(prefix, newline):
    text = prefix + (" \t\n" + VALID).replace("\n", newline)
    assert parse_frontmatter(text) == parse_frontmatter(VALID)
    assert parse_frontmatter_strict(text) == parse_frontmatter_strict(VALID)


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(NO_FRONTMATTER, id="bare"),
        pytest.param(" \t" + NO_FRONTMATTER + " \t", id="outer-whitespace"),
        pytest.param("\ufeff " + NO_FRONTMATTER + "---\nLater text.\n", id="bom-plain"),
        pytest.param(" \t\n\ufeff" + VALID, id="nonleading-bom"),
        pytest.param("\ufeff\ufeff" + VALID, id="double-bom"),
    ],
)
def test_frontmatter_readers_preserve_plain_body(document):
    # A leading BOM is dropped from the returned body whether or not a block follows it.
    expected = ({}, document.removeprefix("\ufeff").strip())
    assert parse_frontmatter(document) == expected
    assert parse_frontmatter_strict(document) == expected


@pytest.fixture(
    params=[
        pytest.param("---x\nname: example\n---\nBody.\n", id="suffixed-opener"),
        pytest.param("--- heading\n---\nBody.\n", id="decorative-opener"),
        pytest.param("--- # agent\nname: example\n---\nBody.\n", id="commented-opener"),
        pytest.param("---\u200b\nname: example\n---\nBody.\n", id="zero-width-opener"),
        pytest.param(
            "--- # agent\nintro\n---\nname: example\n---\nBody.\n", id="block-after-opener"
        ),
        pytest.param("--- heading\nSome\n---\nBody\n---\nMore.\n", id="two-bare-delimiters"),
    ]
)
def malformed_opener(request):
    return request.param


def test_tolerant_reader_keeps_malformed_opener_body(prefix, malformed_opener):
    """A first line that starts with --- but is not a bare --- opens no block, so no later
    block is read as settings and the whole text comes back as the body."""
    assert parse_frontmatter(prefix + malformed_opener) == ({}, malformed_opener.strip())


def test_strict_reader_rejects_malformed_opener(prefix, malformed_opener):
    with pytest.raises(yaml.YAMLError, match="opening line must be a bare ---"):
        parse_frontmatter_strict(prefix + malformed_opener)


def test_parse_frontmatter_tolerates_no_closing_delimiter(prefix):
    """The loose reader's contract is unchanged: an opener with no matching
    closer is treated the same as no frontmatter block at all."""
    fm, body = parse_frontmatter(prefix + NO_CLOSING_DELIMITER)
    assert fm == {}
    assert body == NO_CLOSING_DELIMITER.strip()


def test_parse_frontmatter_strict_raises_on_missing_closing_delimiter(prefix):
    """An opening ``---`` with no closing ``---`` is malformed, not merely
    absent -- the strict reader must reject it instead of silently treating
    the whole input as body text with empty metadata."""
    with pytest.raises(yaml.YAMLError):
        parse_frontmatter_strict(prefix + NO_CLOSING_DELIMITER)


def test_parse_frontmatter_tolerates_null_frontmatter(prefix):
    """The loose reader's contract is unchanged: an explicit YAML ``null``
    document still coerces to an empty metadata dict."""
    fm, body = parse_frontmatter(prefix + NULL_FRONTMATTER)
    assert fm == {}
    assert body == ""


def test_parse_frontmatter_strict_raises_on_null_frontmatter(prefix):
    """An explicit YAML ``null`` document is not a mapping -- the strict
    reader must reject it instead of coercing it to an empty metadata dict
    that then reads as successfully-parsed, empty frontmatter."""
    with pytest.raises(yaml.YAMLError):
        parse_frontmatter_strict(prefix + NULL_FRONTMATTER)


def test_the_delimiter_pattern_is_the_opener_alone_on_its_line():
    assert _FM_SPLIT.fullmatch(_FM_OPENER)
    assert _FM_SPLIT.fullmatch(_FM_OPENER + "  ")
    assert _FM_SPLIT.fullmatch(_FM_OPENER + " # agent") is None
    assert _FM_SPLIT.fullmatch(_FM_OPENER + "-") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("---\nname: x\n---\nBody.", ["", "\nname: x\n", "\nBody."]),
        ("---\nname: x", ["", "\nname: x"]),
        ("--- # agent\nname: x\n---\nBody.", None),
        ("Body.\n---\nname: x\n---", None),
    ],
    ids=["closed", "unclosed", "not-a-bare-opener", "no-opener"],
)
def test_frontmatter_parts_splits_only_after_a_bare_opener(text, expected):
    assert _frontmatter_parts(text) == expected
