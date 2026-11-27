# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""A profile whose first line starts with --- but is not a bare ---.

The frontmatter readers take no settings from such a file, including a block after the
next bare ---. Loading it warns that its settings are ignored, or refuses when an
ignored setting would widen or contain what the worker may do.
"""

from __future__ import annotations

import logging

import pytest

from lionagi._errors import ConfigurationError
from lionhub.orchestration.spawn import _parse_profile
from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging


@pytest.fixture(
    params=["--- # agent", "---settings", "---\u200b"], ids=["comment", "suffix", "zwsp"]
)
def opener(request):
    return request.param


@pytest.fixture(
    params=[
        pytest.param("{opener}\n{settings}---\n\nWork.\n", id="block-under-opener"),
        pytest.param("{opener}\nIntro.\n---\n{settings}---\n\nWork.\n", id="block-after-opener"),
        pytest.param(
            "{opener}\n{settings}---\n\nWork.\n---\n\nMore work.\n",
            id="block-under-opener-then-rule",
        ),
    ]
)
def layout(request):
    return request.param


def _profile_text(layout: str, opener: str, settings: str) -> str:
    return layout.format(opener=opener, settings=settings)


def test_settings_after_a_malformed_first_line_are_ignored_with_a_warning(caplog, layout, opener):
    text = _profile_text(layout, opener, "model: claude_code/sonnet\neffort: high\n")
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        profile = _parse_profile("role", text)

    assert profile.model is None
    assert profile.effort is None
    assert "frontmatter opening line must be a bare ---" in caplog.text
    assert "every setting in it is ignored" in caplog.text
    # Nothing is dropped: the unread settings stay in the prompt text.
    assert "model: claude_code/sonnet" in profile.raw_body


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        pytest.param("tools: [Read]\n", "tool settings could not be read", id="tools"),
        pytest.param("allowed_tools: [Read]\n", "tool settings could not be read", id="allowed"),
        pytest.param("deny_read: [secrets]\n", "deny_read could not be read", id="deny-read"),
        pytest.param("mcp: none\n", "opens with a line that is not a bare ---", id="mcp"),
    ],
)
def test_an_ignored_tool_or_mcp_setting_after_a_malformed_first_line_refuses(
    layout, opener, settings, message
):
    with pytest.raises(ConfigurationError, match=message):
        _parse_profile("role", _profile_text(layout, opener, settings))


@pytest.mark.parametrize("value", ["true", "false"])
def test_a_containment_flag_after_a_malformed_first_line_still_contains(
    caplog, layout, opener, value
):
    text = _profile_text(layout, opener, f"contain_user_settings: {value}\n")
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        profile = _parse_profile("role", text)

    assert profile.contain_user_settings is True
    assert "its worker is contained" in caplog.text


def test_a_light_workload_after_a_malformed_first_line_is_kept(caplog, layout, opener):
    text = _profile_text(layout, opener, "model: claude_code/sonnet\nworkload: light\n")
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        profile = _parse_profile("role", text)

    assert profile.model is None
    assert profile.workload == "light"
    assert "workload 'light' is kept and other settings are ignored" in caplog.text


def test_a_bare_opener_still_reads_its_settings(caplog):
    text = "---\nmodel: claude_code/sonnet\n---\n\nWork.\n"
    with caplog.at_level(logging.WARNING, logger="lionagi.cli.warn"):
        profile = _parse_profile("role", text)

    assert profile.model == "claude_code/sonnet"
    assert "ignored" not in caplog.text
