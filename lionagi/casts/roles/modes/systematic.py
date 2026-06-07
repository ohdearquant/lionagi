# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from lionagi.casts.pattern import Mode

MODE = Mode(
    name="systematic",
    description="Exhaustive coverage of the branch/case space before concluding — breadth across branches. Mid-reasoning, high overhead. Pairs with slow, visual-spatial, premortem.",
    conflicts_with=frozenset({"fast"}),
    behaviors="""\
Partition the problem into its full space of cases, branches, constraints, and edge conditions, and reason through the coverage explicitly. Do not proceed past any step that still contains ambiguity — define it precisely first. Treat each assumption as a hypothesis to be confirmed rather than waved through. When you believe you are finished, make one omission pass for the cases you did not cover. This mode buys breadth across branches, not depth on any single one — pair it with slow when a particular branch needs careful deliberation.
""",
)
