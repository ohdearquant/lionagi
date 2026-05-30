# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Reactive capability bus — live demo.

A claude_code/sonnet agent reads part of the codebase and, *as it goes*, emits
``Finding`` capabilities inline in its normal text (```json blocks). We don't
wait for the run to finish: a session observer fires on every Finding the
instant it streams in, so we can react in real time — log it, score it, or
spawn a deeper investigation.

Contrast with before: either we sat idle while the model ran, or we had to give
it a dedicated tool to call. Now any text response can carry typed signals and
the agentic loop keeps going uninterrupted.

Run::

    uv run python examples/capability_bus_demo.py
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

import lionagi as li
from lionagi.ln.types import Operable, Spec
from lionagi.session import Session


class Finding(BaseModel):
    claim: str = Field(description="One specific thing you learned about the code.")
    file: str = Field(default="", description="Where you saw it.")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


async def main() -> None:
    session = Session()
    branch = session.default_branch
    branch.chat_model = li.iModel(provider="claude_code", model="sonnet", verbose_output=True)

    # 1) Grant the capability — sets the runtime grant AND injects the
    #    instruction block so the model knows it may emit Finding inline.
    branch.grant_capabilities(Operable((Spec(Finding, name="finding"),), name="Caps"))

    findings: list[Finding] = []

    # 2) React the instant a Finding streams in — real-time, mid-run.
    @session.observe(Finding)
    async def dig_deeper(finding: Finding, _session: Session) -> None:
        findings.append(finding)
        print(f"\n  ⚡ FINDING #{len(findings)} (conf={finding.confidence}): {finding.claim}")
        if finding.file:
            print(f"     ↳ {finding.file}")
        # Real-time manipulation hook: e.g. on a high-confidence finding you
        # could spawn a sub-branch to investigate, enqueue a follow-up question,
        # or steer the run. Kept as a log here to stay cheap and bounded.
        if finding.confidence >= 0.8:
            print("     → high confidence; this is where dig_deeper() would branch off")

    # 3) Drive the agentic process. The run loop parses every assistant message
    #    for capability emissions and raises them onto the bus.
    prompt = (
        "Read lionagi/session/signal.py and lionagi/session/observer.py. "
        "As you go, whenever you understand something concrete about how the "
        "reactive bus works, immediately emit a Finding inline as a ```json "
        'block, e.g. {"finding": {"claim": "...", "file": "...", '
        '"confidence": 0.8}} — then keep reading. Emit 3-5 findings total, '
        "then give a one-line summary."
    )

    print("=== streaming claude_code/sonnet (findings fire live) ===")
    async for _msg in branch.run(prompt):
        pass  # messages also print via verbose_output; we only care about signals

    print("\n=== done ===")
    print(f"observer collected {len(findings)} findings")
    print(f"bus recorded {len(session.observer.by_type(Finding))} Finding-bearing signals")


if __name__ == "__main__":
    asyncio.run(main())
