# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Contract tests for descendant CPU heartbeat warnings."""

from __future__ import annotations

import time

import pytest

from lionagi.cli.orchestrate import flow as flow_mod


def _running_segment(now: float, age: float = 601) -> dict:
    return {
        "branch_name": "node",
        "status": "running",
        "started_at": now - age,
    }


def _warning(
    previous: flow_mod._DescendantCpuSample | None,
    current: flow_mod._DescendantCpuSample,
    *,
    age: float = 601,
) -> str | None:
    now = time.time()
    return flow_mod._heartbeat_warning(
        _running_segment(now, age),
        now=now,
        max_idle_seconds=600,
        previous=previous,
        current=current,
    )


def test_floor_ticks_without_working_delta_emit_stall_warning():
    warning = _warning(
        ({41: 8.0, 42: 3.0, 43: 5.0, 44: 2.0}, True),
        ({41: 8.0, 42: 3.01, 43: 5.02, 44: 2.03}, True),
    )

    assert warning is not None
    assert "IDLE STALL" in warning


def test_delta_at_working_cutoff_suppresses_stall_warning():
    assert (
        _warning(
            ({41: 8.0, 42: 3.0}, True),
            ({41: 8.1, 42: 3.02}, True),
        )
        is None
    )


def test_busy_survivor_suppresses_warning_during_pid_churn():
    assert (
        _warning(
            ({41: 8.0, 42: 3.0}, True),
            ({41: 8.2, 43: 1.0}, True),
        )
        is None
    )


def test_quiet_survivor_emits_warning_during_pid_churn():
    warning = _warning(
        ({41: 8.0, 42: 3.0}, True),
        ({41: 8.03, 43: 1.0}, True),
    )

    assert warning is not None
    assert "IDLE STALL" in warning


def test_empty_pid_intersection_emits_distinct_condition():
    warning = _warning(
        ({41: 8.0}, True),
        ({42: 1.0}, True),
    )

    assert warning is not None
    assert "NO CPU OVERLAP" in warning
    assert "IDLE STALL" not in warning
    assert "hung" not in warning.lower()


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (({41: 8.0}, True), ({41: 8.2}, True)),
        (({41: 8.0}, True), ({41: 8.0}, True)),
        (({41: 8.0}, True), ({42: 1.0}, True)),
        (({}, True), ({}, True)),
    ],
    ids=["busy", "quiet", "churn", "empty"],
)
def test_under_elapsed_threshold_never_warns(previous, current):
    assert _warning(previous, current, age=599) is None


def test_no_descendants_emits_louder_condition():
    warning = _warning(({}, True), ({}, True))

    assert warning is not None
    assert "NO DESCENDANTS" in warning
    assert "IDLE STALL" not in warning
    assert "hung" not in warning.lower()


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        (None, ({41: 8.0}, True)),
        (({41: 8.0}, False), ({41: 8.0}, True)),
        (({41: 8.0}, True), ({41: 8.0}, False)),
    ],
    ids=["first", "previous-incomplete", "current-incomplete"],
)
def test_unreadable_or_first_sample_is_silent(previous, current):
    assert _warning(previous, current) is None
