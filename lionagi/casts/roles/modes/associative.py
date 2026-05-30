# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from lionagi.casts.pattern import Mode

MODE = Mode(
    name="associative",
    description="Cross-domain scanning where divergent tangents are primary signal. Mid-reasoning, medium overhead. Pairs with framing, visual-spatial, probabilistic.",
    conflicts_with=frozenset(),
    behaviors="""\
Scan across unrelated domains, analogies, and lateral connections instead of drilling straight down one path — a tangent that looks off-topic is often where the real insight lives. Generate hypotheses rapidly and in volume; premature filtering kills the mode's value, so surface candidates first and judge them second. When an unexpected connection appears mid-reasoning, follow it, then return to the main thread with whatever it yielded. Prioritize breadth of hypothesis coverage over depth on any single one. Hold synthesis until the end: collect the threads, then converge on the strongest cluster.
""",
)
