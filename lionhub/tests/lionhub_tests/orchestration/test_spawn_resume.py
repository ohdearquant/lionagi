# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Profile resume policy parsing."

from __future__ import annotations

import logging

import pytest

from lionhub.orchestration.spawn import _parse_profile
from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging


def test_parse_profile_resume_on_timeout_once():
    text = "---\nresume_on_timeout: once\n---\nbody"
    profile = _parse_profile("reviewer", text)
    assert profile.resume_on_timeout is True


def test_parse_profile_resume_on_timeout_absent_is_false():
    profile = _parse_profile("reviewer", "---\nmodel: claude\n---\nbody")
    assert profile.resume_on_timeout is False


def test_parse_profile_resume_on_timeout_unrecognized_is_ignored(caplog):
    text = "---\nresume_on_timeout: sometimes\n---\nbody"
    with caplog.at_level(logging.WARNING):
        profile = _parse_profile("reviewer", text)
    assert profile.resume_on_timeout is False


@pytest.mark.parametrize("raw_yaml", ["true", "false", "yes", "sometimes"])
def test_parse_profile_resume_on_timeout_rejects_non_once_values(raw_yaml, caplog):
    """Only the literal string 'once' opts in; boolean aliases and other
    strings must warn-and-ignore (parse as False), not opt in."""
    text = f"---\nresume_on_timeout: {raw_yaml}\n---\nbody"
    with caplog.at_level(logging.WARNING):
        profile = _parse_profile("reviewer", text)
    assert profile.resume_on_timeout is False
