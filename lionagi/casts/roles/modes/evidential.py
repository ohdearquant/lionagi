# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from lionagi.casts.pattern import Mode

MODE = Mode(
    name="evidential",
    description="Gate assertions by source support and inference traceability. All phases, medium overhead. Pairs with probabilistic, slow, adversarial.",
    conflicts_with=frozenset(),
    behaviors="""\
Before asserting any non-trivial claim, classify its support: direct source, indirect source, inference, or unsupported. Do not present inferences as facts — label them and state the evidence they rest on. When you summarize prior work, cite the artifact, not your memory of it. Record what you searched for and could not find; absence of a source is itself a data point worth logging. Classify the *quality* of support only — how likely a claim is *given* that support is the job of probabilistic mode, not this one.
""",
)
