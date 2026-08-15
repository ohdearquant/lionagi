# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Dimensional review engine — fan-out per-dimension reviewers, adversarial verify, converge to a single ReviewVerdict."""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from typing import Any

import anyio
from pydantic import Field

from lionagi.casts.emission import Finding, Verdict
from lionagi.ln import gather as ln_gather
from lionagi.ln.concurrency._compat import (
    get_exception_group_exceptions,
    is_exception_group,
)
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

# Transport failures that kill one dimension's worker without saying anything
# about the run. A dropped MCP connection surfaces as the MCP SDK's own
# McpError, which derives from Exception rather than from ProviderError, and a
# dropped stream surfaces as anyio's — so neither is reachable by a
# ProviderError-only except clause even though both are exactly the
# "ordinary provider/transport failure" this stage means to isolate.
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    # A closed MCP response stream surfaces here: the SDK reads replies with
    # `await response_stream_reader.receive()`, which raises EndOfStream rather
    # than ClosedResourceError once the peer is gone.
    anyio.EndOfStream,
)

_ISOLATED_ERRORS: tuple[type[BaseException], ...] = (ProviderError, *_TRANSPORT_ERRORS)


@lru_cache(maxsize=1)
def _mcp_error_type() -> type[BaseException] | None:
    """Return mcp's ``McpError``, or ``None`` when the optional extra is absent.

    Resolved on first use rather than at module import. ``mcp`` is an optional
    extra, importing it pulls the whole package in, and every process that
    touches the engines package would pay that cost to obtain a type only a
    transport failure ever consults.

    A missing ``mcp`` is a normal configuration and yields ``None``. An ``mcp``
    that is present but fails to import is a broken installation and raises, so
    it cannot masquerade as "the extra is not installed" and silently disable
    the isolation this module depends on.
    """
    try:
        from mcp.shared.exceptions import McpError
    except ModuleNotFoundError as exc:
        if exc.name != "mcp":
            # The top-level package resolved but a submodule or dependency is
            # missing — a broken install, not an uninstalled extra.
            raise
        return None
    return McpError


# The MCP SDK spells only two conditions as McpError itself: a closed
# connection and a request timeout. Every other McpError relays a server-side
# error object verbatim — authorization refusals, application failures — which
# says something about the request, not the transport, and must not be
# swallowed as if the wire had dropped.
_MCP_TRANSPORT_CODES: frozenset[int] = frozenset(
    {
        -32000,  # mcp.types.CONNECTION_CLOSED
        408,  # httpx.codes.REQUEST_TIMEOUT, used by the SDK's own timeout raise
    }
)


def _is_transport_mcp_error(exc: BaseException) -> bool:
    mcp_error = _mcp_error_type()
    if mcp_error is None or not isinstance(exc, mcp_error):
        return False
    error = getattr(exc, "error", None)
    return getattr(error, "code", None) in _MCP_TRANSPORT_CODES


def _is_all_isolated_failure(exc: BaseException) -> bool:
    """True iff every leaf is a per-dimension provider/transport failure, recursing into nested groups."""
    if isinstance(exc, _ISOLATED_ERRORS):
        return True
    if _is_transport_mcp_error(exc):
        return True
    if is_exception_group(exc):
        return all(_is_all_isolated_failure(e) for e in get_exception_group_exceptions(exc))
    return False


def _failure_label(exc: BaseException) -> str:
    """Name the leaf cause(s), so a group reports what actually failed rather than 'ExceptionGroup'."""
    if not is_exception_group(exc):
        return type(exc).__name__
    seen: list[str] = []
    for leaf in get_exception_group_exceptions(exc):
        name = _failure_label(leaf)
        if name not in seen:
            seen.append(name)
    return "+".join(seen) if seen else type(exc).__name__


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


def _cap_approvals_on_missing_coverage(
    verdicts: list[ReviewVerdict], failed: list[tuple[str, str]]
) -> bool:
    """Rewrite any APPROVE-family verdict to REQUEST-CHANGES when a dimension
    never ran, recording each dead dimension as a blocking entry.

    Returns True when at least one verdict was capped. The mutation is on the
    emitted event itself so every downstream reader — not just the return
    string — sees a verdict that a run with missing coverage can actually
    stand behind.
    """
    if not failed:
        return False
    gap = ", ".join(f"{name} ({err})" for name, err in failed)
    capped = False
    for verdict in verdicts:
        if not verdict.verdict.strip().upper().startswith("APPROVE"):
            continue
        verdict.verdict = "REQUEST-CHANGES"
        verdict.rationale = (
            f"{verdict.rationale}\n[engine] Approval withdrawn: the following "
            f"dimensions did not run: {gap}. Their silence is missing coverage, "
            "not clean evidence, so an approval cannot be issued for this run."
        ).strip()
        for name, err in failed:
            entry = f"{name} dimension did not run ({err})"
            if entry not in verdict.blocking:
                verdict.blocking.append(entry)
        capped = True
    return capped


def _missing_coverage(
    run: Any, dimensions: tuple[str, ...], failed: list[tuple[str, str]]
) -> list[tuple[str, str]]:
    """Dimensions this run cannot claim to have reviewed, derived from evidence.

    A dimension counts as covered only when it produced something: an issue, or
    an affirmative clean. Deriving the gap from what arrived rather than from
    failure bookkeeping is what makes it complete. A reviewer that exhausts its
    emission repairs raises nothing, so it never reaches the isolated-failure
    recorder and never lands in *failed* — and a dimension that emitted nothing
    is indistinguishable from a clean one to everything downstream. This is the
    same predicate the repair loop uses to decide a dimension arrived, so a
    dimension the engine gave up on is exactly a dimension named here.

    *failed* entries keep their specific error label and their order; anything
    else missing is appended.
    """
    gap: list[tuple[str, str]] = list(failed)
    named = {name for name, _ in gap}
    covered = {i.dimension for i in run.by_type(IssueFound)} | {
        c.dimension for c in run.by_type(DimensionClean)
    }
    for dimension in dimensions:
        if dimension not in named and dimension not in covered:
            gap.append((dimension, "no evidence emitted"))
    return gap


def _cap_verdict_text(text: str, failed: list[tuple[str, str]]) -> str:
    """Front the returned verdict text with a refusal when a dimension never ran.

    Capping the emitted events only reaches a ReviewVerdict that synthesis
    actually issued. When it issues none, this string is the whole verdict as
    far as the caller is concerned, so the same invariant has to hold here:
    otherwise the channel that lacks it becomes the way an approval over
    missing coverage gets out. The synthesis output is kept underneath as
    quoted context so nothing is lost, and so a decision line inside it can no
    longer be read as this run's decision.
    """
    if not failed:
        return text
    blocking = "".join(f"- {name} dimension did not run ({err})\n" for name, err in failed)
    quoted = "\n".join(f"> {line}" for line in text.splitlines()) if text else "> (no output)"
    return (
        "DECISION: REQUEST-CHANGES\n"
        f"BLOCKING:\n{blocking}"
        "RATIONALE: the dimensions listed above did not execute. They produced "
        "no findings because they never ran, not because the artifact is clean "
        "along them, so their silence is missing coverage and this run cannot "
        "issue an approval.\n\n"
        "--- synthesis output below is context, not this run's decision ---\n"
        f"{quoted}"
    )


def _verdict_instruction(
    artifact: str,
    dimensions: tuple[str, ...],
    issues: list,
    verifications: list,
    clean: list[str] | None = None,
    failed: list[tuple[str, str]] | None = None,
) -> str:
    # A dimension whose reviewer died is not a dimension that was reviewed.
    # Naming it under "Dimensions reviewed" asserts coverage that does not
    # exist, and since a dead reviewer emits no issues, its silence otherwise
    # reads exactly like a clean result.
    failed = list(failed or [])
    failed_names = {name for name, _ in failed}
    reviewed = [d for d in dimensions if d not in failed_names]
    parts = [
        "Issue a single ReviewVerdict over the artifact from the issues below.\n",
        f"Dimensions reviewed: {', '.join(reviewed) if reviewed else '(none)'}\n",
    ]
    if failed:
        parts.append(
            "\nDID NOT RUN: "
            + "; ".join(f"{name} ({err})" for name, err in failed)
            + "\nThese dimensions failed. They produced no findings because they "
            "did not execute, not because the artifact is clean along them, so "
            "their silence is not evidence. Do not issue APPROVE on coverage "
            "that is missing: name the gap in the rationale and choose a "
            "verdict that reflects what was actually reviewed.\n"
        )
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
        # Exhaustion is the one exit that skips _verdict, so the cap has to be
        # applied here too. A run cut short by its deadline is the likeliest
        # one to be missing a dimension, which makes this the worst channel to
        # let an approval out of.
        gap = _missing_coverage(run, tuple(dimensions) if dimensions else self.dimensions, [])
        _cap_approvals_on_missing_coverage([verdict], gap)
        run.notify("verdict_emitted_on_exhaustion", verdict=verdict.verdict)
        status_header = (
            "**status: budget_exhausted (verdict emitted on exhaustion)** — "
            "run terminated by deadline/budget after the verdict was computed "
            f"({run.agents_made} agents)\n\n"
        )
        blocking = f"\n\nBlocking: {', '.join(verdict.blocking)}" if verdict.blocking else ""
        return _cap_verdict_text(
            f"{status_header}{verdict.verdict}: {verdict.rationale}{blocking}", gap
        )

    async def _run(
        self, run: EngineRun, artifact: str, *, dimensions: tuple[str, ...] | None = None
    ) -> str:
        dims = tuple(dimensions) if dimensions else self.dimensions
        run.root = artifact
        run.observe(IssueFound, lambda i, _c: self._on_issue(run, i))
        failed: list[tuple[str, str]] = []
        # Cap the verdict where it arrives, not after synthesis returns.
        # Everything downstream — persistence, subscribers, the notify stream —
        # reads the verdict from this flow, so rewriting it later leaves an
        # approval already delivered to whoever was listening. Synthesis runs
        # after every reviewer, so the coverage gap is final by the time this
        # fires.
        run.observe(
            ReviewVerdict,
            lambda v, _c: _cap_approvals_on_missing_coverage(
                [v], _missing_coverage(run, dims, failed)
            ),
        )

        # Fan out one reviewer per dimension. Ordinary provider/transport
        # failures are isolated per dimension so completed sibling evidence is
        # still usable; run-wide budget exhaustion and cancellation keep their
        # existing structured-concurrency semantics.
        try:
            await ln_gather(
                *(
                    self._review_dimension_isolated(run, artifact, dimension, failed)
                    for dimension in dims
                )
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
        return await self._verdict(run, artifact, dims, failed)

    # -- reactions ------------------------------------------------------------

    def _on_issue(self, run: EngineRun, issue: IssueFound) -> None:
        if issue.severity in self.verify_severities and not run.seen(_verify_key(issue)):
            run.spawn(self._verify(run, issue))

    # -- stages ---------------------------------------------------------------

    async def _review_dimension_isolated(
        self, run: EngineRun, artifact: str, dimension: str, failed: list[tuple[str, str]]
    ) -> None:
        try:
            await self._review_dimension(run, artifact, dimension)
        except Exception as exc:
            # Catch broadly and let the predicate decide, rather than naming the
            # isolated types in the clause: McpError is resolved lazily and so
            # cannot appear in a static tuple here. Anything the predicate does
            # not claim is re-raised unchanged. Cancellation derives from
            # BaseException and is therefore never caught.
            #
            # A group reaches here when the dimension's own task group collects
            # several transport failures at once. Isolate only when every leaf
            # is one: a mixed group carries something this stage has no claim
            # to swallow (budget exhaustion, a genuine defect), and laundering
            # it into a per-dimension degrade would hide it behind a verdict.
            if not _is_all_isolated_failure(exc):
                raise
            error_type = _failure_label(exc)
            run.notify(
                "dimension_failed",
                dimension=dimension,
                error_type=error_type,
            )
            # Record it for the verdict stage as well as the run's degrade
            # markers. Synthesis is otherwise handed the full requested
            # dimension list and no indication that one of them never ran,
            # which reads as "reviewed and found nothing".
            failed.append((dimension, error_type))
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

    async def _verdict(
        self,
        run: EngineRun,
        artifact: str,
        dimensions: tuple[str, ...],
        failed: list[tuple[str, str]] | None = None,
    ) -> str:
        issues = run.by_type(IssueFound)
        verifications = run.by_type(VerifyResult)
        clean = [c.dimension for c in run.by_type(DimensionClean)]
        run.notify(
            "verdict",
            issues=len(issues),
            verifications=len(verifications),
            clean=len(clean),
            failed=len(failed or []),
        )
        synth = await run.make_agent(
            self.synthesis_role,
            name="verdict",
            model=self.model_for("verdict"),
            emits=(ReviewVerdict,),
            exempt=True,
        )
        res = await synth.operate(
            instruction=_verdict_instruction(
                artifact, dimensions, issues, verifications, clean, failed
            )
        )
        # The instruction tells synthesis not to approve on missing coverage,
        # but an instruction is steering, not a gate: the model can still emit
        # APPROVE. Enforce it structurally — a run in which a dimension never
        # executed cannot produce an approval, whatever the synthesis text says.
        # Both channels carry the cap: the emitted verdict, and the returned
        # text, which is the only verdict a caller sees when synthesis emits
        # nothing structured at all.
        gap = _missing_coverage(run, dimensions, failed or [])
        if gap:
            _cap_approvals_on_missing_coverage(run.by_type(ReviewVerdict), gap)
            run.notify("verdict_capped", failed=", ".join(name for name, _ in gap))
        return _cap_verdict_text(str(res) if res is not None else "", gap)
