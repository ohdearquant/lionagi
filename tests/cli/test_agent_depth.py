# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""`lionagi.cli._agent_depth` — the LIONAGI_AGENT_DEPTH env marker `li agent` and the
orchestration workers stamp, after refusing to run inside an agent lionagi started.

See docs/internals/cli.md for the mechanism (env inheritance survives
nohup/setsid/launchd reparenting; process ancestry does not, so the refusal reads both).
"""

from __future__ import annotations

import os

import pytest

from lionagi._provider_errors import NestedAgentRefusedError
from lionagi.cli._agent_depth import (
    DEPTH_ENV,
    SEAT_PROFILES_ENV,
    stamp_agent_depth,
    stamp_worker_depth,
)
from lionagi.providers import _subagent_admission as admission


@pytest.fixture(autouse=True)
def _clean_depth_env(monkeypatch):
    monkeypatch.delenv(SEAT_PROFILES_ENV, raising=False)


# stamp_agent_depth — seat vs non-seat vs no-profile


class TestStampAgentDepth:
    def test_seat_profile_stamps_zero(self, monkeypatch):
        monkeypatch.setenv(SEAT_PROFILES_ENV, "reviewer,tester")
        assert stamp_agent_depth("reviewer") == 0
        assert admission.child_agent_depth() == 0

    def test_non_seat_profile_stamps_one(self, monkeypatch):
        monkeypatch.setenv(SEAT_PROFILES_ENV, "reviewer")
        assert stamp_agent_depth("implementer") == 1

    def test_no_profile_treated_as_non_seat(self):
        assert stamp_agent_depth(None) == 1

    def test_empty_seat_set_by_default(self):
        """No LIONAGI_SEAT_PROFILES configured -> empty set -> every name
        is non-seat, per contract (operators must opt in explicitly)."""
        assert stamp_agent_depth("implementer") == 1

    def test_seat_names_are_stripped_and_empty_entries_ignored(self, monkeypatch):
        monkeypatch.setenv(SEAT_PROFILES_ENV, " reviewer , , tester ")
        assert stamp_agent_depth("reviewer") == 0
        assert stamp_agent_depth("tester") == 0

    def test_sets_env_var_and_the_childs_depth(self):
        depth = stamp_agent_depth("implementer")
        assert os.environ[DEPTH_ENV] == str(depth)
        assert admission.child_agent_depth() == depth

    @pytest.mark.parametrize("listed", [False, True])
    def test_refused_inside_an_agent_even_for_a_listed_profile(self, monkeypatch, listed):
        monkeypatch.setenv(SEAT_PROFILES_ENV, "reviewer")
        monkeypatch.setattr(admission, "_INHERITED_AGENT_DEPTH", 1)
        with pytest.raises(NestedAgentRefusedError, match="nested agent refused"):
            stamp_agent_depth("reviewer" if listed else "implementer")
        assert DEPTH_ENV not in os.environ
        assert admission.child_agent_depth() == 1

    def test_refused_under_an_ancestor_that_started_inside_an_agent(self, monkeypatch):
        monkeypatch.setattr(admission, "_ancestor_agent_depth", lambda: (1, 4242))
        with pytest.raises(NestedAgentRefusedError, match="pid 4242"):
            stamp_agent_depth("implementer")
        assert DEPTH_ENV not in os.environ


# stamp_worker_depth — always 1, never top-level


class TestStampWorkerDepth:
    def test_stamps_one_even_with_seat_profiles_configured(self, monkeypatch):
        monkeypatch.setenv(SEAT_PROFILES_ENV, "anything")
        assert stamp_worker_depth() == 1

    def test_sets_env_var(self):
        depth = stamp_worker_depth()
        assert os.environ[DEPTH_ENV] == str(depth)

    def test_refused_inside_an_agent(self, monkeypatch):
        monkeypatch.setattr(admission, "_INHERITED_AGENT_DEPTH", 2)
        with pytest.raises(NestedAgentRefusedError, match="LIONAGI_AGENT_DEPTH=2"):
            stamp_worker_depth()
        assert DEPTH_ENV not in os.environ


def test_a_second_stamp_in_one_process_is_not_refused_by_the_first():
    """`_run_agent` re-enters itself in process on auto-resume: the first stamp writes
    depth 1 into os.environ, and the second must neither read that as running inside an
    agent nor stamp any deeper."""
    first = stamp_agent_depth("implementer")
    second = stamp_agent_depth("implementer")

    assert first == 1
    assert second == 1
