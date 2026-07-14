# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`lionagi.cli._agent_depth` — the inherited LIONAGI_AGENT_DEPTH env marker.

See docs/internals/cli.md for the mechanism (env inheritance survives
nohup/setsid/launchd reparenting; process ancestry does not).
"""

from __future__ import annotations

import pytest

from lionagi.cli import _agent_depth as depth_mod
from lionagi.cli._agent_depth import (
    DEPTH_ENV,
    SEAT_PROFILES_ENV,
    _parse_depth,
    inherited_depth,
    stamp_agent_depth,
    stamp_worker_depth,
)


@pytest.fixture(autouse=True)
def _clean_depth_env(monkeypatch):
    # Register cleanup for a key the production code writes directly via
    # os.environ (bypassing monkeypatch's own setenv tracking).
    monkeypatch.delenv(DEPTH_ENV, raising=False)
    monkeypatch.delenv(SEAT_PROFILES_ENV, raising=False)


# ---------------------------------------------------------------------------
# _parse_depth
# ---------------------------------------------------------------------------


class TestParseDepth:
    def test_none_is_zero(self):
        assert _parse_depth(None) == 0

    def test_unset_string_is_zero(self):
        assert _parse_depth("") == 0

    def test_garbage_is_zero(self):
        assert _parse_depth("not-a-number") == 0

    def test_negative_is_zero(self):
        assert _parse_depth("-3") == 0

    def test_valid_positive(self):
        assert _parse_depth("3") == 3

    def test_valid_zero(self):
        assert _parse_depth("0") == 0


# ---------------------------------------------------------------------------
# inherited_depth — import-captured, not a live re-read
# ---------------------------------------------------------------------------


class TestInheritedDepth:
    def test_returns_captured_module_constant(self, monkeypatch):
        monkeypatch.setattr(depth_mod, "_INHERITED_DEPTH", 5)
        assert inherited_depth() == 5

    def test_ignores_live_env_changes(self, monkeypatch):
        """A live re-read after the module's already captured a value would
        double-increment the in-process auto-resume recursion — must not
        happen."""
        monkeypatch.setattr(depth_mod, "_INHERITED_DEPTH", 2)
        monkeypatch.setenv(DEPTH_ENV, "99")
        assert inherited_depth() == 2


# ---------------------------------------------------------------------------
# stamp_agent_depth — seat vs non-seat vs no-profile
# ---------------------------------------------------------------------------


class TestStampAgentDepth:
    def test_seat_profile_resets_to_zero(self, monkeypatch):
        monkeypatch.setattr(depth_mod, "_INHERITED_DEPTH", 5)
        monkeypatch.setenv(SEAT_PROFILES_ENV, "reviewer,tester")
        assert stamp_agent_depth("reviewer") == 0

    def test_non_seat_profile_increments(self, monkeypatch):
        monkeypatch.setattr(depth_mod, "_INHERITED_DEPTH", 2)
        monkeypatch.setenv(SEAT_PROFILES_ENV, "reviewer")
        assert stamp_agent_depth("implementer") == 3

    def test_no_profile_treated_as_non_seat(self, monkeypatch):
        monkeypatch.setattr(depth_mod, "_INHERITED_DEPTH", 0)
        assert stamp_agent_depth(None) == 1

    def test_empty_seat_set_by_default(self, monkeypatch):
        """No LIONAGI_SEAT_PROFILES configured -> empty set -> every name
        is non-seat, per contract (operators must opt in explicitly)."""
        monkeypatch.setattr(depth_mod, "_INHERITED_DEPTH", 0)
        assert stamp_agent_depth("implementer") == 1

    def test_seat_names_are_stripped_and_empty_entries_ignored(self, monkeypatch):
        monkeypatch.setattr(depth_mod, "_INHERITED_DEPTH", 0)
        monkeypatch.setenv(SEAT_PROFILES_ENV, " reviewer , , tester ")
        assert stamp_agent_depth("reviewer") == 0
        assert stamp_agent_depth("tester") == 0

    def test_sets_env_var(self, monkeypatch):
        monkeypatch.setattr(depth_mod, "_INHERITED_DEPTH", 0)
        depth = stamp_agent_depth("implementer")
        import os

        assert os.environ[DEPTH_ENV] == str(depth)


# ---------------------------------------------------------------------------
# stamp_worker_depth — always parent+1, never a seat
# ---------------------------------------------------------------------------


class TestStampWorkerDepth:
    def test_always_increments_even_with_seat_profiles_configured(self, monkeypatch):
        monkeypatch.setattr(depth_mod, "_INHERITED_DEPTH", 0)
        monkeypatch.setenv(SEAT_PROFILES_ENV, "anything")
        assert stamp_worker_depth() == 1

    def test_increments_from_nonzero_inherited_depth(self, monkeypatch):
        monkeypatch.setattr(depth_mod, "_INHERITED_DEPTH", 3)
        assert stamp_worker_depth() == 4

    def test_sets_env_var(self, monkeypatch):
        monkeypatch.setattr(depth_mod, "_INHERITED_DEPTH", 1)
        depth = stamp_worker_depth()
        import os

        assert os.environ[DEPTH_ENV] == str(depth)


# ---------------------------------------------------------------------------
# Auto-resume recursion must not double-increment
# ---------------------------------------------------------------------------


def test_double_stamp_in_one_process_does_not_double_increment(monkeypatch):
    """Simulates `_run_agent`'s in-process auto-resume recursion: calling
    stamp_agent_depth twice in the same process (same captured
    _INHERITED_DEPTH) must yield the same depth both times, not depth+2."""
    monkeypatch.setattr(depth_mod, "_INHERITED_DEPTH", 0)

    first = stamp_agent_depth("implementer")
    second = stamp_agent_depth("implementer")

    assert first == 1
    assert second == 1
