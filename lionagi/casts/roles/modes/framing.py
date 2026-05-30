# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from lionagi.casts.pattern import Mode

MODE = Mode(
    name="framing",
    description="Generate multiple problem representations before solving. Pre-reasoning, medium overhead. Pairs with associative, constraint-solving, empathetic.",
    conflicts_with=frozenset(),
    behaviors="""\
Before committing to a solution path, re-express the task under distinct assumptions, objectives, or boundaries — at least two or three genuinely different frames, not restatements of the same one. For each frame, note what it makes visible and what it hides. Choose the reasoning path deliberately from that comparison rather than accepting the first framing that came to mind. The goal is to vary the problem representation, not yet to generate solutions within a fixed one.
""",
)
