# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Profile timeout parsing."

from __future__ import annotations

import logging

import pytest

from lionhub.orchestration.spawn import _parse_profile
from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging


def test_parse_profile_timeout_valid():
    text = "---\ntimeout: 1800\n---\nbody"
    profile = _parse_profile("reviewer", text)
    assert profile.timeout == 1800


def test_parse_profile_timeout_invalid_non_numeric_is_ignored(caplog):
    text = "---\ntimeout: not-a-number\n---\nbody"
    with caplog.at_level(logging.WARNING):
        profile = _parse_profile("reviewer", text)
    assert profile.timeout is None


def test_parse_profile_timeout_non_positive_is_ignored(caplog):
    text = "---\ntimeout: -5\n---\nbody"
    with caplog.at_level(logging.WARNING):
        profile = _parse_profile("reviewer", text)
    assert profile.timeout is None


def test_parse_profile_timeout_absent_is_none():
    profile = _parse_profile("reviewer", "---\nmodel: claude\n---\nbody")
    assert profile.timeout is None


@pytest.mark.parametrize("raw_yaml", ["true", "false", "1.9", "3.0"])
def test_parse_profile_timeout_rejects_bool_and_float(raw_yaml, caplog):
    """YAML booleans and floats must warn-and-ignore, not coerce (bool is an
    int subclass in Python; int(1.9) would silently truncate to 1)."""
    text = f"---\ntimeout: {raw_yaml}\n---\nbody"
    with caplog.at_level(logging.WARNING):
        profile = _parse_profile("reviewer", text)
    assert profile.timeout is None


def test_parse_profile_idle_timeout_valid():
    profile = _parse_profile("reviewer", "---\nidle_timeout: 900\n---\nbody")
    assert profile.idle_timeout == 900
    assert "idle_timeout" not in profile.extra


@pytest.mark.parametrize("raw_yaml", ["not-a-number", "-5", "0", "true", "1.5"])
def test_parse_profile_idle_timeout_garbage_is_ignored(raw_yaml, caplog):
    with caplog.at_level(logging.WARNING):
        profile = _parse_profile("reviewer", f"---\nidle_timeout: {raw_yaml}\n---\nbody")
    assert profile.idle_timeout is None
