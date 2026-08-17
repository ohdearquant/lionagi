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

# Refusals that describe the run rather than one attempt. The credentials, the
# quota, the model and the safety policy are the same for every dimension, so a
# refusal on any of those grounds recurs identically on the next dimension and
# on any retry. They are ProviderError subclasses, so the isolated set above
# already covers them, and covering them is wrong: isolating one records a
# skipped dimension and lets the run reach a verdict, which turns a run-wide
# misconfiguration into a quietly degraded pass over an artifact whose
# dimensions were never actually read. A safety refusal is the sharpest case,
# because the reason the content was not reviewed is the finding.
#
# Context overflow is deliberately not here. That is a property of the single
# prompt that overflowed rather than of the run -- one verifier's oversized
# prompt says nothing about the next one's, which is why the engine repairs it
# instead of failing.
#
# A quota refusal belongs here despite being marked retryable, which looks like
# a contradiction and is not. Retryability is about time: the same request may
# well succeed once the limit resets. This tuple is about scope: at the moment
# it is raised, the limit applies to every dimension alike. A run in flight has
# no later, so isolating the quota failure would let the remaining dimensions
# report and the run publish a verdict over an artifact one dimension never
# read. Failing the whole run is the louder and more accurate outcome, and it
# is the same principle the coverage check enforces downstream.
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
    """What synthesis concluded, before the evidence gate has ruled on it.

    Synthesis proposes; the engine decides and publishes. The two were one step
    before, and the ordering that produced was wrong in a way no later
    correction can undo: the terminal event went onto the bus while synthesis
    ran, and the gate relabelled it afterwards. A consumer watching the stream
    had already seen the approval. An emitted decision cannot be recalled, so
    nothing may emit one before the thing that can refuse it has run.

    This is an ``EngineEvent`` and deliberately not a ``Verdict``. A consumer
    watching for the run's decision must not be able to mistake a proposal for
    one, and inheriting from ``Verdict`` would put it in exactly the type the
    verdict is found by.
    """

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
    """Evidence queries scoped to this run: a gate that admits another run's evidence is not a gate.

    Scoping is by identity of the events that predate the run, which is exact
    for sequential reuse of a session and blind to two runs started before
    either emitted. Separating those needs per-run attribution on the events,
    a change to what the observer records; short of that this notices the
    condition (``shares_session``) and the gate refuses to approve on it.
    """

    def __init__(self, engine: Engine, **kwargs: Any) -> None:
        super().__init__(engine, **kwargs)
        self._inherited: set[Any] = {e.id for e in self.session.observer.flow.items}
        # Equal snapshots = nothing emitted in between = overlap; sequential reuse
        # always leaves the later run a strictly larger one. Contamination is mutual.
        self.shares_session: bool = False
        peers = _SESSION_RUNS.setdefault(self.session, [])
        live = []
        for ref in peers:
            peer = ref()
            if peer is None:
                continue
            live.append(ref)
            if peer._inherited == self._inherited:
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
    """Say which dimensions were not covered, in the verdict itself.

    An approval withheld for missing coverage is only useful if the reader can
    tell what is missing; "review incomplete" sends them back to the logs to
    find out which reviewer died.
    """
    names = ", ".join(unevidenced)
    return (
        "Approval withheld: no reviewer output was recorded for "
        f"{names}. A pass cannot rest on dimensions that produced nothing, "
        "whether their reviewer failed or never started."
    )


def _unverified_note(unverified: tuple[IssueFound, ...]) -> str:
    """Say which findings never got their verification outcome, in the verdict.

    Named individually rather than counted. A reader deciding what to do next
    needs to know which claim is unresolved, and a count sends them back to the
    logs to find out -- the same reason the coverage note names its dimensions.
    """
    named = "; ".join(f"{i.dimension}: {i.description}" for i in unverified)
    return (
        "Approval withheld: these findings were never verified and were not "
        f"withdrawn — {named}. A pass cannot rest on a finding whose "
        "verification did not come back, whether the verifier failed or never "
        "returned a result."
    )


def _shared_session_note() -> str:
    """Unlike the coverage note, nothing on the stream is known to belong to this run."""
    return (
        "Approval withheld: another review run started against this session "
        "before either had emitted, so this run's evidence cannot be told "
        "apart from that run's. Give each run its own Session."
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
        # A clean or minor-only review spawns no issue verifiers, so it would
        # reach the verdict with zero VerifyResult and ship an APPROVE backed
        # by nothing executed. A consumer may well refuse that as
        # evidence-empty, but whether any given one does is a property of that
        # consumer's code and is not observable from this package, so it is not
        # a guard this engine gets to count on. Gate on zero VerifyResult (not
        # zero issues) so both shapes instead carry one adversarial audit of
        # the clean verdict itself: positive executed evidence rather than
        # absence, decided here where it can be seen.
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
        """Record an isolated verification failure; False means the caller must re-raise.

        Verification is discretionary work performed on evidence that already
        exists: by the time a verifier runs, its dimension has reported and its
        findings are on the run. A provider or transport failure here says the
        worker died, not that the review is unsound, so it degrades the audit of
        one finding rather than the run that produced it. The drain already
        treats one failure this way — ``EngineBudgetError`` is swallowed as
        discretionary work declined — and this extends the same reading to the
        transport failures the dimension stage isolates.

        The failure stays visible: the marker reaches the caller through
        ``_emission_failures`` and is reported alongside the result, so a run
        that verified less than it intended says so instead of presenting a
        verdict as fully audited.
        """
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
        # Base runs carry no attribution, so they cannot report the condition.
        shared = bool(getattr(run, "shares_session", False))
        proposals = run.by_type(ProposedVerdict)
        proposed = proposals[-1] if proposals else None
        final = self._rule(proposed, unevidenced, unverified, text, shared=shared)
        await run.emit(final)

        notes = []
        if shared:
            notes.append(_shared_session_note())
        if unevidenced:
            notes.append(_withheld_note(unevidenced))
        if unverified:
            notes.append(_unverified_note(unverified))
        if notes:
            joined = " ".join(notes)
            return f"{text}\n\n{joined}" if text else joined
        return text

    def _unevidenced_dimensions(
        self, run: EngineRun, dimensions: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Declared dimensions that produced nothing this run can point at.

        The set is enumerated from the dimensions the run was CONFIGURED with,
        never from the reviewers that happened to report. Deriving it from who
        reported would shrink the obligation by exactly the dimensions that
        failed, so a dimension whose worker died -- or never launched at all --
        would quietly leave the denominator and the gate would read clean on
        the smaller one. Died and never-born are different mechanisms and they
        need the same answer, and both of them look like this: nothing.

        A dimension counts as evidenced when it produced at least one issue or
        an affirmative all-clear. Reporting issues is execution evidence even
        when none of them drew a verifier -- whether an issue is verified is a
        severity policy, and a dimension that returned three minor findings
        demonstrably ran. Requiring verification instead would make a
        minor-only review unapprovable, since minors spawn no verifier and a
        dimension that reported issues has no all-clear to audit.
        """
        reported = self._reported_dimensions(run)
        return tuple(d for d in dimensions if d not in reported)

    def _verification_arrived(self, run: EngineRun, issue: IssueFound) -> bool:
        """Whether *issue* has its verification outcome on this run.

        Arrival keys on the echoed ref token; the verbatim-description match
        stays only as a fallback for a verifier that filled issue exactly but
        dropped the ref. Two issues that share a verify key share one verifier
        and therefore one ref, so the second is answered by the first's result
        rather than being owed one of its own.

        Read by repair, to decide whether to re-prompt, and by the gate, to
        decide whether an approval has an open question under it. One
        definition for the same reason the dimension predicate has one.
        """
        ref = _verify_ref(issue)
        return any(v.ref == ref or v.issue == issue.description for v in run.by_type(VerifyResult))

    def _unverified_findings(self, run: EngineRun) -> tuple[IssueFound, ...]:
        """Findings owed a verification outcome that do not have one.

        Coverage asks whether a dimension produced anything and cannot see
        this: a dimension that reported and then lost its verifier is covered
        and still has an open question underneath it. That is the shape the
        gate exists for -- an issue serious enough to be worth refuting, no
        refutation, and an approval resting on the gap.

        Which findings are owed an outcome is the severity policy's call, read
        from the same set that decides whether to spawn a verifier at all. A
        minor is not owed one, so a minor-only review stays approvable; taking
        the obligation from anywhere else would either invent work the engine
        never scheduled or forgive work it did.
        """
        return tuple(
            issue
            for issue in run.by_type(IssueFound)
            if issue.severity in self.verify_severities
            and not self._verification_arrived(run, issue)
        )

    def _reported_dimensions(self, run: EngineRun) -> set[str]:
        """Dimensions that have produced something this run can point at.

        Two decisions read this and they must not drift apart. Repair asks it
        to decide whether a reviewer arrived or needs re-prompting, and the
        coverage gate asks it to decide whether the verdict may approve. Two
        spellings of the same predicate would eventually disagree, and both
        directions of that disagreement are bad: repair stopping while the gate
        still calls the dimension unevidenced wastes a retry that would have
        helped, and the gate calling it evidenced while repair thinks otherwise
        approves over a dimension nothing ever heard from.
        """
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
        shared: bool = False,
    ) -> ReviewVerdict:
        """Turn the proposal into the one verdict this run publishes.

        Coverage, verification and attribution are separate questions and each
        refuses in one direction only: approval. A refusal already proposed
        stands, since none of the three could have removed an objection.
        """
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
        if verdict.upper().startswith("APPROVE") and (shared or unevidenced or unverified):
            notes = []
            if shared:
                notes.append(_shared_session_note())
            if unevidenced:
                notes.append(_withheld_note(unevidenced))
            if unverified:
                notes.append(_unverified_note(unverified))
            return ReviewVerdict(
                verdict="REQUEST-CHANGES",
                rationale=" ".join(notes) + (f" {rationale}" if rationale else ""),
                blocking=blocking + [i.description for i in unverified],
            )
        return ReviewVerdict(verdict=verdict, rationale=rationale, blocking=blocking)
