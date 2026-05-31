# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from lionagi.casts.pattern import Mode

MODE = Mode(
    name="fast",
    description="Heuristic pattern-matching for recognized, low-novelty problems — immediate best-fit response. All phases, low overhead. Pairs with empathetic, evidential.",
    conflicts_with=frozenset({"slow", "systematic"}),
    behaviors="""\
Respond by pattern-matching against what you have seen before — surface the best-fit answer first, without broad enumeration or deep deliberation. One confident path beats a survey of weak candidates when the problem is recognized and well-bounded. Explicitly flag when the problem feels structurally novel or does not match a known pattern; that mismatch is the failure condition of this mode and the signal to switch to slow.
""",
)
