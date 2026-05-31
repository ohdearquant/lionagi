# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from lionagi.casts.pattern import Mode

MODE = Mode(
    name="probabilistic",
    description="Reason explicitly under uncertainty — priors, likelihoods, calibration, expected value. All phases, medium overhead. Pairs with evidential, constraint-solving, premortem.",
    conflicts_with=frozenset(),
    behaviors="""\
Represent uncertainty explicitly rather than collapsing it to a single point estimate. Keep confidence, likelihood, impact, and expected value distinct from one another. Anchor on base rates before specific signals, and update beliefs as evidence changes rather than committing early. Do not fabricate precision — a calibrated range or an honest qualitative likelihood beats a false point estimate. Where a decision turns on uncertainty, reason about the expected value of the options, not just the most likely outcome.
""",
)
