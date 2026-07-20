# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Dimensional review engine — fan-out per-dimension reviewers, adversarial verify, converge to a single ReviewVerdict."""

from __future__ import annotations

import re
from typing import Any

from pydantic import Field

from lionagi.casts.emission import Finding, Verdict
from lionagi.ln import gather as ln_gather

from .engine import Engine, EngineEvent, EngineRun

__all__ = (
    "IssueFound",
    "VerifyResult",
    "ReviewVerdict",
    "ReviewEngine",
    "DEFAULT_DIMENSIONS",
)


class IssueFound(Finding):
    """One issue found along a review dimension; extends Finding so by_type(Finding) also surfaces review issues."""

    dimension: str = Field(description="The review lens that surfaced this (e.g. security).")
    location: str = Field(
        default="", description="Where in the artifact: path:line, symbol, or section."
    )
    severity: str = Field(default="minor", description="Impact: critical | major | minor.")


class VerifyResult(EngineEvent):
    """Adversarial verifier's call on whether an issue survives refutation; no casts twin."""

    issue: str = Field(description="The issue description being verified.")
    holds: bool = Field(
        default=True, description="True only if the issue survives the strongest refutation."
    )
    rationale: str = Field(default="", description="Why it holds, or how it was refuted.")


class ReviewVerdict(Verdict):
    """Terminal review decision; extends Verdict with the list of blocking issues."""

    blocking: list[str] = Field(
        default_factory=list, description="Issues that must be fixed before approval."
    )


DEFAULT_DIMENSIONS: tuple[str, ...] = (
    "correctness",
    "security",
    "performance",
    "maintainability",
)

# A cognitive mode that fits each dimension's reasoning (best-effort; unknown
# dimensions just get no mode overlay).
_DIM_MODE: dict[str, str] = {
    "correctness": "systematic",
    "security": "adversarial",
    "performance": "evidential",
    "maintainability": "metacognitive",
}


_LOC_PAT = re.compile(r"^(?P<file>[\w./\\-]+?)[:@](?P<line>\d+)")


def _verify_key(issue: IssueFound) -> str:
    """Dedup key for adversarial verification. Two dimensions often surface the
    same defect with different wording, so keying on the raw description spawns
    duplicate heavyweight verifiers; when the location parses as path:line,
    bucket nearby lines of the same file together instead."""
    m = _LOC_PAT.match(issue.location.strip()) if issue.location else None
    if m:
        return f"verify:{m.group('file')}:{int(m.group('line')) // 25}"
    return f"verify:{issue.description}"


def _dimension_instruction(artifact: str, dimension: str) -> str:
    return (
        f"Review the artifact below for **{dimension}** only. For each concrete "
        "problem, emit an issue_found with: dimension, description, severity "
        "(critical|major|minor), location, confidence (0-1). Do not comment on "
        "other dimensions; do not pad with praise.\n\n"
        f"# Artifact\n{artifact}"
    )


def _verify_instruction(issue: IssueFound) -> str:
    return (
        "Adversarially verify this review issue — try to REFUTE it with the "
        "strongest counter-argument. Emit a verify_result with holds (true only "
        "if it survives refutation) and rationale.\n\n"
        f"- dimension: {issue.dimension}\n- severity: {issue.severity}\n"
        f"- location: {issue.location}\n- claim: {issue.description}"
    )


def _verdict_instruction(
    artifact: str, dimensions: tuple[str, ...], issues: list, verifications: list
) -> str:
    parts = [
        "Issue a single ReviewVerdict over the artifact from the issues below.\n",
        f"Dimensions reviewed: {', '.join(dimensions)}\n",
        f"\n# Issues ({len(issues)})",
    ]
    for i, it in enumerate(issues, 1):
        parts.append(
            f"\n## {i}. [{it.dimension}/{it.severity}] {it.description}"
            f"{(' @ ' + it.location) if it.location else ''}"
        )
    if verifications:
        parts.append(f"\n\n# Adversarial verifications ({len(verifications)})")
        for v in verifications:
            parts.append(f"\n- holds={v.holds}: {v.issue} — {v.rationale}")
    parts.append(
        "\n\nWeigh refuted issues down. Decide APPROVE / APPROVE-WITH-FIXES / "
        "REQUEST-CHANGES / REJECT with a grounded rationale and the list of "
        "blocking issues (if any)."
    )
    return "".join(parts)


class ReviewEngine(Engine):
    """Dimensional review engine (stateless config). See docs/reference/engines.md for parameter details."""

    def __init__(
        self,
        *,
        dimensions: tuple[str, ...] = DEFAULT_DIMENSIONS,
        reviewer_role: str = "critic",
        verifier_role: str = "critic",
        synthesis_role: str = "synthesizer",
        verify_severities: tuple[str, ...] = ("critical", "major"),
        repair_retries: int = 1,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.dimensions = dimensions
        self.reviewer_role = reviewer_role
        self.verifier_role = verifier_role
        self.synthesis_role = synthesis_role
        self.verify_severities = set(verify_severities)
        self.repair_retries = repair_retries

    # -- lifecycle --------------------------------------------------------------

    async def _partial_export(  # type: ignore[override]
        self, run: EngineRun, artifact: str, *, dimensions: tuple[str, ...] | None = None
    ) -> str:
        """Return an already-computed verdict after budget/deadline exhaustion
        instead of discarding it.

        A synthesis agent's structured emission is captured onto the session
        bus via the branch's async signal-emission side channel (on_message_
        added -> fire-and-forget emit_message()) independently of whether the
        ``synth.operate()`` call in ``_verdict`` itself ever returns — so a
        ReviewVerdict can already exist in ``run.by_type(ReviewVerdict)`` even
        though the deadline watchdog cancelled ``_run_task`` before ``_verdict``
        reached its ``return`` statement (e.g. a CLI-backed worker still
        retrying its emission). The base ``Engine._partial_export`` no-op
        would silently drop that verdict; this surfaces it, flagged via the
        normal EngineResult degrade signal.
        """
        verdicts = run.by_type(ReviewVerdict)
        if not verdicts:
            return ""
        verdict = verdicts[-1]
        run.notify("verdict_emitted_on_exhaustion", verdict=verdict.verdict)
        status_header = (
            "**status: budget_exhausted (verdict emitted on exhaustion)** — "
            "run terminated by deadline/budget after the verdict was computed "
            f"({run.agents_made} agents)\n\n"
        )
        blocking = f"\n\nBlocking: {', '.join(verdict.blocking)}" if verdict.blocking else ""
        return f"{status_header}{verdict.verdict}: {verdict.rationale}{blocking}"

    async def _run(
        self, run: EngineRun, artifact: str, *, dimensions: tuple[str, ...] | None = None
    ) -> str:
        dims = tuple(dimensions) if dimensions else self.dimensions
        run.root = artifact
        run.observe(IssueFound, lambda i, _c: self._on_issue(run, i))

        # Fan out one reviewer per dimension; ln_gather's structured concurrency
        # cancels siblings on a dimension failure so no coroutine outlives this scope.
        try:
            await ln_gather(*(self._review_dimension(run, artifact, d) for d in dims))
        except BaseException:
            # Cancel any verifier tasks spawned before the failure so no
            # background work mutates shared run state after _run exits.
            await run.cancel_active()
            raise
        # Drain any adversarial verifiers spawned by high-severity issues.
        await run.wait_quiescence()
        return await self._verdict(run, artifact, dims)

    # -- reactions ------------------------------------------------------------

    def _on_issue(self, run: EngineRun, issue: IssueFound) -> None:
        if issue.severity in self.verify_severities and not run.seen(_verify_key(issue)):
            run.spawn(self._verify(run, issue))

    # -- stages ---------------------------------------------------------------

    async def _review_dimension(self, run: EngineRun, artifact: str, dimension: str) -> None:
        emits = (IssueFound,)
        async with run._sem:
            mode = _DIM_MODE.get(dimension)
            agent = await run.make_agent(
                self.reviewer_role,
                name=f"review-{dimension}",
                modes=[mode] if mode else None,
                model=self.model_for("review"),
                emits=emits,
            )
            # Repair re-prompts a reviewer that emitted prose instead of fenced
            # issues; it never fabricates one, so a clean dimension emits nothing again.
            await run.operate_with_repair(
                agent,
                _dimension_instruction(artifact, dimension),
                arrived=lambda: any(i.dimension == dimension for i in run.by_type(IssueFound)),
                emits=emits,
                retries=self.repair_retries,
            )

    async def _verify(self, run: EngineRun, issue: IssueFound) -> None:
        emits = (VerifyResult,)
        async with run._sem:
            verifier = await run.make_agent(
                self.verifier_role,
                name=f"verify-{issue.dimension}",
                modes=["adversarial"],
                model=self.model_for("verify"),
                emits=emits,
            )
            await run.operate_with_repair(
                verifier,
                _verify_instruction(issue),
                arrived=lambda: any(
                    v.issue == issue.description for v in run.by_type(VerifyResult)
                ),
                emits=emits,
                retries=self.repair_retries,
            )

    async def _verdict(self, run: EngineRun, artifact: str, dimensions: tuple[str, ...]) -> str:
        issues = run.by_type(IssueFound)
        verifications = run.by_type(VerifyResult)
        run.notify("verdict", issues=len(issues), verifications=len(verifications))
        synth = await run.make_agent(
            self.synthesis_role,
            name="verdict",
            model=self.model_for("verdict"),
            emits=(ReviewVerdict,),
            exempt=True,
        )
        res = await synth.operate(
            instruction=_verdict_instruction(artifact, dimensions, issues, verifications)
        )
        return str(res) if res is not None else ""
