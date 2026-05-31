# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from lionagi.casts.pattern import Mode

MODE = Mode(
    name="adversarial",
    description="Stress-test claims by attacking their strongest version. Post-reasoning, medium overhead. Pairs with evidential, slow, empathetic.",
    conflicts_with=frozenset(),
    behaviors="""\
First reconstruct the target — claim, proposal, argument, or rationale — in its strongest defensible form; attacking a weak version proves nothing. Then go after that strongest version: hunt for false premises, invalid inference, missing evidence, and failure under pressure. Keep defects in the argument separate from defects in whoever produced it — you are testing the reasoning, not the author. State what evidence or change would actually neutralize each objection.
""",
)
