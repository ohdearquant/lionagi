# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Inherited agent-depth env marker — see docs/internals/cli.md."""

from __future__ import annotations

import os

from lionagi.service._subagent_admission import (
    AGENT_DEPTH_ENV,
    refuse_nested_agent,
    set_child_agent_depth,
)

DEPTH_ENV = AGENT_DEPTH_ENV
SEAT_PROFILES_ENV = "LIONAGI_SEAT_PROFILES"


def _seat_profiles() -> set[str]:
    raw = os.environ.get(SEAT_PROFILES_ENV, "")
    return {name.strip() for name in raw.split(",") if name.strip()}


def stamp_agent_depth(agent_name: str | None) -> int:
    """Set LIONAGI_AGENT_DEPTH for `li agent -a NAME`: 0 if LIONAGI_SEAT_PROFILES has NAME, else 1.

    Refused first when this process runs inside an agent lionagi started: such an agent
    starts no other through lionagi, API legs included (ADR-0133 D7). Past that check
    this process is at depth 0, so the depth it stamps is never above 1."""
    refuse_nested_agent()
    depth = 0 if agent_name and agent_name in _seat_profiles() else 1
    set_child_agent_depth(depth)
    return depth


def stamp_worker_depth() -> int:
    """Set LIONAGI_AGENT_DEPTH to 1 for fanout/flow/play workers, which are never top-level.

    Refused first, as :func:`stamp_agent_depth` is, inside an agent lionagi started."""
    refuse_nested_agent()
    set_child_agent_depth(1)
    return 1
