# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from lionagi.casts.pattern import Mode

MODE = Mode(
    name="metacognitive",
    description="Second-order monitoring — watch reasoning and outputs for drift from the assigned objective. Continuous, low overhead. Pairs with slow, evidential, premortem.",
    conflicts_with=frozenset(),
    behaviors="""\
Continuously check whether the active reasoning path still matches the assigned task, role, constraints, and selected modes — not just a generic quality bar. Distinguish a bad output from a good output aimed at the wrong task; they need different corrections. Flag drift early, since a signal at 20% done is far cheaper to act on than one at 90%, and back every flag with a specific excerpt rather than a general impression. If your own reasoning starts drifting from the objective, name the drift explicitly before continuing. Monitor and surface only — issuing a binding verdict is role authority, not part of this mode.
""",
)
