# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from lionagi.casts.pattern import Mode

MODE = Mode(
    name="slow",
    description="Deliberate step-by-step reasoning with every assumption questioned — depth on one chain. All phases, high overhead. Pairs with evidential, probabilistic, adversarial.",
    conflicts_with=frozenset({"fast"}),
    behaviors="""\
Work the problem one explicit step at a time, writing out each inference before drawing on it in the next. Make every step depend on a premise you have actually checked; pause on hidden assumptions, state them, and either confirm them or consciously accept the risk. Do not skip steps because they seem obvious — obvious-seeming steps are where silent errors accumulate. After reaching a conclusion, assume it is wrong and test whether the argument still holds. This mode buys depth on a single reasoning chain, not coverage of many — pair it with systematic when breadth is also required.
""",
)
