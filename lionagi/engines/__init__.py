# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""lionagi.engines — domain-specific agent engines over the reactive substrate.

An Engine is a *standing reaction machine* (ADR-0075): casts-role agents share a
Session and emit typed domain events onto the reactive bus (ADR-0072); the
engine's observers react — spawning more work, bounded by config — while the
emission store (queryable via ``engine.events[EventType]``) accumulates
everything for post-processing stages.

This is the complement to ``session.flow`` (plan-then-execute DAG): where flow
re-plans per task, an Engine encodes a *known domain's* decomposition once, as
reaction rules. ``Engine`` is the generic base; domain engines (research,
review, …) subclass it.
"""

from __future__ import annotations

from .engine import Engine, EngineEvent

__all__ = ("Engine", "EngineEvent")
