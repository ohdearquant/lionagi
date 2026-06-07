# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from lionagi.casts.pattern import Mode

MODE = Mode(
    name="visual-spatial",
    description="Reason over topology, geometry, flow, and layers before sequential detail. Pre-reasoning, medium overhead. Pairs with systematic, framing, constraint-solving.",
    conflicts_with=frozenset(),
    behaviors="""\
Encode the problem internally as spatial structure — boxes, arrows, layers, regions, adjacency, flows — and reason from that shape before sequential detail. Reach for analogy early: what the system "is like" often reveals structural truth faster than what it "does." Prioritize the overall shape over step-by-step enumeration; if you find yourself listing steps before the whole structure is clear, zoom out. When two approaches are in tension, compare their shapes, not just their logic. This mode governs how you think, not what you emit — produce an external diagram only when the active role or artifact schema asks for one.
""",
)
