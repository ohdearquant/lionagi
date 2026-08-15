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


class _RunCoverage:
    """Reads this run's evidence out of a flow that may hold more than this run's.

    ``run.by_type()`` reads the *session's* accumulated flow, and a session can
    outlive a run: ``EngineRun`` takes a caller-supplied ``Session`` and
    ``Engine.run()`` passes one through. On a reused session a previous run's
    evidence is therefore indistinguishable from this one's, which lets a
    dimension that emitted nothing this run pass as covered because it emitted
    something last run. That is the precise failure the coverage check exists
    to catch.

    Scoping is by exclusion: ``_run`` records which events were already in the
    flow before it started, and everything else in the flow belongs to this run.
    Filtering at read time rather than accumulating at emit time is what keeps
    it correct regardless of when the ledger is created relative to the
    evidence — accumulating from a subscription can only ever see what was
    emitted after the subscription, so a stage reached without one reports every
    dimension missing while the same artifact lists the issues they found.

    Constructed with no *run* the prior set is empty, meaning "nothing is known
    to predate this run, so treat the whole flow as ours". That is the only
    sound default when the run boundary was never recorded.
    """

    __slots__ = ("_prior",)

    def __init__(self, run: Any = None) -> None:
        tracked = (IssueFound, DimensionClean, ReviewVerdict)
        self._prior: dict[type, set[int]] = {
            event_type: ({id(e) for e in run.by_type(event_type)} if run is not None else set())
            for event_type in tracked
        }

    def _mine(self, run: Any, event_type: type) -> list[Any]:
        """Events of *event_type* in the flow that were not there before this run."""
        prior = self._prior.get(event_type, set())
        return [e for e in run.by_type(event_type) if id(e) not in prior]

    def issued(self, run: Any) -> set[str]:
        return {i.dimension for i in self._mine(run, IssueFound)}

    def clean(self, run: Any) -> set[str]:
        return {c.dimension for c in self._mine(run, DimensionClean)}

    def verdicts(self, run: Any) -> list[ReviewVerdict]:
        return self._mine(run, ReviewVerdict)

    def missing(
        self, run: Any, dimensions: tuple[str, ...], failed: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        """Dimensions this run cannot claim to have reviewed, derived from evidence.

        A dimension counts as covered only when it produced something: an issue,
        or an affirmative clean. Deriving the gap from what arrived rather than
        from failure bookkeeping is what makes it complete. A reviewer that
        exhausts its emission repairs raises nothing, so it never reaches the
        isolated-failure recorder and never lands in *failed* — and a dimension
        that emitted nothing is indistinguishable from a clean one to everything
        downstream.

        *failed* entries keep their specific error label and their order;
        anything else missing is appended.
        """
        gap: list[tuple[str, str]] = list(failed)
        named = {name for name, _ in gap}
        covered = self.issued(run) | self.clean(run)
        for dimension in dimensions:
            if dimension not in named and dimension not in covered:
                gap.append((dimension, "no evidence emitted"))
        return gap


def _gap_blocking(gap: list[tuple[str, str]]) -> list[str]:
    return [f"{name} dimension did not run ({err})" for name, err in gap]


_GAP_RATIONALE = (
    "The dimensions listed as blocking did not execute. They produced no "
    "findings because they never ran, not because the artifact is clean along "
    "them, so their silence is missing coverage and this run cannot issue an "
    "approval."
)


def _gap_verdict(gap: list[tuple[str, str]], summary: str) -> ReviewVerdict:
    """The verdict a run with missing coverage is entitled to, authored here.

    Engine-authored rather than model-authored: when coverage is incomplete the
    synthesis agent is never granted the ability to emit a ``ReviewVerdict``
    (see ``_verdict``), so this is the only one that exists for such a run. That
    is what makes the invariant structural — there is no approval to withdraw,
    because none was ever created.
    """
    rationale = _GAP_RATIONALE
    if summary.strip():
        rationale = f"{rationale}\n\n{summary.strip()}"
    return ReviewVerdict(
        verdict="REQUEST-CHANGES",
        rationale=rationale,
        blocking=_gap_blocking(gap),
        reversible_by=(
            "Re-run the review with every dimension executing; the gap is "
            "missing evidence, not an adverse finding."
        ),
    )


def _gap_verdict_text(verdict: ReviewVerdict) -> str:
    """Render an engine-authored gap verdict in the shape synthesis output uses.

    A gap run issues no synthesis call, so this string is the whole verdict as
    far as the caller is concerned. It keeps the ``DECISION:`` / ``BLOCKING:``
    shape that downstream readers already parse rather than inventing a second
    one for the degraded path.
    """
    blocking = "".join(f"- {entry}\n" for entry in verdict.blocking)
    return f"DECISION: {verdict.verdict}\nBLOCKING:\n{blocking}RATIONALE: {verdict.rationale}"


def _verdict_text(verdict: ReviewVerdict, prefix: str = "") -> str:
    blocking = f"\n\nBlocking: {', '.join(verdict.blocking)}" if verdict.blocking else ""
    return f"{prefix}{verdict.verdict}: {verdict.rationale}{blocking}"


def _issue_digest(issues: list[IssueFound]) -> str:
    """Render the issues the surviving dimensions did find.

    A run with a coverage gap issues no synthesis call, so nothing else would
    carry these into the returned text — and issues found by the dimensions
    that *did* run are the actionable half of a degraded review. Rendered here
    rather than summarised by a model so the degraded path stays deterministic
    and costs no agent.
    """
    if not issues:
        return ""
    lines = []
    for issue in issues:
        severity = f"{issue.severity}: " if issue.severity else ""
        lines.append(f"- [{issue.dimension}] {severity}{issue.description}")
    return f"Issues found by the dimensions that did run ({len(issues)}):\n" + "\n".join(lines)


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
        # This run's verdicts, not the session's: run.by_type() reads the
        # session-accumulated flow, so on a reused session this exit would
        # export a *previous* run's verdict as this one's result.
        verdicts = self._coverage(run).verdicts(run)
        if not verdicts:
            return ""
        verdict = verdicts[-1]
        # No coverage cap is applied here, and none is needed. A ReviewVerdict
        # exists in this run only if _verdict granted synthesis the capability
        # to emit one, and it grants that only when coverage is complete. An
        # exhausted run that never reached _verdict has no verdict to export.
        run.notify("verdict_emitted_on_exhaustion", verdict=verdict.verdict)
        status_header = (
            "**status: budget_exhausted (verdict emitted on exhaustion)** — "
            "run terminated by deadline/budget after the verdict was computed "
            f"({run.agents_made} agents)\n\n"
        )
        return _verdict_text(verdict, status_header)

    async def _run(
        self, run: EngineRun, artifact: str, *, dimensions: tuple[str, ...] | None = None
    ) -> str:
        dims = tuple(dimensions) if dimensions else self.dimensions
        run.root = artifact
        # Marks where this run begins in a flow the session may already have
        # filled, so coverage read later is this run's and not a previous one's.
        self._coverage(run, mark_start=True)
        run.observe(IssueFound, lambda i, _c: self._on_issue(run, i))
        failed: list[tuple[str, str]] = []

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

    @staticmethod
    def _coverage(run: EngineRun, *, mark_start: bool = False) -> _RunCoverage:
        """This run's evidence reader, created once per run.

        Attached to the run rather than the engine: one engine serves many
        runs, and keying per-run state off the engine is how a previous run's
        coverage leaks into this one's, which is the same mistake as reading it
        back from a shared session.

        ``mark_start=True`` records what the flow already held, and only ``_run``
        passes it, because only ``_run`` knows where this run begins. Reached any
        other way the reader is built with an empty prior set, which treats the
        whole flow as this run's — the only sound reading when the boundary was
        never recorded, and the behaviour a direct call to a single stage has
        always had.
        """
        existing = getattr(run, "_review_coverage", None)
        if existing is None:
            existing = _RunCoverage(run if mark_start else None)
            run._review_coverage = existing  # type: ignore[attr-defined]
        return existing

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
        coverage = self._coverage(run)
        issues = run.by_type(IssueFound)
        verifications = run.by_type(VerifyResult)
        affirmed_clean = coverage.clean(run)
        clean = [d for d in dimensions if d in affirmed_clean]
        gap = coverage.missing(run, dimensions, list(failed or []))
        run.notify(
            "verdict",
            issues=len(issues),
            verifications=len(verifications),
            clean=len(clean),
            failed=len(failed or []),
        )
        # Every reviewer has returned by now, so the coverage gap is final.
        #
        # When a dimension never ran, synthesis is not given the ability to
        # issue a verdict at all: no agent is created with a ReviewVerdict
        # emission grant, so no approval can be constructed, dispatched, or
        # observed. The earlier shape of this code let synthesis emit freely and
        # rewrote an approval afterwards, which cannot be airtight — the bus
        # gathers handlers concurrently and a subscriber registered before the
        # run has already received the event by the time any rewrite runs. An
        # approval that is never created needs no withdrawing.
        if gap:
            run.notify("verdict_withheld", failed=", ".join(name for name, _ in gap))
            verdict = _gap_verdict(gap, _issue_digest(issues))
            await run.emit(verdict)
            return _gap_verdict_text(verdict)

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
        return str(res) if res is not None else ""
