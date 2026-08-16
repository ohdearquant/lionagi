# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Dimensional review engine — fan-out per-dimension reviewers, adversarial verify, converge to a single ReviewVerdict."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from pydantic import Field

from lionagi.casts.emission import Finding, Verdict
from lionagi.ln import gather as ln_gather
from lionagi.providers._provider_errors import ProviderError

from .engine import Engine, EngineEvent, EngineRun

__all__ = (
    "IssueFound",
    "DimensionClean",
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


class DimensionClean(EngineEvent):
    """Reviewer's affirmative all-clear for one dimension; no casts twin.

    A separate type rather than a sentinel IssueFound, so a "clean" dimension
    never surfaces as a phantom finding to a by_type(Finding) consumer, and
    silence stays distinguishable from an affirmed clean.
    """

    dimension: str = Field(description="The review lens that found no concrete problems.")
    rationale: str = Field(
        default="", description="One sentence on what was checked and found clean."
    )


class VerifyResult(EngineEvent):
    """Adversarial verifier's call on whether an issue survives refutation; no casts twin."""

    issue: str = Field(description="The issue description being verified.")
    ref: str = Field(
        default="", description="Echo of the engine-assigned claim ref, exactly as given."
    )
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


def _verify_ref(issue: IssueFound) -> str:
    """Short engine-assigned token the verifier echoes back (``ref='V-1a2b3c4d'``).

    Arrival detection keys on this rather than a verbatim echo of the (long,
    paraphrase-prone) issue description.
    """
    return f"V-{hashlib.sha256(_verify_key(issue).encode()).hexdigest()[:8]}"


def _clean_ref(dimensions: tuple[str, ...]) -> str:
    """Ref token for the clean-verdict audit — one per run, derived from the
    dimension set so the verdict stage can partition clean-audit VerifyResults
    from issue verifications by ref alone (the ``issue`` field is model-filled
    free text and paraphrases)."""
    key = "verify-clean:" + ",".join(sorted(dimensions))
    return f"V-{hashlib.sha256(key.encode()).hexdigest()[:8]}"


def _dimension_instruction(artifact: str, dimension: str) -> str:
    return (
        f"Review the artifact below for **{dimension}** only. For each concrete "
        "problem, emit an issue_found with: dimension, description, severity "
        "(critical|major|minor), location, confidence (0-1). If you find no "
        f"concrete problem, emit a dimension_clean with dimension='{dimension}' "
        "and a one-sentence rationale — never finish without emitting. Do not "
        "comment on other dimensions; do not pad with praise.\n\n"
        f"# Artifact\n{artifact}"
    )


def _verify_instruction(issue: IssueFound, ref: str) -> str:
    return (
        "Adversarially verify this review issue — try to REFUTE it with the "
        "strongest counter-argument. Emit a verify_result with issue (the claim "
        f"being verified), ref='{ref}' exactly as given, holds (true only "
        "if it survives refutation) and rationale.\n\n"
        f"- ref: {ref}\n- dimension: {issue.dimension}\n- severity: {issue.severity}\n"
        f"- location: {issue.location}\n- claim: {issue.description}"
    )


def _verify_clean_instruction(artifact: str, clean: list[DimensionClean], ref: str) -> str:
    claims = "\n".join(f"- {c.dimension}: {c.rationale or 'affirmed clean'}" for c in clean) or (
        "- (no affirmative clean events on record; the review surfaced no "
        "issues severe enough to verify)"
    )
    return (
        "Adversarially audit this review's CLEAN verdict — try to REFUTE it. "
        "You MUST execute at least one concrete check against the artifact "
        "before answering: resolve one central claim or citation the artifact "
        "makes, or run the check it claims passes. Your rationale MUST name "
        "the exact command you ran or the specific claim you resolved and "
        "what you observed — a rationale naming no executed check is invalid "
        "and will be rejected. Emit a verify_result with issue='CLEAN: review "
        f"affirmed no blocking issues', ref='{ref}' exactly as given, holds "
        "(true only if the clean verdict survives your strongest refutation) "
        "and rationale (what you executed, what you observed, why the clean "
        "verdict does or does not hold).\n\n"
        f"# Clean claims under audit\n{claims}\n\n# Artifact\n{artifact}"
    )


def _verdict_instruction(
    artifact: str,
    dimensions: tuple[str, ...],
    issues: list,
    verifications: list,
    clean: list[str] | None = None,
) -> str:
    parts = [
        "Issue a single ReviewVerdict over the artifact from the issues below.\n",
        f"Dimensions reviewed: {', '.join(dimensions)}\n",
    ]
    if clean:
        parts.append(f"Affirmed clean: {', '.join(dict.fromkeys(clean))}\n")
    parts.append(f"\n# Issues ({len(issues)})")
    for i, it in enumerate(issues, 1):
        parts.append(
            f"\n## {i}. [{it.dimension}/{it.severity}] {it.description}"
            f"{(' @ ' + it.location) if it.location else ''}"
        )
    # Clean-verdict audits carry the run's clean ref; their polarity is the
    # OPPOSITE of an issue verification — holds=false refutes the review's
    # clean verdict, not an issue — so they get their own section and their
    # own weighing guidance rather than riding the "weigh refuted issues
    # down" line, which would read them backwards.
    clean_ref = _clean_ref(dimensions)
    issue_verifications = [v for v in verifications if v.ref != clean_ref]
    clean_audits = [v for v in verifications if v.ref == clean_ref]
    if issue_verifications:
        parts.append(f"\n\n# Adversarial verifications ({len(issue_verifications)})")
        for v in issue_verifications:
            parts.append(f"\n- holds={v.holds}: {v.issue} — {v.rationale}")
    if clean_audits:
        parts.append(f"\n\n# Clean-verdict audit ({len(clean_audits)})")
        for v in clean_audits:
            parts.append(f"\n- holds={v.holds}: {v.rationale}")
        parts.append(
            "\nIn this section holds=false means the review's CLEAN verdict "
            "was REFUTED — weigh that AGAINST approval."
        )
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
        verify_clean: bool = True,
        repair_retries: int = 1,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.dimensions = dimensions
        self.reviewer_role = reviewer_role
        self.verifier_role = verifier_role
        self.synthesis_role = synthesis_role
        self.verify_severities = set(verify_severities)
        self.verify_clean = verify_clean
        self.repair_retries = repair_retries

    # -- lifecycle --------------------------------------------------------------

    async def _partial_export(  # type: ignore[override]
        self, run: EngineRun, artifact: str, *, dimensions: tuple[str, ...] | None = None
    ) -> str:
        """Return an already-computed verdict after budget/deadline exhaustion instead of discarding it.

        See docs/internals/providers.md#review-engine-partial-export-on-deadline.
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

        # Fan out one reviewer per dimension. Ordinary provider/transport
        # failures are isolated per dimension so completed sibling evidence is
        # still usable; run-wide budget exhaustion and cancellation keep their
        # existing structured-concurrency semantics.
        try:
            await ln_gather(
                *(self._review_dimension_isolated(run, artifact, dimension) for dimension in dims)
            )
        except BaseException:
            # Cancel any verifier tasks spawned before the failure so no
            # background work mutates shared run state after _run exits.
            await run.cancel_active()
            raise
        # Drain any adversarial verifiers spawned by high-severity issues.
        await run.wait_quiescence()
        # A clean or minor-only review spawns no issue verifiers, so it would
        # reach the verdict with zero VerifyResult — and a downstream evidence
        # floor then refuses the APPROVE as evidence-empty, structurally.
        # Gate on zero VerifyResult (not zero issues) so both shapes instead
        # carry one adversarial audit of the clean verdict itself: positive
        # executed evidence rather than absence.
        if self.verify_clean and not run.by_type(VerifyResult):
            await self._verify_clean(run, artifact, dims)
        return await self._verdict(run, artifact, dims)

    # -- reactions ------------------------------------------------------------

    def _on_issue(self, run: EngineRun, issue: IssueFound) -> None:
        if issue.severity in self.verify_severities and not run.seen(_verify_key(issue)):
            run.spawn(self._verify(run, issue))

    # -- stages ---------------------------------------------------------------

    async def _review_dimension_isolated(
        self, run: EngineRun, artifact: str, dimension: str
    ) -> None:
        try:
            await self._review_dimension(run, artifact, dimension)
        except ProviderError as exc:
            error_type = type(exc).__name__
            run.notify(
                "dimension_failed",
                dimension=dimension,
                error_type=error_type,
            )
            marker = f"review-{dimension} ({error_type})"
            if marker not in run._emission_failures:
                run._emission_failures.append(marker)

    async def _review_dimension(self, run: EngineRun, artifact: str, dimension: str) -> None:
        emits = (IssueFound, DimensionClean)
        async with run._sem:
            mode = _DIM_MODE.get(dimension)
            agent = await run.make_agent(
                self.reviewer_role,
                name=f"review-{dimension}",
                modes=[mode] if mode else None,
                model=self.model_for("review"),
                emits=emits,
            )
            # Repair re-prompts a reviewer that emitted prose instead of a
            # fenced emission. A clean dimension arrives as an affirmative
            # dimension_clean, so reaching the repair path means transport
            # failed — not that the dimension was clean.
            await run.operate_with_repair(
                agent,
                _dimension_instruction(artifact, dimension),
                arrived=lambda: (
                    any(i.dimension == dimension for i in run.by_type(IssueFound))
                    or any(c.dimension == dimension for c in run.by_type(DimensionClean))
                ),
                emits=emits,
                retries=self.repair_retries,
            )

    async def _verify(self, run: EngineRun, issue: IssueFound) -> None:
        emits = (VerifyResult,)
        ref = _verify_ref(issue)
        async with run._sem:
            verifier = await run.make_agent(
                self.verifier_role,
                name=f"verify-{issue.dimension}",
                modes=["adversarial"],
                model=self.model_for("verify"),
                emits=emits,
            )
            # Arrival keys on the echoed ref token; the verbatim-description
            # match stays only as a fallback for a verifier that filled issue
            # exactly but dropped the ref.
            await run.operate_with_repair(
                verifier,
                _verify_instruction(issue, ref),
                arrived=lambda: any(
                    v.ref == ref or v.issue == issue.description for v in run.by_type(VerifyResult)
                ),
                emits=emits,
                retries=self.repair_retries,
            )

    async def _verify_clean(
        self, run: EngineRun, artifact: str, dimensions: tuple[str, ...]
    ) -> None:
        emits = (VerifyResult,)
        ref = _clean_ref(dimensions)
        clean = run.by_type(DimensionClean)
        async with run._sem:
            verifier = await run.make_agent(
                self.verifier_role,
                name="verify-clean",
                modes=["adversarial"],
                model=self.model_for("verify"),
                emits=emits,
            )
            await run.operate_with_repair(
                verifier,
                _verify_clean_instruction(artifact, clean, ref),
                arrived=lambda: any(v.ref == ref for v in run.by_type(VerifyResult)),
                emits=emits,
                retries=self.repair_retries,
            )

    async def _verdict(self, run: EngineRun, artifact: str, dimensions: tuple[str, ...]) -> str:
        issues = run.by_type(IssueFound)
        verifications = run.by_type(VerifyResult)
        clean = [c.dimension for c in run.by_type(DimensionClean)]
        run.notify(
            "verdict", issues=len(issues), verifications=len(verifications), clean=len(clean)
        )
        synth = await run.make_agent(
            self.synthesis_role,
            name="verdict",
            model=self.model_for("verdict"),
            emits=(ReviewVerdict,),
            exempt=True,
        )
        res = await synth.operate(
            instruction=_verdict_instruction(artifact, dimensions, issues, verifications, clean)
        )
        return str(res) if res is not None else ""

# Fix for issue #3250: safe input handling
