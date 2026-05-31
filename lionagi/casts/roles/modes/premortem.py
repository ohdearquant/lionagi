# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from lionagi.casts.pattern import Mode

MODE = Mode(
    name="premortem",
    description="Assume failure and trace causes and cascades before committing. Pre-reasoning, medium overhead. Pairs with probabilistic, systematic, metacognitive.",
    conflicts_with=frozenset(),
    behaviors="""\
Pick the load-bearing target in the current work — a planned action, a dependency, or a standing assumption — and assume it has already failed. List the two or three most likely causes and the cascade each would trigger, then state a remedy or recovery path for each before you proceed. Keep it proportional to stakes: a single sentence for a trivial step, a structured trace for a consequential one. Removing an assumption to see what collapses is analysis; doing it without a recovery path is sabotage — always pair each failure you surface with its repair. After acting, check briefly whether any anticipated failure materialized and whether anything unforeseen did.
""",
)
