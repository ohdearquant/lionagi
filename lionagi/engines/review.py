# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Dimensional review engine — fan-out per-dimension reviewers, adversarial verify, converge to a single ReviewVerdict."""

from __future__ import annotations

import hashlib
import re
import weakref
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
from lionagi.providers._provider_errors import (
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
    ProviderSafetyError,
    ProviderUnsupportedModelError,
)

from .engine import Engine, EngineEvent, EngineRun

__all__ = (
    "IssueFound",
    "DimensionClean",
    "VerifyResult",
    "ProposedVerdict",
    "ReviewVerdict",
    "ReviewEngine",
    "ReviewRun",
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

# Refusals that describe the run, not one attempt, so isolating one would
# publish a decision over an artifact nothing read.
_RUN_WIDE_REFUSALS: tuple[type[BaseException], ...] = (
    ProviderAuthError,
    ProviderQuotaError,
    ProviderSafetyError,
    ProviderUnsupportedModelError,
)


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


# The MCP SDK raises McpError for two conditions of its own — a closed
# connection and a request timeout — and also for every error object a server
# sends back. A server's error says something about the request, not the
# transport, and must not be swallowed as if the wire had dropped.
#
# The error code alone cannot tell those apart, because the code travels in the
# server's payload. A server is free to answer with the SDK's own numbers, and
# a buggy one reusing -32000 for its internal failures would have each of them
# recorded as a dropped connection.
_MCP_CONNECTION_CLOSED = -32000  # mcp.types.CONNECTION_CLOSED
_MCP_REQUEST_TIMEOUT = 408  # httpx.codes.REQUEST_TIMEOUT, the SDK's timeout code

# The SDK builds this one itself, verbatim, when the read loop ends with
# requests still waiting. It is matched exactly rather than by code alone
# because the peer-closed path and a server's own reply are raised from the
# same line and are otherwise identical. Should the SDK ever reword it, this
# stops recognising a dropped connection and the run fails loudly instead of
# degrading, which is the safe direction to be wrong in.
_MCP_CONNECTION_CLOSED_MESSAGE = "Connection closed"


def _carries_only_the_sdks_own_fields(error: object) -> bool:
    """True while the error object holds nothing the SDK would not have put there.

    The closed-connection signal is not raised as a distinct condition: the read
    loop synthesises an ordinary error reply and pushes it onto the same
    response stream a server's reply arrives on, so both surface from one line
    with an empty ``__context__``. Code and message are therefore the whole of
    what separates them, and both travel in the payload.

    The SDK constructs that reply with two fields and no others. ``ErrorData``
    permits a third and accepts unknown ones besides, so anything populated
    there came from a server and could not have come from the SDK. That does
    not close the ambiguity -- a server sending exactly the two fields with
    exactly the SDK's values is still indistinguishable here -- but it removes
    every server reply carrying detail alongside its code, which is what a
    server relaying an upstream failure typically sends.
    """
    if getattr(error, "data", None) is not None:
        return False
    return not getattr(error, "model_extra", None)


def _is_transport_mcp_error(exc: BaseException) -> bool:
    mcp_error = _mcp_error_type()
    if mcp_error is None or not isinstance(exc, mcp_error):
        return False
    error = getattr(exc, "error", None)
    code = getattr(error, "code", None)
    if code == _MCP_REQUEST_TIMEOUT:
        # The SDK raises its timeout from inside `except TimeoutError`, so the
        # chained context is set. A server answering 408 of its own is raised
        # outside any handler and carries no context, which separates the two
        # without reading the message.
        return isinstance(exc.__context__, TimeoutError)
    if code == _MCP_CONNECTION_CLOSED:
        # Known residual, stated here because this is where the call is made.
        # A server that answers with exactly this code, exactly this message
        # and no other field is indistinguishable from a real drop, and no
        # further check closes it: the SDK builds its closed-connection reply
        # with those two fields and pushes it onto the same response stream a
        # server's reply arrives on, so both surface from one raise with an
        # empty context. Code and message are the whole of the difference and
        # both travel in the server's payload.
        #
        # What bounds it is where the answer is used rather than how good the
        # answer is. A dimension classified either way is recorded as skipped
        # and forces a degraded result, so getting this wrong mislabels the
        # cause of a failure that is reported either way. It cannot turn a
        # failure into a pass. Closing the residual needs something outside
        # the exception -- whether the session survived, or whether the other
        # dimensions died with it -- which this signature cannot see.
        return getattr(
            error, "message", None
        ) == _MCP_CONNECTION_CLOSED_MESSAGE and _carries_only_the_sdks_own_fields(error)
    return False


def _is_all_isolated_failure(exc: BaseException) -> bool:
    """True iff every leaf is a per-dimension provider/transport failure, recursing into nested groups."""
    if isinstance(exc, _RUN_WIDE_REFUSALS):
        # Asked before the isolated set, not after. These derive from
        # ProviderError, so the wider test answers True for all of them and
        # this branch would never be reached from below it.
        return False
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


class ProposedVerdict(EngineEvent):
    """What synthesis concluded, before the evidence gate has ruled on it; not a ``Verdict``, which is how consumers find the decision."""

    verdict: str = Field(
        description="The proposed decision, e.g. APPROVE | APPROVE-WITH-FIXES | REQUEST-CHANGES | REJECT."
    )
    rationale: str = Field(default="", description="Why this decision, grounded in the findings.")
    blocking: list[str] = Field(
        default_factory=list, description="Issues that must be fixed before approval."
    )


class ReviewVerdict(Verdict):
    """Terminal review decision; extends Verdict with the list of blocking issues."""

    blocking: list[str] = Field(
        default_factory=list, description="Issues that must be fixed before approval."
    )


# Runs per session, weak both ways, so a run can see whether it started beside another.
_SESSION_RUNS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class ReviewRun(EngineRun):
    """Evidence scoped to this run; ``shares_session`` flags a Session this scoping cannot divide."""

    def __init__(self, engine: Engine, **kwargs: Any) -> None:
        super().__init__(engine, **kwargs)
        self._inherited: set[Any] = {e.id for e in self.session.observer.flow.items}
        # A run's window is everything emitted after it started, so any other run
        # alive on this Session emits into it. Which run produced a given event is
        # not recorded anywhere -- agent emissions reach the shared observer
        # through the branch, not through this run -- so the start snapshot cannot
        # divide them, whether the runs started together or one after the other.
        # Flagged both ways and on sight of any peer: a start-order test would
        # only ever be right about the emissions that had already happened.
        self.shares_session: bool = False
        peers = _SESSION_RUNS.setdefault(self.session, [])
        live = []
        for ref in peers:
            peer = ref()
            if peer is None:
                continue
            live.append(ref)
            peer.shares_session = True
            self.shares_session = True
        live.append(weakref.ref(self))
        _SESSION_RUNS[self.session] = live

    @property
    def events(self) -> list[Any]:
        return [e for e in self.session.observer.flow.items if e.id not in self._inherited]


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


def _withheld_note(unevidenced: tuple[str, ...]) -> str:
    """Name the uncovered dimensions: "review incomplete" sends the reader back to the logs."""
    names = ", ".join(unevidenced)
    return (
        "Approval withheld: no reviewer output was recorded for "
        f"{names}. A pass cannot rest on dimensions that produced nothing, "
        "whether their reviewer failed or never started."
    )


def _unverified_note(unverified: tuple[IssueFound, ...]) -> str:
    """Name the unresolved findings rather than counting them, as the coverage note does."""
    named = "; ".join(f"{i.dimension}: {i.description}" for i in unverified)
    return (
        "Approval withheld: these findings were never verified and were not "
        f"withdrawn — {named}. A pass cannot rest on a finding whose "
        "verification did not come back, whether the verifier failed or never "
        "returned a result."
    )


def _unaudited_note() -> str:
    """The audit was required and attempted; only its result is missing."""
    return (
        "Approval withheld: this run requires one adversarial audit of what it "
        "read and no verification came back, so the pass rests on nothing "
        "executed."
    )


def _cannot_attribute(run: EngineRun) -> bool:
    """True when the run's window holds another run's emissions and nothing records which is which."""
    return bool(getattr(run, "shares_session", False))


def _shared_session_note() -> str:
    """Unlike the coverage note, nothing on the stream is known to belong to this run."""
    return (
        "Approval withheld: another review run was alive on this session, and "
        "which run produced a given event is not recorded, so this run's "
        "evidence cannot be told apart from that run's. Give each run its own "
        "Session."
    )


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
        "Issue a single ProposedVerdict over the artifact from the issues below.\n",
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

    run_context_cls: type[EngineRun] = ReviewRun

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
        # The window this reads is the one _verdict refuses to read, and the newest
        # verdict in it may be a peer's, so exhaustion must not export it as this
        # run's result.
        if _cannot_attribute(run):
            run.notify("verdict", shared_session=True)
            return _shared_session_note()
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
        # Drain any adversarial verifiers spawned by high-severity issues. Each
        # was spawned already wrapped, so a dead verifier worker is recorded and
        # the drain stays clean; anything the wrapper does not claim still
        # reaches here and ends the run.
        await run.wait_quiescence()
        # A clean or minor-only review spawns no verifiers, so gate on zero
        # VerifyResult to make both shapes carry one adversarial audit.
        if self.verify_clean and not run.by_type(VerifyResult):
            await self._verify_clean_isolated(run, artifact, dims)
        return await self._verdict(run, artifact, dims)

    # -- reactions ------------------------------------------------------------

    def _on_issue(self, run: EngineRun, issue: IssueFound) -> None:
        if issue.severity in self.verify_severities and not run.seen(_verify_key(issue)):
            run.spawn(self._verify_isolated(run, issue))

    # -- stages ---------------------------------------------------------------

    async def _review_dimension_isolated(
        self, run: EngineRun, artifact: str, dimension: str
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
            marker = f"review-{dimension} ({error_type})"
            if marker not in run._emission_failures:
                run._emission_failures.append(marker)

    def _isolate_verification_failure(
        self, run: EngineRun, exc: BaseException, *, stage: str
    ) -> bool:
        """Record an isolated verification failure; False means the caller must re-raise."""
        if not _is_all_isolated_failure(exc):
            return False
        error_type = _failure_label(exc)
        run.notify("verification_failed", stage=stage, error_type=error_type)
        marker = f"{stage} ({error_type})"
        if marker not in run._emission_failures:
            run._emission_failures.append(marker)
        return True

    async def _verify_isolated(self, run: EngineRun, issue: IssueFound) -> None:
        try:
            await self._verify(run, issue)
        except Exception as exc:
            # Spawned into the run's background set, so an escape does not fail
            # this verifier alone: the drain collects it and re-raises, which
            # discards a review whose dimensions have already succeeded.
            if not self._isolate_verification_failure(run, exc, stage=f"verify-{issue.dimension}"):
                raise

    async def _verify_clean_isolated(
        self, run: EngineRun, artifact: str, dimensions: tuple[str, ...]
    ) -> None:
        try:
            await self._verify_clean(run, artifact, dimensions)
        except Exception as exc:
            if not self._isolate_verification_failure(run, exc, stage="verify-clean"):
                raise

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
                arrived=lambda: dimension in self._reported_dimensions(run),
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
                arrived=lambda: self._verification_arrived(run, issue),
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
        # Base runs carry no attribution, so they cannot report the condition.
        if _cannot_attribute(run):
            # The window holds another run's emissions and nothing records which
            # run produced what, so synthesising from it would put a different
            # artifact's findings in this verdict and could credit a dimension
            # this run never covered. Refuse before the prompt is built.
            note = _shared_session_note()
            final = ReviewVerdict(verdict="REQUEST-CHANGES", rationale=note, blocking=[])
            run.notify("verdict", shared_session=True)
            await run.emit(final)
            return note

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
            emits=(ProposedVerdict,),
            exempt=True,
        )
        res = await synth.operate(
            instruction=_verdict_instruction(artifact, dimensions, issues, verifications, clean)
        )
        text = str(res) if res is not None else ""

        unevidenced = self._unevidenced_dimensions(run, dimensions)
        unverified = self._unverified_findings(run)
        # The condition that spawned the audit, re-asked now: still true = it produced nothing.
        unaudited = bool(self.verify_clean) and not verifications
        proposals = run.by_type(ProposedVerdict)
        proposed = proposals[-1] if proposals else None
        final = self._rule(proposed, unevidenced, unverified, text, unaudited=unaudited)
        await run.emit(final)

        notes = []
        if unevidenced:
            notes.append(_withheld_note(unevidenced))
        if unverified:
            notes.append(_unverified_note(unverified))
        if unaudited:
            notes.append(_unaudited_note())
        if notes:
            joined = " ".join(notes)
            return f"{text}\n\n{joined}" if text else joined
        return text

    def _unevidenced_dimensions(
        self, run: EngineRun, dimensions: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Configured dimensions with no issue and no all-clear; reporters would drop the failed ones."""
        reported = self._reported_dimensions(run)
        return tuple(d for d in dimensions if d not in reported)

    def _verification_arrived(self, run: EngineRun, issue: IssueFound) -> bool:
        """Keyed on the echoed ref, description as fallback; one definition so repair and the gate cannot drift."""
        ref = _verify_ref(issue)
        return any(v.ref == ref or v.issue == issue.description for v in run.by_type(VerifyResult))

    def _unverified_findings(self, run: EngineRun) -> tuple[IssueFound, ...]:
        """Owed an outcome by the same severity set that spawns verifiers, and lacking one."""
        return tuple(
            issue
            for issue in run.by_type(IssueFound)
            if issue.severity in self.verify_severities
            and not self._verification_arrived(run, issue)
        )

    def _reported_dimensions(self, run: EngineRun) -> set[str]:
        """Dimensions that produced something this run can point at; repair and the coverage gate share it."""
        reported = {i.dimension for i in run.by_type(IssueFound)}
        reported |= {c.dimension for c in run.by_type(DimensionClean)}
        return reported

    def _rule(
        self,
        proposed: ProposedVerdict | None,
        unevidenced: tuple[str, ...],
        unverified: tuple[IssueFound, ...],
        text: str,
        *,
        unaudited: bool = False,
    ) -> ReviewVerdict:
        """The one verdict this run publishes; coverage and verification refuse approval only. Attribution refuses earlier, before synthesis reads a window it cannot divide."""
        verdict = (proposed.verdict if proposed else "").strip()
        rationale = (proposed.rationale if proposed else "") or text
        blocking = list(proposed.blocking) if proposed else []

        if proposed is None:
            return ReviewVerdict(
                verdict="REQUEST-CHANGES",
                rationale=(
                    "Synthesis produced no decision, so there is nothing to approve on. "
                    f"{text}".strip()
                ),
                blocking=blocking,
            )
        if verdict.upper().startswith("APPROVE") and (unevidenced or unverified or unaudited):
            notes = []
            if unevidenced:
                notes.append(_withheld_note(unevidenced))
            if unverified:
                notes.append(_unverified_note(unverified))
            if unaudited:
                notes.append(_unaudited_note())
            return ReviewVerdict(
                verdict="REQUEST-CHANGES",
                rationale=" ".join(notes) + (f" {rationale}" if rationale else ""),
                blocking=blocking + [i.description for i in unverified],
            )
        return ReviewVerdict(verdict=verdict, rationale=rationale, blocking=blocking)
