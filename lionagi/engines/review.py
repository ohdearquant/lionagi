# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Dimensional review engine — the second domain engine on the Engine base.

The artifact is reviewed along several *dimensions* in parallel (one reviewer
each, optionally in a dimension-appropriate cognitive mode). Reviewers emit
``IssueFound``; a high-severity issue reactively spawns an adversarial verifier
that tries to refute it (``VerifyResult``). When everything quiesces, a
synthesizer reads the emission store and issues a single ``ReviewVerdict``.

This is the *Dimensional* shape (fan-out lenses → adversarial verify →
converge), the complement to research's recursive *Tree* shape.
"""

from __future__ import annotations

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
    """One issue a reviewer found along a dimension — the casts ``Finding``
    (description, confidence, severity, evidence, source) plus the review locus.
    Reusing ``Finding`` means ``by_type(Finding)`` also surfaces review issues."""

    dimension: str = Field(description="The review lens that surfaced this (e.g. security).")
    location: str = Field(
        default="", description="Where in the artifact: path:line, symbol, or section."
    )
    severity: str = Field(default="minor", description="Impact: critical | major | minor.")


class VerifyResult(EngineEvent):
    """An adversarial verifier's call on whether an issue survives refutation —
    a refutation outcome, with no casts twin (neither a discovery nor a verdict)."""

    issue: str = Field(description="The issue description being verified.")
    holds: bool = Field(
        default=True, description="True only if the issue survives the strongest refutation."
    )
    rationale: str = Field(default="", description="Why it holds, or how it was refuted.")


class ReviewVerdict(Verdict):
    """The terminal review decision — the casts ``Verdict`` (verdict, rationale,
    evidence, reversible_by) plus the set of blocking issues."""

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
    """Dimensional review engine (stateless config).

    Parameters extend :class:`Engine` with:

    dimensions
        Review lenses; each runs a reviewer in parallel.
    reviewer_role / verifier_role / synthesis_role
        Casts roles for the reviewers, the adversarial verifier, and the final
        verdict author.
    verify_severities
        Issue severities that reactively spawn an adversarial verifier.
    """

    def __init__(
        self,
        *,
        dimensions: tuple[str, ...] = DEFAULT_DIMENSIONS,
        reviewer_role: str = "critic",
        verifier_role: str = "critic",
        synthesis_role: str = "synthesizer",
        verify_severities: tuple[str, ...] = ("critical", "major"),
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.dimensions = dimensions
        self.reviewer_role = reviewer_role
        self.verifier_role = verifier_role
        self.synthesis_role = synthesis_role
        self.verify_severities = set(verify_severities)

    async def _run(
        self, run: EngineRun, artifact: str, *, dimensions: tuple[str, ...] | None = None
    ) -> str:
        dims = tuple(dimensions) if dimensions else self.dimensions
        run.root = artifact
        run.observe(IssueFound, lambda i, _c: self._on_issue(run, i))

        # Fan out: one reviewer per dimension, in parallel.
        # Using ln_gather (structured concurrency) so a dimension failure cancels
        # siblings and no coroutine outlives this scope.
        try:
            await ln_gather(*(self._review_dimension(run, artifact, d) for d in dims))
        except BaseException:
            # Cancel any verifier tasks that were spawned before the failure so
            # no background work keeps mutating shared run state after _run exits.
            await run.cancel_active()
            raise
        # Drain any adversarial verifiers spawned by high-severity issues.
        await run.wait_quiescence()
        return await self._verdict(run, artifact, dims)

    # -- reactions ------------------------------------------------------------

    def _on_issue(self, run: EngineRun, issue: IssueFound) -> None:
        if issue.severity in self.verify_severities and not run.seen(f"verify:{issue.description}"):
            run.spawn(self._verify(run, issue))

    # -- stages ---------------------------------------------------------------

    async def _review_dimension(self, run: EngineRun, artifact: str, dimension: str) -> None:
        async with run._sem:
            mode = _DIM_MODE.get(dimension)
            agent = await run.make_agent(
                self.reviewer_role,
                name=f"review-{dimension}",
                modes=[mode] if mode else None,
                model=self.model_for("review"),
                emits=(IssueFound,),
            )
            await agent.operate(instruction=_dimension_instruction(artifact, dimension))

    async def _verify(self, run: EngineRun, issue: IssueFound) -> None:
        async with run._sem:
            verifier = await run.make_agent(
                self.verifier_role,
                name=f"verify-{issue.dimension}",
                modes=["adversarial"],
                model=self.model_for("verify"),
                emits=(VerifyResult,),
            )
            await verifier.operate(instruction=_verify_instruction(issue))

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
