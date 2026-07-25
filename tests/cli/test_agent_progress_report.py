# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""The agent heartbeat must report progress, or say it cannot see any.

A timer that only proves the event loop is scheduling reads as "still working"
to whoever is deciding whether to kill the leg. These pin the distinction.
"""

from __future__ import annotations

import pytest

from lionagi import Branch
from lionagi.cli.agent import _ProgressReport


def _branch() -> Branch:
    return Branch(system="s")


def test_a_run_that_produces_nothing_never_implies_progress():
    branch = _branch()
    report = _ProgressReport(branch, now=0.0)

    lines = [report.line(t) for t in (60.0, 120.0, 180.0)]

    for line in lines:
        assert "no completed turn yet" in line
        assert "still running" not in line
        assert "turn," not in line
        assert "turns," not in line
    assert lines[-1] == "[progress] 180s elapsed — no completed turn yet (180s since start)"


def test_a_stalled_run_reports_a_stale_last_activity():
    branch = _branch()
    report = _ProgressReport(branch, now=0.0)

    branch.msgs.add_message(assistant_response="one turn's worth")
    first = report.line(60.0)
    assert "1 turn," in first
    assert "last activity 0s ago" in first

    # Nothing further arrives. The line must age rather than repeat.
    second = report.line(120.0)
    third = report.line(180.0)
    assert "last activity 60s ago" in second
    assert "last activity 120s ago" in third
    assert first != second != third


def test_tool_calls_advance_the_marker_too():
    branch = _branch()
    report = _ProgressReport(branch, now=0.0)

    branch.msgs.add_message(action_function="grep", action_arguments={"q": "x"})
    line = report.line(60.0)

    assert "0 turns" in line
    assert "1 tool call," in line
    assert "last activity 0s ago" in line


def test_only_this_runs_messages_count_toward_progress():
    """A resumed branch starts with history; that history is not progress."""
    branch = _branch()
    branch.msgs.add_message(assistant_response="from an earlier run")

    report = _ProgressReport(branch, now=0.0)

    assert "no completed turn yet" in report.line(60.0)


def test_unreadable_counts_report_unobservable_rather_than_working():
    class _NoMessages:
        @property
        def msgs(self):
            raise AttributeError("this engine exposes no message pile")

    report = _ProgressReport(_NoMessages(), now=0.0)
    line = report.line(60.0)

    assert "not observable for this engine" in line
    assert "alive, not working" in line
    assert "still running" not in line


class _Msgs:
    def __init__(self, messages):
        self.messages = messages


class _Broken:
    @property
    def messages(self):
        raise RuntimeError("pile went away")


class _Holder:
    def __init__(self, msgs):
        self.msgs = msgs


def _report_that_breaks_mid_run() -> tuple[_ProgressReport, _Holder, Branch]:
    real = _branch()
    holder = _Holder(_Msgs(real.msgs.messages))
    report = _ProgressReport(holder, now=0.0)
    return report, holder, real


def test_counts_becoming_unreadable_mid_run_does_not_leave_a_stale_claim():
    report, holder, real = _report_that_breaks_mid_run()
    real.msgs.add_message(assistant_response="one turn's worth")
    assert "1 turn," in report.line(60.0)

    holder.msgs = _Broken()
    line = report.line(120.0)
    assert "could not be read this tick" in line
    assert "alive, not working" in line
    assert "1 turn" not in line


def test_a_bad_tick_is_not_reported_as_a_property_of_the_engine():
    """The two unreadable states license different claims, so they read differently."""
    never_readable = _ProgressReport(_Holder(_Broken()), now=0.0).line(60.0)

    report, holder, _ = _report_that_breaks_mid_run()
    holder.msgs = _Broken()
    one_bad_tick = report.line(60.0)

    assert "not observable for this engine" in never_readable
    assert "not observable for this engine" not in one_bad_tick
    assert never_readable != one_bad_tick


@pytest.mark.parametrize("count,expected", [(1, "1 turn,"), (2, "2 turns,")])
def test_turn_count_reads_as_english(count, expected):
    branch = _branch()
    report = _ProgressReport(branch, now=0.0)
    for _ in range(count):
        branch.msgs.add_message(assistant_response="x")

    assert expected in report.line(60.0)
