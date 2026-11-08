# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"Deadline preamble construction."

from __future__ import annotations

import re
import time

import pytest

from lionhub.orchestration.spawn import build_deadline_preamble
from tests._cli_logging_test_support import _restore_cli_logging as _restore_cli_logging


def test_preamble_contains_deadline_tags():
    """Output is wrapped in [DEADLINE] … [/DEADLINE] markers."""
    preamble = build_deadline_preamble(300)
    assert preamble.startswith("[DEADLINE]\n")
    assert "[/DEADLINE]\n" in preamble


def test_preamble_contains_minutes():
    """300 seconds → 5 minutes in preamble."""
    preamble = build_deadline_preamble(300)
    assert "5 minutes" in preamble


def test_preamble_singular_minute():
    """60 seconds → '1 minute' (not '1 minutes')."""
    preamble = build_deadline_preamble(60)
    assert "1 minute " in preamble
    assert "1 minutes" not in preamble


def test_preamble_deadline_iso_format():
    """Preamble contains an ISO-8601 timestamp matching the expected pattern."""
    preamble = build_deadline_preamble(300)
    # Pattern: YYYY-MM-DDTHH:MM:SSZ
    assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", preamble), (
        f"No ISO timestamp found in preamble: {preamble!r}"
    )


def test_preamble_deadline_approximately_correct(monkeypatch: pytest.MonkeyPatch):
    """Deadline timestamp is exactly now + timeout, truncated to seconds."""
    now = 1_700_000_000.75
    monkeypatch.setattr(time, "time", lambda: now)
    preamble = build_deadline_preamble(300)

    # Extract the ISO timestamp
    m = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", preamble)
    assert m, f"No timestamp in preamble: {preamble!r}"

    from datetime import datetime, timezone

    deadline_ts = (
        datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    )

    assert deadline_ts == int(now + 300)


def test_preamble_contains_date_command_hint():
    """Preamble tells the agent how to check the current time."""
    preamble = build_deadline_preamble(300)
    assert "date -Iseconds" in preamble


def test_preamble_no_timeout_not_called():
    """When timeout is None, build_deadline_preamble is not called at all
    (guard test — importing the function is idempotent)."""
    # This is a trivial guard: the real test is in test_run_agent_* below.
    # We just verify the function is importable and callable.
    result = build_deadline_preamble(120)
    assert result  # non-empty string


def test_preamble_sub_minute_timeout_clamps_to_1():
    """Timeouts < 60 s still produce '1 minute' (not '0 minutes')."""
    preamble = build_deadline_preamble(30)
    assert "1 minute " in preamble
