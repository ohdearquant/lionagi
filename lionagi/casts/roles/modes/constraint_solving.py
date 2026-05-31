# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from lionagi.casts.pattern import Mode

MODE = Mode(
    name="constraint-solving",
    description="Filter by hard constraints before optimizing among feasible options. Mid-reasoning, medium overhead. Pairs with probabilistic, framing, systematic.",
    conflicts_with=frozenset(),
    behaviors="""\
First separate hard constraints — inviolable given current authorization — from soft preferences that are merely trade-offs. State the objective precisely enough that two candidate solutions can be compared. Enumerate only options that satisfy every hard constraint; do not pad the list with infeasible candidates as low-ranked entries, but do mark each infeasible option with the specific constraint it violates so a caller can choose to relax one if authorized. Optimize among the feasible set against the stated objective. Never silently relax a hard constraint — if nothing is feasible, report infeasibility with the binding constraint rather than returning the least-bad violation.
""",
)
