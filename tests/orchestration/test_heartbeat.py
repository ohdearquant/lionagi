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
    sample_interval_seconds: float,
    age: float = 601,
) -> str | None:
    now = time.time()
    return flow_mod._heartbeat_warning(
        _running_segment(now, age),
        now=now,
        max_idle_seconds=600,
        sample_interval_seconds=sample_interval_seconds,
        previous=previous,
        current=current,
    )


def test_floor_ticks_without_working_delta_emit_stall_warning():
    warning = _warning(
        ({41: 8.0, 42: 3.0, 43: 5.0, 44: 2.0, 45: 4.0}, True),
        ({41: 8.03, 42: 3.03, 43: 5.03, 44: 2.03, 45: 4.03}, True),
        sample_interval_seconds=60,
    )

    assert warning is not None
    assert "IDLE STALL" in warning
    assert "median peer rate" in warning
    assert "60s sample" in warning


@pytest.mark.parametrize(
    "current",
    [
        ({41: 8.03}, True),
        ({41: 8.03, 42: 3.0, 43: 5.0}, True),
        ({41: 8.03, 42: 3.01, 43: 5.0}, True),
    ],
    ids=["single-descendant", "zero-peer-median", "mixed-jitter"],
)
def test_default_interval_floor_ticks_warn(current):
    warning = _warning(
        ({41: 8.0, 42: 3.0, 43: 5.0}, True),
        current,
        sample_interval_seconds=60,
    )

    assert warning is not None
    assert "fallback cutoff" in warning
    assert "60s sample" in warning


@pytest.mark.parametrize(
    ("sample_interval_seconds", "activity"),
    [(5, 0.09), (5, 0.15), (75, 0.375)],
    ids=["short-low", "short-maximum", "long-low-rate"],
)
def test_healthy_activity_without_peer_floor_does_not_warn(
    sample_interval_seconds,
    activity,
):
    warning = _warning(
        ({41: 8.0}, True),
        ({41: 8.0 + activity}, True),
        sample_interval_seconds=sample_interval_seconds,
    )

    assert warning is None


@pytest.mark.parametrize(
    "maximum_activity",
    [0.09, 0.15],
    ids=["below-legacy-cutoff", "observed-maximum"],
)
def test_short_interval_healthy_activity_does_not_warn(maximum_activity):
    now = time.time()
    warning = flow_mod._heartbeat_warning(
        _running_segment(now),
        now=now,
        max_idle_seconds=600,
        sample_interval_seconds=5,
        previous=({41: 8.0, 42: 3.0, 43: 5.0}, True),
        current=({41: 8.01, 42: 3.02, 43: 5.0 + maximum_activity}, True),
    )

    assert warning is None


def test_delta_at_working_cutoff_suppresses_stall_warning():
    assert (
        _warning(
            ({41: 8.0, 42: 3.0}, True),
            ({41: 8.1, 42: 3.02}, True),
            sample_interval_seconds=60,
        )
        is None
    )


def test_busy_survivor_suppresses_warning_during_pid_churn():
    assert (
        _warning(
            ({41: 8.0, 42: 3.0}, True),
            ({41: 8.2, 43: 1.0}, True),
            sample_interval_seconds=60,
        )
        is None
    )


def test_quiet_survivor_emits_warning_during_pid_churn():
    warning = _warning(
        ({41: 8.0, 42: 3.0}, True),
        ({41: 8.03, 43: 0.01}, True),
        sample_interval_seconds=60,
    )

    assert warning is not None
    assert "IDLE STALL" in warning


def test_busy_new_pid_suppresses_warning_during_pid_churn():
    assert (
        _warning(
            ({41: 8.0, 42: 3.0}, True),
            ({42: 3.03, 43: 1.80}, True),
            sample_interval_seconds=60,
        )
        is None
    )


def test_floor_cpu_new_pids_emit_stall_warning():
    warning = _warning(
        ({41: 8.0}, True),
        ({42: 0.01, 43: 0.02, 44: 0.03}, True),
        sample_interval_seconds=60,
    )

    assert warning is not None
    assert "IDLE STALL" in warning


def test_busy_new_pid_suppresses_warning_without_overlap():
    assert (
        _warning(
            ({41: 8.0}, True),
            ({42: 1.0}, True),
            sample_interval_seconds=60,
        )
        is None
    )


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
    assert _warning(previous, current, sample_interval_seconds=60, age=599) is None


def test_no_descendants_emits_louder_condition():
    warning = _warning(({}, True), ({}, True), sample_interval_seconds=60)

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
    assert _warning(previous, current, sample_interval_seconds=60) is None
