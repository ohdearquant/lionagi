# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import importlib.util
from types import SimpleNamespace

import anyio
import pytest

from lionagi.engines.engine import Engine, EngineBudgetError
from lionagi.engines.review import (
    DimensionClean,
    IssueFound,
    ReviewEngine,
    ReviewVerdict,
    _is_all_isolated_failure,
)
from lionagi.ln.concurrency._compat import ExceptionGroup
from lionagi.providers._provider_errors import ProviderContextError


class _StubEngine(Engine):
    async def _run(self, run, *args, **kwargs):  # pragma: no cover
        return ""


class _NearLimitBranch:
    name = "near-limit"
    chat_model = SimpleNamespace(is_cli=False)
    token_budget = SimpleNamespace(
        is_critical=True,
        used=95_000,
        limit=100_000,
        usage_pct=0.95,
    )

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def operate(self, *, instruction: str) -> str:
        self.calls.append(instruction)
        return "prose without an emission"


@pytest.mark.asyncio
async def test_critical_context_skips_repair_that_can_only_overflow() -> None:
    events: list[dict] = []
    run = _StubEngine().new_run(on_event=events.append)
    branch = _NearLimitBranch()

    await run.operate_with_repair(
        branch,
        "initial instruction",
        arrived=lambda: False,
        retries=1,
    )

    assert branch.calls == ["initial instruction"]
    assert any(
        event["type"] == "emission_repair_skipped" and event["reason"] == "context_critical"
        for event in events
    )
    assert run._emission_failures == ["near-limit x1"]


class _PartiallyFailingReview(ReviewEngine):
    def __init__(self) -> None:
        super().__init__(
            dimensions=("broken", "healthy"),
            verify_clean=False,
        )
        self.healthy_started = asyncio.Event()
        self.healthy_finished = asyncio.Event()

    async def _review_dimension(self, run, artifact: str, dimension: str) -> None:
        if dimension == "broken":
            await self.healthy_started.wait()
            raise ProviderContextError("provider context overflow")
        self.healthy_started.set()
        await asyncio.sleep(0.05)
        self.healthy_finished.set()

    async def _verdict(self, run, artifact: str, dimensions: tuple[str, ...], failed=None) -> str:
        assert self.healthy_finished.is_set()
        self.failed_seen = list(failed or [])
        return "healthy dimension survived"


@pytest.mark.asyncio
async def test_review_dimension_failure_does_not_cancel_siblings_or_verdict() -> None:
    events: list[dict] = []
    engine = _PartiallyFailingReview()

    result = await engine.run("artifact", on_event=events.append)

    assert result == "healthy dimension survived"
    assert result.degraded is True
    assert result.skipped == ["review-broken (ProviderContextError)"]
    assert any(
        event["type"] == "dimension_failed"
        and event["dimension"] == "broken"
        and event["error_type"] == "ProviderContextError"
        for event in events
    )


class _TransportFailingReview(ReviewEngine):
    """One dimension dies of `failure`; the sibling must still finish and reach a verdict."""

    def __init__(self, failure: BaseException) -> None:
        super().__init__(dimensions=("broken", "healthy"), verify_clean=False)
        self._failure = failure
        self.healthy_started = asyncio.Event()
        self.healthy_finished = asyncio.Event()

    async def _review_dimension(self, run, artifact: str, dimension: str) -> None:
        if dimension == "broken":
            await self.healthy_started.wait()
            raise self._failure
        self.healthy_started.set()
        await asyncio.sleep(0.05)
        self.healthy_finished.set()

    async def _verdict(self, run, artifact: str, dimensions: tuple[str, ...], failed=None) -> str:
        assert self.healthy_finished.is_set()
        self.failed_seen = list(failed or [])
        return "healthy dimension survived"


def _mcp_error(message: str) -> BaseException:
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    return McpError(ErrorData(code=-32000, message=message))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make_failure", "expected_label"),
    [
        (lambda: anyio.ClosedResourceError(), "ClosedResourceError"),
        (lambda: anyio.BrokenResourceError(), "BrokenResourceError"),
        # The MCP SDK reads replies with `await response_stream_reader.receive()`,
        # which raises EndOfStream (not ClosedResourceError) once the peer closes.
        (lambda: anyio.EndOfStream(), "EndOfStream"),
        (
            lambda: ExceptionGroup(
                "unhandled errors in a TaskGroup",
                [anyio.ClosedResourceError(), anyio.ClosedResourceError()],
            ),
            "ClosedResourceError",
        ),
        (
            pytest.param(
                lambda: _mcp_error("Connection closed"),
                "McpError",
                marks=pytest.mark.skipif(
                    importlib.util.find_spec("mcp") is None,
                    reason="mcp is an optional extra",
                ),
            )
        ),
    ],
)
async def test_review_isolates_transport_failures_like_provider_failures(
    make_failure, expected_label: str
) -> None:
    """A dropped transport is a per-dimension failure, so it must degrade one dimension, not kill the run.

    Before this, only ProviderError was isolated. A dropped MCP connection
    raises the MCP SDK's McpError (an Exception, not a ProviderError) and a
    dropped stream raises anyio's, so both escaped the isolation clause and
    propagated to the run-level handler that cancels every sibling — turning
    one dead dimension into ENGINE-UNAVAILABLE with no verdict at all.
    """
    events: list[dict] = []
    engine = _TransportFailingReview(make_failure())

    result = await engine.run("artifact", on_event=events.append)

    assert result == "healthy dimension survived"
    assert result.degraded is True
    assert result.skipped == [f"review-broken ({expected_label})"]
    assert any(
        event["type"] == "dimension_failed"
        and event["dimension"] == "broken"
        and event["error_type"] == expected_label
        for event in events
    )


def test_isolation_predicate_refuses_a_group_carrying_a_non_transport_leaf() -> None:
    """Isolate only when EVERY leaf is a transport/provider failure.

    A group mixing a transport drop with budget exhaustion must propagate:
    swallowing it would launder a run-wide stop into a per-dimension degrade
    and hide it behind a verdict that looks reasoned.
    """
    all_transport = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [anyio.ClosedResourceError(), anyio.BrokenResourceError()],
    )
    assert _is_all_isolated_failure(all_transport) is True

    mixed = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [anyio.ClosedResourceError(), EngineBudgetError("agent budget exhausted (12/12)")],
    )
    assert _is_all_isolated_failure(mixed) is False

    nested_mixed = ExceptionGroup(
        "outer",
        [ExceptionGroup("inner", [anyio.ClosedResourceError(), EngineBudgetError("exhausted")])],
    )
    assert _is_all_isolated_failure(nested_mixed) is False


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="mcp is an optional extra")
def test_application_mcp_errors_are_not_isolated_as_transport_failures() -> None:
    """Only connection-shaped McpErrors are per-dimension transport failures.

    The SDK spells just two conditions as McpError itself: connection closed
    and request timeout. Every other McpError relays a server-side error —
    an authorization refusal, an application failure — which describes the
    request, not the wire. Swallowing those as transport drops turns e.g. a
    permission denial into a silent one-dimension degrade.
    """
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    from lionagi.engines.review import _is_all_isolated_failure

    connection_closed = McpError(ErrorData(code=-32000, message="Connection closed"))
    assert _is_all_isolated_failure(connection_closed) is True

    permission_denied = McpError(ErrorData(code=-32603, message="permission denied"))
    assert _is_all_isolated_failure(permission_denied) is False

    mixed = ExceptionGroup(
        "unhandled errors in a TaskGroup", [connection_closed, permission_denied]
    )
    assert _is_all_isolated_failure(mixed) is False


async def test_missing_coverage_grants_no_agent_the_ability_to_issue_a_verdict() -> None:
    """The structural invariant, asserted at the grant rather than the outcome.

    Rewriting an approval after synthesis emits it can never be airtight: the
    session bus gathers handlers concurrently, so a subscriber registered
    before the run already holds the APPROVE by the time any rewrite runs. The
    engine therefore does not create a ReviewVerdict-emitting agent at all when
    a dimension is missing. Asserting on the grant is what makes this checkable
    — an approval that is never constructed cannot leak through any channel.
    """
    engine = ReviewEngine()
    run = _CoverageRun(clean=("correctness",))

    await engine._verdict(run, "artifact.py", ("correctness", "security"), [])

    granted = [kwargs.get("emits", ()) for _role, kwargs in run.agent_calls]
    assert not any(ReviewVerdict in emits for emits in granted), (
        f"an agent was granted the ability to emit a ReviewVerdict over missing "
        f"coverage; grants were {granted!r}"
    )
    # Not merely ungranted: no synthesis agent is created at all, so the
    # degraded path also costs no model call.
    assert run.agent_calls == []


async def test_the_verdict_a_gap_run_emits_is_engine_authored_and_refuses() -> None:
    """Downstream readers take the verdict off the bus, not from the returned
    string, so the run has to put a real REQUEST-CHANGES event there. It names
    each dimension that did not run as a blocking entry."""
    engine = ReviewEngine()
    run = _CoverageRun(clean=("correctness",))

    await engine._verdict(
        run, "artifact.py", ("correctness", "security"), [("security", "McpError")]
    )

    emitted = [e for e in run.emitted if isinstance(e, ReviewVerdict)]
    assert len(emitted) == 1, f"expected exactly one engine-authored verdict, got {emitted!r}"
    assert emitted[0].verdict == "REQUEST-CHANGES"
    assert "security dimension did not run (McpError)" in emitted[0].blocking
    assert "correctness" not in " ".join(emitted[0].blocking)
    assert emitted[0].reversible_by and "Re-run" in emitted[0].reversible_by
    assert any(kind == "verdict_withheld" for kind, _ in run.notices)


async def test_a_gap_run_still_reports_what_the_surviving_dimensions_found() -> None:
    """Skipping synthesis must not throw away the actionable half of a degraded
    review. The issues the dimensions that did run found are rendered by the
    engine, deterministically and at no model cost."""
    engine = ReviewEngine()
    issue = IssueFound(
        dimension="correctness",
        description="off-by-one in the retry bound",
        severity="major",
    )
    run = _CoverageRun(issues=(issue,))

    result = await engine._verdict(run, "artifact.py", ("correctness", "security"), [])

    assert result.startswith("DECISION: REQUEST-CHANGES")
    assert "security dimension did not run" in result
    assert "off-by-one in the retry bound" in result
    assert "[correctness]" in result


async def test_complete_coverage_still_grants_synthesis_and_returns_its_text() -> None:
    """The must-not-over-fire arm. With every dimension covered there is no gap,
    so synthesis is granted the verdict capability as before and its output is
    returned untouched. Without this arm the refusal path would pass even if it
    fired on every run."""
    engine = ReviewEngine()
    run = _CoverageRun(clean=("correctness", "security"))

    result = await engine._verdict(run, "artifact.py", ("correctness", "security"), [])

    granted = [kwargs.get("emits", ()) for _role, kwargs in run.agent_calls]
    assert any(ReviewVerdict in emits for emits in granted), (
        "synthesis was not granted the verdict capability on a fully covered run"
    )
    assert result == "DECISION: APPROVE"
    assert not any(kind == "verdict_withheld" for kind, _ in run.notices)


def test_verdict_prompt_refuses_to_present_a_dead_dimension_as_reviewed() -> None:
    """A dimension whose reviewer died must not be listed as reviewed.

    Isolating a transport failure keeps the run alive, but a dead reviewer
    emits no issues, and an unqualified "Dimensions reviewed: ... security ..."
    tells synthesis that security WAS reviewed and found nothing. That is a
    fail-open review gate: the strongest evidence for approval becomes the
    silence of a dimension that never executed.
    """
    from lionagi.engines.review import _verdict_instruction

    prompt = _verdict_instruction(
        "artifact",
        ("correctness", "security"),
        issues=[],
        verifications=[],
        clean=["correctness"],
        failed=[("security", "McpError")],
    )

    reviewed_line = next(
        line for line in prompt.splitlines() if line.startswith("Dimensions reviewed:")
    )
    assert "security" not in reviewed_line
    assert "correctness" in reviewed_line

    assert "DID NOT RUN: security (McpError)" in prompt
    assert "not evidence" in prompt
    # The instruction must actively steer away from approving on absent
    # coverage, not merely mention that something failed.
    assert "Do not issue APPROVE" in prompt


def test_verdict_prompt_is_unchanged_when_every_dimension_ran() -> None:
    """The all-healthy prompt keeps its previous shape: no empty warning block."""
    from lionagi.engines.review import _verdict_instruction

    prompt = _verdict_instruction(
        "artifact",
        ("correctness", "security"),
        issues=[],
        verifications=[],
        clean=["correctness", "security"],
    )

    assert "Dimensions reviewed: correctness, security" in prompt
    assert "DID NOT RUN" not in prompt


def test_a_broken_mcp_install_is_not_silently_treated_as_absent(monkeypatch) -> None:
    """An mcp that fails to import must raise, not disable isolation quietly.

    Reporting a broken install as "extra not present" would silently drop
    McpError out of the isolated set, and the first symptom would be a run
    dying with no verdict, far from the cause.
    """
    import builtins

    from lionagi.engines import review as review_mod

    review_mod._mcp_error_type.cache_clear()
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mcp.shared.exceptions":
            # mcp itself is importable; one of ITS dependencies is missing.
            raise ModuleNotFoundError("No module named 'pydantic_core'", name="pydantic_core")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ModuleNotFoundError):
        review_mod._mcp_error_type()
    review_mod._mcp_error_type.cache_clear()

    # The subtler arm: the top-level package resolved but the submodule itself
    # is missing. exc.name is then "mcp.shared.exceptions", which a prefix
    # check reads as "mcp is absent" — it is not, the install is broken.
    def fake_import_submodule(name, *args, **kwargs):
        if name == "mcp.shared.exceptions":
            raise ModuleNotFoundError(
                "No module named 'mcp.shared.exceptions'", name="mcp.shared.exceptions"
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import_submodule)
    with pytest.raises(ModuleNotFoundError):
        review_mod._mcp_error_type()
    review_mod._mcp_error_type.cache_clear()


def test_a_missing_mcp_extra_is_a_normal_configuration(monkeypatch) -> None:
    """The other arm: mcp genuinely absent yields None rather than raising."""
    import builtins

    from lionagi.engines import review as review_mod

    review_mod._mcp_error_type.cache_clear()
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "mcp.shared.exceptions":
            raise ModuleNotFoundError("No module named 'mcp'", name="mcp")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert review_mod._mcp_error_type() is None
    review_mod._mcp_error_type.cache_clear()


# -- the approval cap has to hold on every channel a verdict can leave by ------


class _CoverageRun:
    """Minimal run exposing the surface the verdict paths read.

    *clean* / *issues* / *verdicts* seed both this run's coverage ledger and the
    session flow, which is what a real run produces. The ``stale_*`` arguments
    seed the session flow **only**, standing in for a previous run on a reused
    session: ``run.by_type()`` reads the session's accumulated flow, so anything
    that derives this run's coverage from it counts a previous run's evidence as
    this one's.
    """

    def __init__(
        self,
        *,
        verdicts=None,
        clean=(),
        issues=(),
        stale_verdicts=None,
        stale_clean=(),
        stale_issues=(),
    ) -> None:
        from lionagi.engines.review import _RunCoverage

        # The stale events are in the flow first, exactly as a previous run on a
        # reused session would have left them.
        self._by_type = {
            ReviewVerdict: list(stale_verdicts or []),
            DimensionClean: [DimensionClean(dimension=d) for d in stale_clean],
            IssueFound: list(stale_issues),
        }
        # Mark the run boundary here, the way _run does, then add this run's own
        # events after it.
        self._review_coverage = _RunCoverage(self)
        self._by_type[ReviewVerdict].extend(verdicts or [])
        self._by_type[DimensionClean].extend(DimensionClean(dimension=d) for d in clean)
        self._by_type[IssueFound].extend(issues)

        self.notices: list[tuple[str, dict]] = []
        self.emitted: list = []
        self.agent_calls: list[tuple[str, dict]] = []
        self.agents_made = 3

    def by_type(self, event_type):
        return self._by_type.get(event_type, [])

    def notify(self, kind: str, **data) -> None:
        self.notices.append((kind, data))

    async def emit(self, event):
        self.emitted.append(event)
        return []

    async def make_agent(self, role: str, **kwargs):
        self.agent_calls.append((role, kwargs))

        class _Approve:
            async def operate(self, instruction: str):
                return "DECISION: APPROVE"

        return _Approve()


async def test_a_dimension_that_emitted_nothing_cannot_be_approved_over() -> None:
    """A reviewer that exhausts its emission repairs raises nothing.

    So it never reaches the isolated-failure recorder and the failed list stays
    empty. If coverage is read from that list alone, a dimension that produced
    no output at all is indistinguishable from one that came back clean, and
    the run approves over it.
    """
    engine = ReviewEngine()
    run = _CoverageRun(clean=("correctness",))

    result = await engine._verdict(run, "artifact.py", ("correctness", "security"), [])

    assert result.startswith("DECISION: REQUEST-CHANGES")
    assert "security dimension did not run (no evidence emitted)" in result
    # The dimension that actually reported is not accused of silence.
    assert "correctness dimension did not run" not in result


async def test_a_previous_runs_evidence_on_a_reused_session_is_not_this_runs_coverage() -> None:
    """``run.by_type()`` reads the *session's* accumulated flow, and a session
    outlives a run: ``EngineRun`` takes a caller-supplied ``Session`` and
    ``Engine.run()`` passes one through. Deriving coverage from there lets a
    dimension that emitted nothing this run pass as covered because it emitted
    something last run, which is the exact failure the coverage check exists to
    catch. Coverage must come from observers this run registered."""
    engine = ReviewEngine()
    stale = IssueFound(
        dimension="security", description="found on a previous run", severity="major"
    )
    # The session flow says security produced evidence. This run's ledger says
    # it did not, and this run's ledger is the one that counts.
    run = _CoverageRun(clean=("correctness",), stale_issues=(stale,), stale_clean=("security",))

    result = await engine._verdict(run, "artifact.py", ("correctness", "security"), [])

    assert "security dimension did not run (no evidence emitted)" in result, (
        "a previous run's evidence was counted as this run's coverage"
    )
    assert result.startswith("DECISION: REQUEST-CHANGES")


async def test_partial_export_returns_this_runs_verdict_not_a_previous_runs() -> None:
    """Exhaustion is the one exit that never reaches ``_verdict``.

    Reading the stored verdict off the session flow means that on a reused
    session an exhausted run exports the *previous* run's verdict as its own
    result, over an artifact it may never have finished reviewing.
    """
    from lionagi.engines.review import ReviewVerdict

    previous = ReviewVerdict(verdict="APPROVE", rationale="a previous run", blocking=[])
    engine = ReviewEngine()
    run = _CoverageRun(stale_verdicts=[previous], clean=("correctness", "security"))

    out = await engine._partial_export(run, "artifact.py", dimensions=("correctness", "security"))

    assert out == "", f"exported a verdict this run never issued: {out!r}"
    assert previous.verdict == "APPROVE", "a previous run's stored verdict was mutated"


async def test_partial_export_still_returns_a_verdict_this_run_did_issue() -> None:
    """The must-not-over-fire arm for the exit above: a verdict this run issued
    is still exported, with the budget-exhaustion header. No coverage cap is
    applied, and none is needed, because a ReviewVerdict exists for this run
    only where synthesis was granted the capability to emit one."""
    from lionagi.engines.review import ReviewVerdict

    mine = ReviewVerdict(verdict="APPROVE", rationale="every dimension reported", blocking=[])
    engine = ReviewEngine()
    run = _CoverageRun(verdicts=[mine], clean=("correctness", "security"))

    out = await engine._partial_export(run, "artifact.py", dimensions=("correctness", "security"))

    assert "budget_exhausted" in out
    assert "APPROVE: every dimension reported" in out
    assert any(kind == "verdict_emitted_on_exhaustion" for kind, _ in run.notices)


async def test_run_marks_the_boundary_that_makes_coverage_run_scoped() -> None:
    """Scoping is only real if ``_run`` actually records where the run begins.

    Without this, every coverage test above would still pass against a reader
    whose prior set is empty, which is the permissive default — it treats a
    previous run's evidence as this run's, the exact defect being fixed.
    """

    class _ObservingRun(_CoverageRun):
        root = ""
        _sem = asyncio.Semaphore(1)

        def observe(self, event_type, handler):
            return None

        async def wait_quiescence(self):
            return None

        async def cancel_active(self):
            return None

        async def operate_with_repair(self, agent, instruction, **kwargs):
            return ""

    stale = IssueFound(dimension="security", description="previous run", severity="major")
    engine = ReviewEngine(verify_clean=False)
    run = _ObservingRun(stale_issues=(stale,))
    # Drop the reader the stub pre-built, so _run is the thing that marks the
    # boundary rather than the fixture.
    run._review_coverage = None

    await engine._run(run, "artifact.py", dimensions=("correctness", "security"))

    reader = engine._coverage(run)
    assert reader.issued(run) == set(), (
        "a previous run's issue was counted as this run's evidence, so _run did "
        "not mark the boundary"
    )

    # Evidence arriving after the mark is this run's.
    mine = IssueFound(dimension="correctness", description="this run", severity="minor")
    run._by_type[IssueFound].append(mine)
    assert reader.issued(run) == {"correctness"}
    assert reader.missing(run, ("correctness", "security"), []) == [
        ("security", "no evidence emitted")
    ]
