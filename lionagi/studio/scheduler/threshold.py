# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Metric threshold alerts: pure schema + comparison helpers for schedule.threshold_config.

Deliberately free of any DB/FastAPI import so the service write-boundary
validation (services/schedules.py) can call ``validate_threshold_config``
without pulling in StateDB or the scheduler engine -- same pattern as
subprocess.py's ``_validate_*`` helpers. Metric aggregation itself lives in
``StateDB.metric_value()``; the engine composes the two (see
``SchedulerEngine._evaluate_threshold_breach``).
"""

from __future__ import annotations

from typing import Any

VALID_METRICS: frozenset[str] = frozenset(
    {
        "failed_sessions",
        "total_cost_usd",
        "p95_latency_ms",
        # Observer self-health (ADR-0070 poller): point-in-time gauges, not
        # windowed aggregates -- see StateDB.metric_value.
        "github_poll_healthy_age_minutes",
        "github_poll_consecutive_401",
    }
)
VALID_OPS: frozenset[str] = frozenset({"gt", "gte"})
ALLOWED_KEYS: frozenset[str] = frozenset({"metric", "op", "value", "window_minutes"})


def validate_threshold_config(config: Any) -> None:
    """Raise ValueError if *config* is not a well-formed threshold spec.

    Shape: ``{"metric": ..., "op": ..., "value": ..., "window_minutes": ...}``
    -- exactly those four keys, nothing else. Rejecting unknown keys (rather
    than silently ignoring them) catches typos like ``cooldown_minutes``
    that would otherwise persist and mislead an operator about what the
    schedule is actually configured to do -- there is no cooldown field;
    the cooldown reuses ``window_minutes`` (see engine.py's stamp logic).
    """
    if not isinstance(config, dict):
        raise ValueError("threshold_config must be an object")

    unknown = set(config) - ALLOWED_KEYS
    if unknown:
        raise ValueError(
            f"threshold_config has unknown key(s) {sorted(unknown)}. "
            f"Allowed keys: {sorted(ALLOWED_KEYS)}"
        )

    metric = config.get("metric")
    if metric not in VALID_METRICS:
        raise ValueError(
            f"threshold_config.metric must be one of {sorted(VALID_METRICS)}, got {metric!r}"
        )

    op = config.get("op")
    if op not in VALID_OPS:
        raise ValueError(f"threshold_config.op must be one of {sorted(VALID_OPS)}, got {op!r}")

    value = config.get("value")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"threshold_config.value must be a number, got {value!r}")

    window_minutes = config.get("window_minutes")
    if (
        isinstance(window_minutes, bool)
        or not isinstance(window_minutes, int)
        or window_minutes < 1
    ):
        raise ValueError(
            f"threshold_config.window_minutes must be a positive integer, got {window_minutes!r}"
        )


def compare(op: str, observed: float, threshold: float) -> bool:
    """Evaluate ``observed <op> threshold``; op is pre-validated to gt/gte."""
    if op == "gt":
        return observed > threshold
    if op == "gte":
        return observed >= threshold
    raise ValueError(f"Unsupported threshold op: {op!r}")
