# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Keys written with YAML escapes in profile text that is not read as settings.

The checks on such text look for a key's spelling, so they also read it with its
double-quoted scalars unescaped: ``"\\x74ools"`` is found as ``tools``.
"""

from __future__ import annotations

import pytest

from lionagi._errors import ConfigurationError
from lionhub.orchestration.frontmatter import _with_unescaped_scalars
from lionhub.orchestration.spawn import _parse_profile
from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging

_UNREADABLE = "notes: [unclosed\n"


def test_text_without_a_backslash_is_returned_unchanged():
    text = 'tools: [Read]\n"name": x\n'
    assert _with_unescaped_scalars(text) is text


@pytest.mark.parametrize(
    ("text", "found"),
    [
        (r'"\x74ools": [Read]', '"tools": [Read]'),
        (r'"\u0074ools": [Read]', '"tools": [Read]'),
        (r'- "deny\x5fread": [secrets]', '- "deny_read": [secrets]'),
        (r'{"\x6dcp": none}', '{"mcp": none}'),
    ],
    ids=["hex", "unicode", "list-item", "flow-mapping"],
)
def test_escaped_scalars_are_unescaped_after_the_original_text(text, found):
    view = _with_unescaped_scalars(text)

    assert view.startswith(text + "\n")
    assert found in view


def test_a_scalar_whose_escape_does_not_decode_is_kept_as_written():
    text = r'"\q": x'
    assert _with_unescaped_scalars(text) == text + "\n" + text


_TOOL_KEYS = [
    pytest.param(r'"\x74ools"', id="tools-hex"),
    pytest.param(r'"\u0074ools"', id="tools-unicode"),
    pytest.param(r'"allowed_\x74ools"', id="allowed-tools"),
]


def _after_prompt_text(line: str) -> str:
    return "Work.\n\n---\n" + line + "\n" + _UNREADABLE + "---\n\nMore work.\n"


@pytest.mark.parametrize("key", _TOOL_KEYS)
def test_an_escaped_tool_key_in_an_unread_block_after_prompt_text_refuses(key):
    with pytest.raises(ConfigurationError, match="tool settings could not be read"):
        _parse_profile("role", _after_prompt_text(key + ": [Read]"))


@pytest.mark.parametrize("key", _TOOL_KEYS)
def test_an_escaped_tool_key_hidden_by_text_on_the_first_line_refuses(key):
    text = "Notes ---\n" + key + ": [Read]\n" + _UNREADABLE + "---\n\nWork.\n"
    with pytest.raises(ConfigurationError, match="tool settings could not be read"):
        _parse_profile("role", text)


@pytest.mark.parametrize(
    ("line", "declared"),
    [
        pytest.param(r'"deny\x5fread": [secrets]', "deny_read", id="deny-read"),
        pytest.param(
            r'"contain\x5fuser_settings": false', "contain_user_settings", id="containment"
        ),
        pytest.param(
            r'"contain_account\x5fintegrations": false',
            "contain_account_integrations",
            id="account-containment",
        ),
    ],
)
def test_an_escaped_setting_in_an_unread_block_after_prompt_text_refuses(line, declared):
    with pytest.raises(ConfigurationError, match=rf"its {declared} declaration would be ignored"):
        _parse_profile("role", _after_prompt_text(line))


def test_an_escaped_mcp_key_in_an_unread_block_after_prompt_text_refuses():
    with pytest.raises(ConfigurationError, match="mcp declaration would be ignored"):
        _parse_profile("role", _after_prompt_text(r'"\x6dcp": none'))


def test_an_escaped_deny_read_in_a_block_after_the_frontmatter_refuses():
    text = (
        "---\nmodel: claude_code/sonnet\n---\n\nWork.\n\n---\n"
        + r'"deny\x5fread": [secrets]'
        + "\n---\n\nMore work.\n"
    )
    with pytest.raises(ConfigurationError, match="sets deny_read outside the leading frontmatter"):
        _parse_profile("role", text)


def test_unread_text_without_escapes_still_loads(caplog):
    import logging

    text = _after_prompt_text("example: value")
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        profile = _parse_profile("role", text)

    assert "More work." in profile.raw_body
