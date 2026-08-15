# Copyright (c) 2023-2026, HaiyangLi <quantocean.li at gmail dot com>
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import importlib.util
from types import SimpleNamespace

import anyio
import pytest

from lionagi.engines.engine import Engine, EngineBudgetError
from lionagi.engines.review import ReviewEngine, _is_all_isolated_failure
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


def test_an_approve_emitted_over_a_dead_dimension_is_structurally_capped() -> None:
    """The prompt tells synthesis not to approve on missing coverage; the
    engine must not depend on it complying. If a dimension never ran and the
    synthesis model emits APPROVE anyway, the verdict event is rewritten to
    REQUEST-CHANGES with the dead dimensions as blocking entries — approval
    on absent coverage must be unrepresentable, not merely discouraged."""
    from lionagi.engines.review import ReviewVerdict, _cap_approvals_on_missing_coverage

    approve = ReviewVerdict(verdict="APPROVE", rationale="looks clean", blocking=[])
    with_fixes = ReviewVerdict(verdict="approve-with-fixes", rationale="minor nits", blocking=[])
    already_blocking = ReviewVerdict(
        verdict="REQUEST-CHANGES", rationale="real issue", blocking=["x"]
    )
    failed = [("security", "McpError"), ("performance", "EndOfStream")]

    capped = _cap_approvals_on_missing_coverage([approve, with_fixes, already_blocking], failed)

    assert capped is True
    # Both APPROVE-family verdicts are rewritten, case-insensitively.
    for verdict in (approve, with_fixes):
        assert verdict.verdict == "REQUEST-CHANGES"
        assert "security dimension did not run (McpError)" in verdict.blocking
        assert "performance dimension did not run (EndOfStream)" in verdict.blocking
        assert "cannot be issued" in verdict.rationale
    # A verdict that already refuses approval is left alone.
    assert already_blocking.rationale == "real issue"
    assert already_blocking.blocking == ["x"]

    # No dead dimensions -> nothing to cap; an APPROVE stands.
    clean_approve = ReviewVerdict(verdict="APPROVE", rationale="fine", blocking=[])
    assert _cap_approvals_on_missing_coverage([clean_approve], []) is False
    assert clean_approve.verdict == "APPROVE"


def test_the_returned_verdict_text_is_capped_even_with_no_verdict_to_rewrite() -> None:
    """Rewriting emitted verdicts reaches nothing when synthesis emits none.

    The returned string is then the entire verdict the caller sees, so it
    carries the same refusal; the synthesis output survives as quoted context
    where its own decision line cannot be read as the run's decision.
    """
    from lionagi.engines.review import _cap_verdict_text

    failed = [("security", "McpError")]
    capped = _cap_verdict_text("DECISION: APPROVE\nAll dimensions look clean.", failed)

    assert capped.startswith("DECISION: REQUEST-CHANGES\n")
    assert "- security dimension did not run (McpError)" in capped
    # The synthesis decision line survives only as quoted context.
    assert "> DECISION: APPROVE" in capped
    assert not any(line.startswith("DECISION: APPROVE") for line in capped.splitlines())

    # Nothing failed -> the synthesis text is returned untouched.
    assert _cap_verdict_text("DECISION: APPROVE", []) == "DECISION: APPROVE"


async def test_a_raw_approve_with_no_emitted_verdict_does_not_yield_approval() -> None:
    """End-to-end on _verdict: synthesis emits no ReviewVerdict and returns a
    bare approval. The event cap has nothing to rewrite, so the refusal has to
    come from the returned text or the run approves on a dimension that never
    executed."""

    class _RawApproveAgent:
        async def operate(self, instruction: str):
            return "DECISION: APPROVE"

    class _RunWithNoVerdictEmitted:
        def __init__(self) -> None:
            self.notices: list[tuple[str, dict]] = []

        def by_type(self, event_type):
            return []

        def notify(self, kind: str, **data) -> None:
            self.notices.append((kind, data))

        async def make_agent(self, role: str, **kwargs):
            return _RawApproveAgent()

    engine = ReviewEngine()
    run = _RunWithNoVerdictEmitted()

    result = await engine._verdict(
        run, "artifact.py", ("correctness", "security"), [("security", "McpError")]
    )

    assert not result.startswith("DECISION: APPROVE")
    assert result.startswith("DECISION: REQUEST-CHANGES")
    assert "security dimension did not run (McpError)" in result
    assert any(kind == "verdict_capped" for kind, _ in run.notices)


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
    """Minimal run exposing the surface the cap paths read."""

    def __init__(self, *, verdicts=None, clean=(), issues=()) -> None:
        from lionagi.engines.review import DimensionClean, IssueFound, ReviewVerdict

        self._by_type = {
            ReviewVerdict: list(verdicts or []),
            DimensionClean: [DimensionClean(dimension=d) for d in clean],
            IssueFound: list(issues),
        }
        self.notices: list[tuple[str, dict]] = []
        self.agents_made = 3

    def by_type(self, event_type):
        return self._by_type.get(event_type, [])

    def notify(self, kind: str, **data) -> None:
        self.notices.append((kind, data))

    async def make_agent(self, role: str, **kwargs):
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


async def test_partial_export_does_not_hand_back_an_approval_over_missing_coverage() -> None:
    """Exhaustion is the one exit that never reaches _verdict.

    A run cut short by its deadline is also the likeliest to be missing a
    dimension, so this is the worst channel to let a stored approval out of.
    """
    from lionagi.engines.review import ReviewVerdict

    verdict = ReviewVerdict(verdict="APPROVE", rationale="nothing came up", blocking=[])
    engine = ReviewEngine()
    run = _CoverageRun(verdicts=[verdict], clean=("correctness",))

    out = await engine._partial_export(run, "artifact.py", dimensions=("correctness", "security"))

    assert "REQUEST-CHANGES" in out
    assert not any(line.startswith("DECISION: APPROVE") for line in out.splitlines())
    # The stored verdict is rewritten too, not just the text built from it.
    assert verdict.verdict == "REQUEST-CHANGES"
    assert any("security" in b for b in verdict.blocking)


async def test_a_verdict_is_capped_when_it_arrives_not_after_synthesis_returns() -> None:
    """Persistence and subscribers read the verdict from the run's flow.

    Rewriting it after synthesis returns leaves an approval already delivered
    to whoever was listening, so the cap runs on arrival.
    """
    from lionagi.engines.review import ReviewVerdict

    observers: dict[type, list] = {}

    class _ObservingRun(_CoverageRun):
        root = ""
        _sem = asyncio.Semaphore(1)

        def observe(self, event_type, handler):
            observers.setdefault(event_type, []).append(handler)

        async def wait_quiescence(self):
            return None

        async def cancel_active(self):
            return None

        async def operate_with_repair(self, agent, instruction, **kwargs):
            return ""

    engine = ReviewEngine(verify_clean=False)
    run = _ObservingRun(clean=("correctness",))
    await engine._run(run, "artifact.py", dimensions=("correctness", "security"))

    assert ReviewVerdict in observers, "no verdict observer registered, so nothing caps on arrival"
    arriving = ReviewVerdict(verdict="APPROVE", rationale="looks clean", blocking=[])
    for handler in observers[ReviewVerdict]:
        handler(arriving, None)

    assert arriving.verdict == "REQUEST-CHANGES", (
        "the verdict object handed to subscribers still says APPROVE while a "
        "dimension produced no evidence"
    )
    assert any("security" in b for b in arriving.blocking)
