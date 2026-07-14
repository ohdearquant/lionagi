# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0
"""Inherited agent-depth env marker — see docs/internals/cli.md."""

from __future__ import annotations

import os

DEPTH_ENV = "LIONAGI_AGENT_DEPTH"
SEAT_PROFILES_ENV = "LIONAGI_SEAT_PROFILES"


def _parse_depth(raw: str | None) -> int:
    if raw is None:
        return 0
    try:
        value = int(raw)
    except ValueError:
        return 0
    return value if value >= 0 else 0


# Captured once at import, not re-read live: _run_agent recurses in-process on
# auto-resume, and a live re-read after the first stamp would double-increment.
_INHERITED_DEPTH = _parse_depth(os.environ.get(DEPTH_ENV))


def inherited_depth() -> int:
    """Depth this process inherited from its parent (import-time snapshot)."""
    return _INHERITED_DEPTH


def _seat_profiles() -> set[str]:
    raw = os.environ.get(SEAT_PROFILES_ENV, "")
    return {name.strip() for name in raw.split(",") if name.strip()}


def stamp_agent_depth(agent_name: str | None) -> int:
    """Set LIONAGI_AGENT_DEPTH for `li agent -a NAME`: 0 if NAME is a seat profile, else parent+1."""
    is_seat = bool(agent_name) and agent_name in _seat_profiles()
    depth = 0 if is_seat else inherited_depth() + 1
    os.environ[DEPTH_ENV] = str(depth)
    return depth


def stamp_worker_depth() -> int:
    """Set LIONAGI_AGENT_DEPTH for fanout/flow/play workers — never seats, always parent+1."""
    depth = inherited_depth() + 1
    os.environ[DEPTH_ENV] = str(depth)
    return depth
