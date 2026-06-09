# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Recursive research engine — the first domain engine on the Engine base.

A team of casts-role agents explores a topic; when one emits a high-novelty
``FindingEmitted`` (or an explicit ``DepthRequested``), the engine spawns a
deeper exploration node — recursively, bounded by ``max_depth`` and topic
dedup. When the tree quiesces, a synthesizer reads every finding from the
emission store and writes the result. The decomposition logic lives in the
reaction rules, not a per-task DAG plan.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import Field

from lionagi.casts.emission import Conflict, Finding

from .engine import Engine, EngineEvent, EngineRun

__all__ = (
    "FindingEmitted",
    "DepthRequested",
    "ContradictionFound",
    "ResearchEngine",
)


class FindingEmitted(Finding):
    """A discovered claim — the casts ``Finding`` (description, evidence,
    confidence, severity, source) plus the two fields that gate depth expansion.
    It rides the bus inside a ``Signal``; no Observable base needed."""

    novelty: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="How non-obvious this finding is (0=well-known, 1=surprising). "
        "Above the engine's threshold it spawns a deeper exploration node.",
    )
    depth: int = Field(
        default=0, description="Recursion depth this was found at (the engine sets it)."
    )


class DepthRequested(EngineEvent):
    """An explicit request to explore a sub-question one level deeper.

    Research-specific recursion control — no casts equivalent (it is a signal to
    the engine, not a discovery)."""

    question: str = Field(description="The sub-question worth its own deeper investigation.")
    parent_depth: int = Field(
        default=0, description="Depth of the node raising the request (the engine sets it)."
    )


class ContradictionFound(Conflict):
    """Two findings that conflict — the casts ``Conflict`` (``sources`` +
    ``nature``) scored by ``severity``."""

    severity: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="How sharp the contradiction is (0=minor nuance, 1=stark).",
    )


_EMITS = (FindingEmitted, DepthRequested, ContradictionFound)


def _node_instruction(topic: str, depth: int, max_depth: int) -> str:
    deeper = (
        "If you uncover a genuinely novel sub-question worth its own investigation, "
        "emit a depth_requested (or a high-novelty finding) to spawn a deeper node. "
        if depth < max_depth
        else "You are at maximum depth — do NOT request further depth; consolidate instead. "
    )
    return (
        f"Research topic (depth {depth}/{max_depth}): {topic}\n\n"
        "Investigate concretely. For each substantive discovery, emit a "
        "finding_emitted with: description (the claim), evidence, novelty "
        "(0-1, how non-obvious), confidence (0-1). Surface conflicts as "
        "contradiction_found. "
        f"{deeper}"
        "Be specific and evidence-led; do not pad."
    )


def _synthesis_instruction(topic: str, findings: list[FindingEmitted], contradictions: list) -> str:
    parts = [
        f"Synthesize the research on: {topic}\n",
        f"\n# Findings ({len(findings)})",
    ]
    for i, f in enumerate(findings, 1):
        parts.append(
            f"\n## {i}. [{f.source or 'agent'} d={f.depth} "
            f"novelty={f.novelty:.2f} conf={f.confidence:.2f}]\n"
            f"- claim: {f.description}\n- evidence: {f.evidence}"
        )
    if contradictions:
        parts.append(f"\n\n# Contradictions ({len(contradictions)})")
        for c in contradictions:
            parts.append(f"\n- {' vs '.join(c.sources)} — {c.nature} (severity={c.severity:.1f})")
    parts.append(
        "\n\nProduce an integrated result: reconcile conflicts with evidence, name "
        "gaps no finding covered, and organize by theme — not by which agent found what."
    )
    return "".join(parts)


class ResearchEngine(Engine):
    """Recursive, reaction-driven research engine (stateless config).

    Parameters extend :class:`Engine` with:

    novelty_threshold
        A ``FindingEmitted`` above this novelty spawns a deeper node.
    roles
        Casts roles forming each exploration team (run in sequence, sharing
        output). Each is granted the research emissions.
    synthesis_role
        Casts role that writes the final synthesis from the emission store.
    repair_retries
        Re-prompt turns when an exploration node's whole team emitted no finding
        — the loop that keeps small/weak workers in the pipeline instead of a
        node silently producing nothing (ADR-0077 §3).

    All run-state (session, seen topics, in-flight nodes) lives on the
    per-call :class:`EngineRun`, so one engine runs many topics concurrently.
    """

    def __init__(
        self,
        *,
        novelty_threshold: float = 0.7,
        roles: tuple[str, ...] = ("researcher", "analyst", "critic"),
        synthesis_role: str = "synthesizer",
        repair_retries: int = 1,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.novelty_threshold = novelty_threshold
        self.roles = roles
        self.synthesis_role = synthesis_role
        self.repair_retries = repair_retries

    # -- lifecycle ------------------------------------------------------------

    async def _run(self, run: EngineRun, topic: str) -> str:
        """Explore *topic* recursively, then synthesize. Returns the synthesis text."""
        topic = topic.strip()
        if not topic:
            raise ValueError("topic is empty")
        run.root = topic
        run.seen(topic)  # mark the root so a child cannot re-explore it

        # Reaction rules — the engine's decomposition logic, bound to this run.
        run.observe(FindingEmitted, lambda f, _ctx: self._on_finding(run, f))
        run.observe(DepthRequested, lambda d, _ctx: self._on_depth(run, d))

        run.notify("node_registered", topic=topic, depth=0)
        team = await self._team_for(run, 0)
        await self._drive_node(run, team, _node_instruction(topic, 0, self.max_depth))

        # Drain the recursively-spawned depth nodes before synthesizing.
        await run.wait_quiescence()
        return await self._synthesize(run, topic)

    # -- reactions ------------------------------------------------------------

    def _on_finding(self, run: EngineRun, f: FindingEmitted) -> None:
        if f.novelty > self.novelty_threshold and f.depth < self.max_depth:
            run.spawn(self._explore(run, f.description, f.depth + 1))

    def _on_depth(self, run: EngineRun, d: DepthRequested) -> None:
        if d.parent_depth + 1 <= self.max_depth:
            run.spawn(self._explore(run, d.question, d.parent_depth + 1))

    # -- exploration ----------------------------------------------------------

    async def _team_for(self, run: EngineRun, depth: int) -> list:
        return [
            await run.make_agent(
                role, name=f"{role}-d{depth}", model=self.model_for(role), emits=_EMITS
            )
            for role in self.roles
        ]

    async def _explore(self, run: EngineRun, topic: str, depth: int) -> None:
        if depth > self.max_depth or run.seen(topic):
            return
        # Depth expansion spends a whole team — gate it on the quality judge
        # (off-topic / duplicative / trivial sub-questions stop here).
        jid = f"d{depth}-" + re.sub(r"[^a-zA-Z0-9]+", "-", topic).strip("-")[:32]
        if not await self.judge(run, jid, f"deeper exploration (depth {depth}): {topic}"):
            return
        run.notify("node_registered", topic=topic, depth=depth)
        async with run._sem:
            team = await self._team_for(run, depth)
            await self._drive_node(run, team, _node_instruction(topic, depth, self.max_depth))

    async def _drive_node(self, run: EngineRun, team: list, instruction: str) -> None:
        """Run an exploration team, then repair if the whole node emitted nothing.

        The team runs as before (sequential, build-on-prior); repair only fires
        when no ``FindingEmitted``/``DepthRequested`` arrived from this node — the
        weak-model case where every member returned prose. It re-prompts the
        consolidating (last) member; a node with real findings costs no extra
        call (early return). An empty team has nothing to drive or repair."""
        before = len(run.by_type(FindingEmitted)) + len(run.by_type(DepthRequested))
        await run.run_team(team, instruction)
        if not team:
            return

        def arrived() -> bool:
            return len(run.by_type(FindingEmitted)) + len(run.by_type(DepthRequested)) > before

        if arrived():
            return
        await run.operate_with_repair(
            team[-1],
            instruction,
            arrived=arrived,
            emits=(FindingEmitted,),
            retries=self.repair_retries,
        )

    async def _synthesize(self, run: EngineRun, topic: str) -> str:
        findings = run.by_type(FindingEmitted)
        contradictions = run.by_type(ContradictionFound)
        run.notify("synthesizing", findings=len(findings))
        synth = await run.make_agent(
            self.synthesis_role,
            name="synthesizer",
            model=self.model_for("synthesize"),
            exempt=True,
        )
        res = await synth.operate(
            instruction=_synthesis_instruction(topic, findings, contradictions)
        )
        return str(res) if res is not None else ""
