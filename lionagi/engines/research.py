# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Recursive research engine — agent teams explore a topic, spawning deeper nodes on high-novelty findings until quiescence."""

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
    """A discovered claim extending Finding with novelty and depth fields that gate recursive expansion."""

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
    """Explicit request to explore a sub-question one level deeper; research-specific, no casts twin."""

    question: str = Field(description="The sub-question worth its own deeper investigation.")
    parent_depth: int = Field(
        default=0, description="Depth of the node raising the request (the engine sets it)."
    )


class ContradictionFound(Conflict):
    """Two conflicting findings; extends Conflict with a severity score."""

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


def _branch_emitted(branch: Any) -> bool:
    """True when *branch*'s own messages carry a FindingEmitted or DepthRequested emission (branch-local check, not run-store)."""
    caps = getattr(branch, "_capabilities", None)
    if caps is None:
        return False
    from lionagi.ln.types.filters import field_values
    from lionagi.operations._observe import attempt_extract
    from lionagi.protocols.messages import AssistantResponse

    kinds = (FindingEmitted, DepthRequested)
    for msg in branch.messages:
        if not isinstance(msg, AssistantResponse):
            continue
        bundles, _, _ = attempt_extract(msg.response, caps)
        for b in bundles:
            if isinstance(b, kinds):
                return True
            if any(isinstance(v, kinds) for v in field_values(b).values()):
                return True
    return False


class ResearchEngine(Engine):
    """Recursive, reaction-driven research engine (stateless config). See docs/reference/engines.md for parameter details."""

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

    async def _partial_export(  # type: ignore[override]
        self,
        run: EngineRun,
        topic: str,
    ) -> str:
        """Synthesize collected findings after budget cancellation; returns empty string if no findings exist."""
        findings = run.by_type(FindingEmitted)
        if not findings:
            return ""
        status_header = (
            "**status: budget_exhausted** — "
            f"run terminated by deadline/budget before completion "
            f"({run.agents_made} agents, "
            f"{len(findings)} findings collected)\n\n"
        )
        report = await self._synthesize(run, topic)
        return status_header + (report or "")

    async def _run(self, run: EngineRun, topic: str) -> str:
        """Explore *topic* recursively via reaction rules, wait for quiescence, then return the synthesis text."""
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
        # Depth expansion spends a whole team — gate on the judge (off-topic /
        # duplicative / trivial sub-questions stop here).
        jid = f"d{depth}-" + re.sub(r"[^a-zA-Z0-9]+", "-", topic).strip("-")[:32]
        if not await self.judge(run, jid, f"deeper exploration (depth {depth}): {topic}"):
            return
        run.notify("node_registered", topic=topic, depth=depth)
        async with run._sem:
            team = await self._team_for(run, depth)
            await self._drive_node(run, team, _node_instruction(topic, depth, self.max_depth))

    async def _drive_node(self, run: EngineRun, team: list, instruction: str) -> None:
        """Run team members sequentially with per-stage repair; falls back to a node-level repair if the whole node produced nothing."""
        if not team:
            return
        before = len(run.by_type(FindingEmitted)) + len(run.by_type(DepthRequested))
        last = ""
        for i, branch in enumerate(team):
            turn = instruction if i == 0 else f"Build on the prior work and continue:\n\n{last}"

            def _arrived(b: Any = branch) -> bool:
                return _branch_emitted(b)

            name = getattr(branch, "name", None) or f"agent-{i}"
            run.notify("agent_start", agent=name)
            try:
                async with run._sem:
                    res = await run.operate_with_repair(
                        branch,
                        turn,
                        arrived=_arrived,
                        emits=(FindingEmitted,),
                        retries=self.repair_retries,
                    )
                last = str(res) if res is not None else ""
                run.notify("agent_done", agent=name, chars=len(last))
            except Exception as exc:
                import logging as _log

                _log.getLogger("lionagi.engines").warning(
                    "research node agent %s failed: %s", name, exc
                )
                run.notify("agent_error", agent=name, error=str(exc))
                last = f"[{name} failed: {exc}]"
            run.drain_pending()

        def arrived() -> bool:
            return len(run.by_type(FindingEmitted)) + len(run.by_type(DepthRequested)) > before

        if arrived():
            return
        # Backstop: every per-stage repair failed — try once more with the last
        # member before giving up on this node entirely.
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
