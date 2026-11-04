# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Agent profile MCP containment declaration."""

from __future__ import annotations

import pytest

from lionagi._errors import ConfigurationError
from lionagi.cli._providers import AgentProfile, _parse_profile, profile_config


def test_profile_mcp_none_is_a_recognized_runtime_setting():
    profile = _parse_profile("reviewer", "---\nmcp: none\n---\nReview.")

    assert profile.mcp == "none"
    assert "mcp" not in profile.extra
    assert profile_config(profile)["mcp"] == "none"


def test_profile_without_mcp_keeps_legacy_defaults():
    profile = _parse_profile("reviewer", "---\nmodel: codex/gpt-5.5\n---\nReview.")

    assert profile.mcp is None
    assert AgentProfile(name="manual").mcp is None
    assert "mcp" not in profile_config(profile)


@pytest.mark.parametrize("value", ["other", "null", "true", "false", "0", "[]", "{}", "'NONE'"])
def test_profile_mcp_invalid_value_is_refused_without_echoing_it(value):
    with pytest.raises(ConfigurationError) as raised:
        _parse_profile("reviewer", f"---\nmcp: {value}\n---\nReview.")

    message = str(raised.value)
    assert "reviewer" in message
    assert "mcp" in message
    assert "must be 'none'" in message
    if value == "other":
        assert value not in message


@pytest.mark.parametrize(
    "frontmatter",
    [
        "mcp: none\nmcp: none\n",
        "mcp: invalid\nmcp: none\n",
        "\"mcp\": none\n'mcp': none\n",
        "defaults: &defaults {mcp: none}\n<<: *defaults\nmcp: none\n",
        "mcp: [unterminated\n",
        "mcp: none\nmodel: [unterminated\n",
    ],
)
def test_duplicate_or_unreadable_mcp_frontmatter_is_refused(frontmatter):
    with pytest.raises(ConfigurationError) as raised:
        _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")

    assert "reviewer" in str(raised.value)
    assert "mcp" in str(raised.value)


def test_unclosed_mcp_frontmatter_is_refused():
    with pytest.raises(ConfigurationError) as raised:
        _parse_profile("reviewer", "---\nmcp: none\n")

    assert "reviewer" in str(raised.value)
    assert "mcp" in str(raised.value)


def test_unrelated_unreadable_frontmatter_keeps_existing_leniency():
    profile = _parse_profile("reviewer", "---\nmodel: [unterminated\n---\nReview.")
    assert profile.mcp is None


def test_unrelated_duplicate_key_keeps_existing_yaml_behavior():
    profile = _parse_profile("reviewer", "---\nmodel: first\nmodel: second\n---\nReview.")
    assert profile.model == "second"
    assert profile.mcp is None


def test_single_merged_mcp_key_is_valid():
    profile = _parse_profile("reviewer", "---\ndefaults: &d {mcp: none}\n<<: *d\n---\nReview.")
    assert profile.mcp == "none"


def test_mcp_example_in_body_is_not_frontmatter():
    profile = _parse_profile("reviewer", "Review.\n\nExample:\n```yaml\nmcp: none\n```")
    assert profile.mcp is None


def test_mcp_mention_in_comment_does_not_change_unreadable_other_frontmatter():
    profile = _parse_profile("reviewer", "---\n# mcp: none\nmodel: [\n---\nReview.")
    assert profile.mcp is None


@pytest.mark.parametrize(
    "frontmatter",
    [
        "- mcp: none\n",
        "- mcp: invalid\n",
        "[{mcp: none}]\n",
        "- nested:\n    mcp: none\n",
    ],
)
def test_non_mapping_frontmatter_with_mcp_key_is_refused(frontmatter):
    with pytest.raises(ConfigurationError) as raised:
        _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")

    assert "reviewer" in str(raised.value)
    assert "mcp" in str(raised.value)


@pytest.mark.parametrize("frontmatter", ["- model: example\n", "'mcp: none'\n"])
def test_non_mapping_without_mcp_key_keeps_existing_behavior(frontmatter):
    profile = _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")
    assert profile.mcp is None


@pytest.mark.parametrize(
    "frontmatter",
    [
        '"m\\u0063p": none\nmodel: [unterminated\n',
        'model: [unterminated\n"m\\u0063p": none\n',
        '[{"m\\u0063p": none, broken: [}]\n',
    ],
)
def test_escaped_mcp_key_in_unreadable_frontmatter_is_refused(frontmatter):
    with pytest.raises(ConfigurationError) as raised:
        _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")

    assert "reviewer" in str(raised.value)
    assert "mcp" in str(raised.value)


def test_escaped_mcp_key_in_unclosed_frontmatter_is_refused():
    with pytest.raises(ConfigurationError):
        _parse_profile("reviewer", '---\n"m\\u0063p": none\n')


@pytest.mark.parametrize(
    "frontmatter",
    [
        "key: &policy_key mcp\n? *policy_key\n: none\nmodel: [\n",
        "model: [unterminated\nkey: &policy_key mcp\n? *policy_key\n: none\n",
    ],
)
def test_alias_mcp_key_in_unreadable_frontmatter_is_refused(frontmatter):
    with pytest.raises(ConfigurationError):
        _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")


def test_escaped_mcp_mention_in_comment_does_not_refuse_unrelated_frontmatter():
    profile = _parse_profile("reviewer", '---\n# "m\\u0063p": none\nmodel: [\n---\nReview.')
    assert profile.mcp is None


def test_escaped_mcp_example_in_yaml_block_scalar_is_not_a_key():
    text = '---\nprompt: |\n  "m\\u0063p": none\nmodel: [\n---\nReview.'
    assert _parse_profile("reviewer", text).mcp is None


def test_mcp_example_in_yaml_block_scalar_is_not_a_key():
    text = "---\nprompt: |\n  mcp: none\nmodel: [\n---\nReview."
    assert _parse_profile("reviewer", text).mcp is None


@pytest.mark.parametrize(
    "frontmatter",
    [
        "model: @invalid, mcp: none\n",
        'model: @invalid, "m\\u0063p": none\n',
    ],
)
def test_mcp_key_after_same_line_syntax_error_is_refused(frontmatter):
    with pytest.raises(ConfigurationError) as raised:
        _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")

    assert "reviewer" in str(raised.value)
    assert "mcp" in str(raised.value)


@pytest.mark.parametrize(
    "frontmatter",
    [
        "model: @invalid # comment, mcp: none\n",
        'model: @invalid, note: "example, mcp: none"\n',
        "model: @invalid\nprompt: |\n  mcp: none\n",
        "model: @invalid, other: value\n",
    ],
)
def test_unrelated_unreadable_same_line_text_remains_lenient(frontmatter):
    profile = _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")
    assert profile.mcp is None


@pytest.mark.parametrize(
    "frontmatter",
    [
        "model: @invalid {mcp: none}\n",
        "model: @invalid [mcp: none]\n",
        'model: @invalid {"m\\u0063p": none}\n',
        'model: @invalid ["m\\u0063p": none]\n',
    ],
)
def test_mcp_key_after_same_line_flow_opener_is_refused(frontmatter):
    with pytest.raises(ConfigurationError) as raised:
        _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")

    assert "reviewer" in str(raised.value)
    assert "mcp" in str(raised.value)


@pytest.mark.parametrize(
    "frontmatter",
    [
        'model: @invalid, note: "example {mcp: none}"\n',
        'model: @invalid, note: "example [mcp: none]"\n',
        "model: @invalid, note: 'example {mcp: none}'\n",
        'model: @invalid {other: "mcp: none"}\n',
        "model: @invalid # example {mcp: none}\n",
        "model: @invalid\nprompt: |\n  example {mcp: none}\n",
        "model: @invalid {other: value}\n",
    ],
)
def test_unrelated_unreadable_flow_text_remains_lenient(frontmatter):
    profile = _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")
    assert profile.mcp is None


@pytest.mark.parametrize(
    "frontmatter",
    [
        "model: @invalid, note: can't, mcp: none\n",
        'model: @invalid, note: can"t, mcp: none\n',
        "model: @invalid, note: can't, {mcp: none}\n",
        'model: @invalid, note: can"t, ["m\\u0063p": none]\n',
    ],
)
def test_plain_scalar_quote_does_not_hide_later_mcp_key(frontmatter):
    with pytest.raises(ConfigurationError) as raised:
        _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")

    assert "reviewer" in str(raised.value)
    assert "mcp" in str(raised.value)


@pytest.mark.parametrize(
    "frontmatter",
    [
        'model: @invalid, note: "can\'t, mcp: none"\n',
        "model: @invalid, note: 'can''t, mcp: none'\n",
        "model: @invalid, note: 'can\"t, {mcp: none}'\n",
        'model: @invalid, note: !!str "example, mcp: none"\n',
        'model: @invalid, note: &note "example, mcp: none"\n',
        'model: @invalid, {"note":"example, mcp: none"}\n',
    ],
)
def test_real_quoted_values_with_mcp_text_remain_lenient(frontmatter):
    profile = _parse_profile("reviewer", f"---\n{frontmatter}---\nReview.")
    assert profile.mcp is None
