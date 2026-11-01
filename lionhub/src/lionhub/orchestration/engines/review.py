# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

"""Dimensional review engine — fan-out per-dimension reviewers, adversarial verify, converge to a single ReviewVerdict."""

from __future__ import annotations

import re
import weakref
from functools import lru_cache
from typing import Any

import anyio
from pydantic import Field

from lionagi._errors import ConfigurationError
from lionagi._provider_errors import (
    ProviderAuthError,
    ProviderError,
    ProviderQuotaError,
    ProviderSafetyError,
    ProviderUnsupportedModelError,
)
from lionagi.casts.emission import Finding, Verdict
from lionagi.ln import compute_hash
from lionagi.ln import gather as ln_gather
from lionagi.ln.concurrency import fail_after
from lionagi.ln.concurrency._compat import (
    get_exception_group_exceptions,
    is_exception_group,
)

from .engine import Engine, EngineEvent, EngineRun, check_max_tokens

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
_MCP_TRANSPORT_PROBE_TIMEOUT_S = 5.0


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
        # This predicate deliberately remains exception-only. A server that
        # answers with exactly this code, message, and field set is identical
        # here to the SDK's synthesized disconnect signal. The async isolation
        # boundary resolves that residual with the originating client's ping
        # when the MCP transport layer was able to preserve one.
        # This function therefore returns the provisional transport answer;
        # callers that own liveness evidence refine it before deciding whether
        # to isolate the dimension.
        return getattr(
            error, "message", None
        ) == _MCP_CONNECTION_CLOSED_MESSAGE and _carries_only_the_sdks_own_fields(error)
    return False


async def _is_all_isolated_failure_after_probe(exc: BaseException) -> bool:
    """Apply transport liveness evidence to the otherwise ambiguous MCP error.

    A missing or failed probe preserves the historical classification: a real
    disconnect must still be isolated even when no client handle reached this
    layer. Only a successful round trip proves that the peer sent the bare
    connection-closed payload while keeping its transport open.
    """
    if not _is_all_isolated_failure(exc):
        return False
    if is_exception_group(exc):
        for leaf in get_exception_group_exceptions(exc):
            if not await _is_all_isolated_failure_after_probe(leaf):
                return False
        return True
    if not _is_transport_mcp_error(exc):
        return True

    error = getattr(exc, "error", None)
    if getattr(error, "code", None) != _MCP_CONNECTION_CLOSED:
        return True

    from lionagi.service.connections.mcp_wrapper import _mcp_transport_probe

    probe = _mcp_transport_probe(exc)
    if not callable(probe):
        return True
    try:
        with fail_after(_MCP_TRANSPORT_PROBE_TIMEOUT_S):
            result = await probe()
    except Exception:
        return True
    return result is False


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
    served_model: str | None = Field(
        default=None,
        description=(
            "The model the provider named for the last synthesis call; None when it "
            "named none or no call was made."
        ),
    )


# Runs per session, weak both ways, so a run can see whether it started beside another.
_SESSION_RUNS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


class _ArtifactTurn:
    """The artifact sent once, in a turn of its own, ahead of every dimension's task.

    A prompt-cache entry covers a request's prefix up to a marked block, so text that
    differs per dimension has to come after the artifact for the dimensions to share
    one entry. The first dimension sends the artifact alone; its request writes the
    entry, and every other dimension waits for it and then starts from a copy of that
    turn, so their requests read the artifact instead of each writing it again.
    """

    def __init__(self) -> None:
        self._claimed = False
        self.ready = anyio.Event()
        # The (Instruction, AssistantResponse) pair a dimension's history starts with,
        # or None once the leader found the turn unusable: then each dimension sends
        # the artifact with its own task.
        self.messages: tuple[Any, Any] | None = None

    def claim(self) -> bool:
        """True for the first caller only: that dimension sends the turn."""
        if self._claimed:
            return False
        self._claimed = True
        return True

    def share(self, messages: tuple[Any, Any] | None) -> None:
        self.messages = messages
        self.ready.set()


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
        self._dimension_failures: dict[str, str] = {}
        self._artifact_turn: _ArtifactTurn | None = None
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

# The stages ReviewEngine builds agents for, i.e. the keys stage_max_tokens can name.
_MAX_TOKENS_STAGES: tuple[str, ...] = ("review", "review_fallback", "verify", "verdict")


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
    return f"V-{compute_hash(_verify_key(issue), max_bytes=None)[:8]}"


def _clean_ref(dimensions: tuple[str, ...]) -> str:
    """Ref token for the clean-verdict audit — one per run, derived from the
    dimension set so the verdict stage can partition clean-audit VerifyResults
    from issue verifications by ref alone (the ``issue`` field is model-filled
    free text and paraphrases)."""
    key = "verify-clean:" + ",".join(sorted(dimensions))
    return f"V-{compute_hash(key)[:8]}"


_ARTIFACT_TURN_REPLY = "ready"


@lru_cache(maxsize=32)
def _mode_behaviors(mode: str | None) -> str:
    if not mode:
        return ""
    from lionagi.casts import Mode

    return Mode.load(mode).behaviors or ""


def _artifact_turn_instruction(artifact: str) -> str:
    return (
        "The artifact below is under review. Your review task follows in the next "
        f"message. For now reply with the single word {_ARTIFACT_TURN_REPLY}: do not "
        "review it yet and do not emit anything.\n\n"
        f"# Artifact\n{artifact}"
    )


def _dimension_task(dimension: str, mode: str | None = None) -> str:
    """The per-dimension text; it follows the artifact, so dimensions share everything before it."""
    task = (
        f"Review the artifact above for **{dimension}** only. For each concrete "
        "problem, emit an issue_found with: dimension, description, severity "
        "(critical|major|minor), location, confidence (0-1). If you find no "
        f"concrete problem, emit a dimension_clean with dimension='{dimension}' "
        "and a one-sentence rationale — never finish without emitting. Do not "
        "comment on other dimensions; do not pad with praise."
    )
    behaviors = _mode_behaviors(mode)
    return f"{task}\n\n# Approach\n{behaviors}" if behaviors else task


def _dimension_instruction(artifact: str, dimension: str, mode: str | None = None) -> str:
    """The artifact and the dimension's task in one message, the artifact first."""
    return f"# Artifact\n{artifact}\n\n{_dimension_task(dimension, mode)}"


def _is_artifact_turn_reply(text: Any) -> bool:
    return (
        isinstance(text, str)
        and text.strip().rstrip(".!").strip().casefold() == _ARTIFACT_TURN_REPLY
    )


async def _seed_artifact_turn(agent: Any, messages: tuple[Any, Any]) -> None:
    """Start *agent*'s history with copies of the artifact turn, so its request reads that entry."""
    instruction, reply = messages
    await agent.msgs.a_add_message(instruction=instruction.clone())
    await agent.msgs.a_add_message(assistant_response=reply.clone())


def _verify_instruction(issue: IssueFound, ref: str) -> str:
    return (
        "Adversarially verify this review issue — try to REFUTE it with the "
        "strongest counter-argument. Emit a verify_result with issue (the claim "
        f"being verified), ref='{ref}' exactly as given, holds (true only "
        "if it survives refutation) and rationale.\n\n"
        f"- ref: {ref}\n- dimension: {issue.dimension}\n- severity: {issue.severity}\n"
        f"- location: {issue.location}\n- claim: {issue.description}"
    )


def _verify_clean_instruction(
    artifact: str, clean: list[DimensionClean], ref: str, *, can_execute: bool = True
) -> str:
    """The clean-verdict audit prompt. ``can_execute`` is whether the auditing leg can run a command.

    A leg that cannot (an API leg, which runs no tool calls) is told so instead of being
    asked to run a check: asked to execute and name the command it ran, it writes tool-call
    markup as its reply, which is no emission and costs a repair round.
    """
    claims = "\n".join(f"- {c.dimension}: {c.rationale or 'affirmed clean'}" for c in clean) or (
        "- (no affirmative clean events on record; the review surfaced no "
        "issues severe enough to verify)"
    )
    if can_execute:
        check = (
            "You MUST execute at least one concrete check against the artifact "
            "before answering: resolve one central claim or citation the artifact "
            "makes, or run the check it claims passes. Your rationale MUST name "
            "the exact command you ran or the specific claim you resolved and "
            "what you observed — a rationale naming no executed check is invalid "
            "and will be rejected. "
        )
    else:
        check = (
            "You have no tools in this turn: do not write tool calls, shell "
            "commands or function-call markup, and answer from the artifact text "
            "below. You MUST resolve at least one central claim or citation the "
            "artifact makes against the rest of the artifact. Your rationale MUST "
            "name the specific claim you resolved and what the artifact shows — a "
            "rationale naming no resolved claim is invalid and will be rejected. "
        )
    did = (
        "what you executed, what you observed"
        if can_execute
        else "what you resolved, what you found"
    )
    return (
        "Adversarially audit this review's CLEAN verdict — try to REFUTE it. "
        f"{check}Emit a verify_result with issue='CLEAN: review "
        f"affirmed no blocking issues', ref='{ref}' exactly as given, holds "
        "(true only if the clean verdict survives your strongest refutation) "
        f"and rationale ({did}, why the clean "
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
        stage_max_tokens: dict[str, int] | None = None,
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
        # Per-stage output-token limit, over the engine-wide ``max_tokens``. Only a stage this
        # engine builds agents for is accepted: a name it never reads would be set and ignored.
        self.stage_max_tokens = {
            stage: check_max_tokens(f"stage_max_tokens[{stage!r}]", limit)
            for stage, limit in (stage_max_tokens or {}).items()
        }
        unknown = sorted(set(self.stage_max_tokens) - set(_MAX_TOKENS_STAGES))
        if unknown:
            raise ConfigurationError(
                f"stage_max_tokens names {unknown}, which ReviewEngine builds no agent for; "
                f"its stages are {list(_MAX_TOKENS_STAGES)}"
            )

    def max_tokens_for(self, stage: str) -> int | None:
        return self.stage_max_tokens.get(stage) or self.max_tokens

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
        if isinstance(run, ReviewRun) and self.prompt_caching and len(dims) > 1:
            run._artifact_turn = _ArtifactTurn()

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
        if self._all_dimensions_failed(run, dims):
            run.notify(
                "verdict_withheld",
                reason="all_dimensions_failed",
                dimensions=list(dims),
            )
            return (
                "No review verdict was emitted: every configured dimension "
                "failed before producing review evidence."
            )
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
            if not await _is_all_isolated_failure_after_probe(exc):
                raise
            error_type = _failure_label(exc)
            if isinstance(run, ReviewRun):
                run._dimension_failures[dimension] = error_type
            run.notify(
                "dimension_failed",
                dimension=dimension,
                error_type=error_type,
            )
            run.record_emission_failure(f"review-{dimension} ({error_type})")

    async def _isolate_verification_failure(
        self, run: EngineRun, exc: BaseException, *, stage: str
    ) -> bool:
        """Record an isolated verification failure; False means the caller must re-raise."""
        if not await _is_all_isolated_failure_after_probe(exc):
            return False
        error_type = _failure_label(exc)
        run.notify("verification_failed", stage=stage, error_type=error_type)
        run.record_emission_failure(f"{stage} ({error_type})")
        return True

    async def _verify_isolated(self, run: EngineRun, issue: IssueFound) -> None:
        try:
            await self._verify(run, issue)
        except Exception as exc:
            # Spawned into the run's background set, so an escape does not fail
            # this verifier alone: the drain collects it and re-raises, which
            # discards a review whose dimensions have already succeeded.
            if not await self._isolate_verification_failure(
                run, exc, stage=f"verify-{issue.dimension}"
            ):
                raise

    async def _verify_clean_isolated(
        self, run: EngineRun, artifact: str, dimensions: tuple[str, ...]
    ) -> None:
        try:
            await self._verify_clean(run, artifact, dimensions)
        except Exception as exc:
            if not await self._isolate_verification_failure(run, exc, stage="verify-clean"):
                raise

    async def _review_dimension(self, run: EngineRun, artifact: str, dimension: str) -> None:
        emits = (IssueFound, DimensionClean)
        turn: _ArtifactTurn | None = getattr(run, "_artifact_turn", None)
        leading = turn is not None and turn.claim()
        try:
            if turn is not None and not leading:
                # Waited for outside the semaphore: the leader holds a slot while its
                # turn is in flight, and waiters holding the others could starve it.
                await turn.ready.wait()
            await self._review_dimension_turn(run, artifact, dimension, emits, turn, leading)
        finally:
            # However the leader ends, nobody is left waiting for its turn.
            if leading and not turn.ready.is_set():
                turn.share(None)

    async def _review_dimension_turn(
        self,
        run: EngineRun,
        artifact: str,
        dimension: str,
        emits: tuple[type, ...],
        turn: _ArtifactTurn | None,
        leading: bool,
    ) -> None:
        # The mode is part of the dimension's task, not of the system prompt, so
        # every dimension's agent has the same system prompt and their requests can
        # share a cached prefix.
        mode = _DIM_MODE.get(dimension)
        async with run._sem:
            agent = await run.make_agent(
                self.reviewer_role,
                name=f"review-{dimension}",
                model=self.model_for("review"),
                max_tokens=self.max_tokens_for("review"),
                emits=emits,
            )
            # The artifact turn this dimension's history starts with, if any.
            held: tuple[Any, Any] | None = None
            if leading:
                held = await self._send_artifact_turn(run, agent, artifact)
                turn.share(held)
            elif turn is not None and turn.messages is not None:
                held = turn.messages
                await _seed_artifact_turn(agent, held)
            # `models["review_fallback"]` names ONE more attempt on a different model
            # for a dimension whose reviewer stayed silent through its repairs. Read
            # with optional_model_for(), not model_for(): that falls back to the
            # engine's base model, which would turn "unset" into a retry on the same model.
            fallback_model = self.optional_model_for("review_fallback")

            async def _fallback_agent() -> Any:
                # A fresh leg: repair replays the whole conversation onto the same
                # branch, so asking that branch again only grows its context.
                leg = await run.make_agent(
                    self.reviewer_role,
                    name=f"review-{dimension}-fallback",
                    model=fallback_model,
                    max_tokens=self.max_tokens_for("review_fallback"),
                    emits=emits,
                )
                # The fallback is handed the first leg's instruction, which names
                # the artifact only when no artifact turn precedes it.
                if held is not None:
                    await _seed_artifact_turn(leg, held)
                return leg

            if held is not None:
                instruction = _dimension_task(dimension, mode)
            else:
                instruction = _dimension_instruction(artifact, dimension, mode)
            # Repair re-prompts a reviewer that emitted prose instead of a
            # fenced emission. A clean dimension arrives as an affirmative
            # dimension_clean, so reaching the repair path means transport
            # failed — not that the dimension was clean.
            await run.operate_with_repair(
                agent,
                instruction,
                arrived=lambda: dimension in self._reported_dimensions(run),
                emits=emits,
                retries=self.repair_retries,
                fallback=_fallback_agent if fallback_model else None,
                fallback_model=fallback_model,
            )

    async def _send_artifact_turn(
        self, run: EngineRun, agent: Any, artifact: str
    ) -> tuple[Any, Any] | None:
        """Send the artifact alone on *agent*; the turn the other dimensions start from, or None.

        None when the agent's endpoint places no prompt cache (nothing to share), when
        the request fails, or when the reply is anything but the requested word: a
        reply that reviewed or emitted is not copied into other agents' histories.
        Each dimension then sends the artifact with its own task, the leader too, at
        the cost of the artifact twice in its context when its turn did complete.

        A failure here never decides the review: the turn only saves cost, and each
        dimension's own request meets whatever caused it under the stage's usual
        isolation rules. Cancellation is not caught.
        """
        endpoint = getattr(getattr(agent, "chat_model", None), "endpoint", None)
        if not getattr(endpoint, "places_prompt_cache", False):
            return None
        name = getattr(agent, "name", "") or ""
        try:
            await agent.operate(instruction=_artifact_turn_instruction(artifact))
        except Exception as exc:
            run.notify("artifact_turn_failed", agent=name, error_type=_failure_label(exc))
            return None
        instruction = agent.msgs.last_instruction
        reply = agent.msgs.last_response
        if instruction is None or reply is None or not _is_artifact_turn_reply(reply.response):
            run.notify("artifact_turn_unused", agent=name, reason="unexpected_reply")
            return None
        return instruction, reply

    async def _verify(self, run: EngineRun, issue: IssueFound) -> None:
        emits = (VerifyResult,)
        ref = _verify_ref(issue)
        async with run._sem:
            verifier = await run.make_agent(
                self.verifier_role,
                name=f"verify-{issue.dimension}",
                modes=["adversarial"],
                model=self.model_for("verify"),
                max_tokens=self.max_tokens_for("verify"),
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
        # ARRIVAL IS "A VERIFY RESULT LANDED", NOT "THE MODEL ECHOED OUR TOKEN".
        #
        # `VerifyResult.ref` defaults to "", so an audit that emits everything
        # else and drops the echo still validates and lands in the run. Keyed on
        # the echo, such an audit was invisible to the arrival check: the repair
        # pass re-prompted, the second emission dropped the echo the same way,
        # and the stage recorded two missing emissions for a dimension that had
        # reported twice, which then blocked the verdict.
        #
        # The token was never load-bearing for ARRIVAL. `_run` reaches this stage
        # only when the run holds ZERO VerifyResult (see the gate above), so
        # anything that lands during it is this audit and nothing else, which is
        # exactly as specific as the ref and cannot be defeated by a dropped echo.
        # The baseline is taken anyway rather than asserting the gate from in here:
        # a future caller that relaxes the gate gets correct behaviour instead of a
        # stale comment.
        before = len(run.by_type(VerifyResult))

        def _fresh() -> list:
            return run.by_type(VerifyResult)[before:]

        async with run._sem:
            verifier = await run.make_agent(
                self.verifier_role,
                name="verify-clean",
                modes=["adversarial"],
                model=self.model_for("verify"),
                max_tokens=self.max_tokens_for("verify"),
                emits=emits,
            )
            await run.operate_with_repair(
                verifier,
                _verify_clean_instruction(
                    artifact,
                    clean,
                    ref,
                    can_execute=bool(
                        getattr(getattr(verifier, "chat_model", None), "is_cli", False)
                    ),
                ),
                arrived=lambda: bool(_fresh()),
                emits=emits,
                retries=self.repair_retries,
            )
            # THE TOKEN IS STILL LOAD-BEARING DOWNSTREAM, so stamp it here rather
            # than teaching every reader to live without it: `_verdict_instruction`
            # partitions clean audits from issue verifications by `v.ref == clean
            # ref` alone, so a ref-less audit would otherwise be presented to the
            # synthesis agent as an adversarial verification OF AN ISSUE, and
            # `_verify_execution_record` would read the clean audit as absent. The
            # stamp is on the stored event (`by_type` returns the payload itself),
            # so every later read agrees.
            #
            # The mismatch is recorded, not silently repaired: if the echo starts
            # failing for a NEW reason -- a renamed field, a model that invents its
            # own token -- the count of these lines is the signal, and without it
            # the fix would hide the next defect as effectively as it fixes this one.
            for v in _fresh():
                if v.ref != ref:
                    run.notify(
                        "verify_clean_ref_stamped",
                        agent="verify-clean",
                        expected=ref,
                        got=v.ref,
                    )
                    v.ref = ref

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
            max_tokens=self.max_tokens_for("verdict"),
            emits=(ProposedVerdict,),
            exempt=True,
        )
        from lionagi.service import cache_intent

        # The synthesis branch is made for this one request, so nothing would read an entry
        # written at the end of its turn; the shared system and tool prefix keeps its mark.
        with cache_intent("final"):
            res = await synth.operate(
                instruction=_verdict_instruction(artifact, dimensions, issues, verifications, clean)
            )
        text = str(res) if res is not None else ""
        # The provider's own report on the synthesis model. The model-level value is the
        # last call's, not call-correlated; it is this call's because this branch and its
        # model were made for the synthesis alone and call in sequence, and a call that
        # reports no model clears it.
        metadata = getattr(getattr(synth, "chat_model", None), "provider_metadata", None)
        served = metadata.get("served_model") if isinstance(metadata, dict) else None
        if not isinstance(served, str) or not served.strip():
            served = None

        unevidenced = self._unevidenced_dimensions(run, dimensions)
        unverified = self._unverified_findings(run)
        # The condition that spawned the audit, re-asked now: still true = it produced nothing.
        unaudited = bool(self.verify_clean) and not verifications
        proposals = run.by_type(ProposedVerdict)
        proposed = proposals[-1] if proposals else None
        final = self._rule(proposed, unevidenced, unverified, text, unaudited=unaudited)
        final.served_model = served
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

    def _all_dimensions_failed(self, run: EngineRun, dimensions: tuple[str, ...]) -> bool:
        """True when isolation recorded every reviewer and none produced evidence."""
        failures = getattr(run, "_dimension_failures", {})
        return (
            bool(dimensions)
            and all(dimension in failures for dimension in dimensions)
            and not self._reported_dimensions(run)
        )

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
