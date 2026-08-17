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
    _is_all_isolated_failure,
)
from lionagi.ln.concurrency._compat import ExceptionGroup
from lionagi.providers._provider_errors import (
    ProviderAuthError,
    ProviderContextError,
    ProviderQuotaError,
    ProviderSafetyError,
    ProviderUnsupportedModelError,
    WorkerLivenessError,
)


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

    async def _verdict(self, run, artifact: str, dimensions: tuple[str, ...]) -> str:
        assert self.healthy_finished.is_set()
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

    async def _verdict(self, run, artifact: str, dimensions: tuple[str, ...]) -> str:
        assert self.healthy_finished.is_set()
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
    one dead dimension into a run that ends with no verdict at all.
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

    An McpError that relays a server-side error — an authorization refusal, an
    application failure — describes the request, not the wire. Swallowing those
    as transport drops turns e.g. a permission denial into a silent
    one-dimension degrade.
    """
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    connection_closed = McpError(ErrorData(code=-32000, message="Connection closed"))
    assert _is_all_isolated_failure(connection_closed) is True

    permission_denied = McpError(ErrorData(code=-32603, message="permission denied"))
    assert _is_all_isolated_failure(permission_denied) is False

    mixed = ExceptionGroup(
        "unhandled errors in a TaskGroup", [connection_closed, permission_denied]
    )
    assert _is_all_isolated_failure(mixed) is False


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


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="mcp is an optional extra")
@pytest.mark.parametrize(
    ("code", "message"),
    [
        (-32000, "boom: upstream unavailable"),
        (408, "rate limited, retry later"),
    ],
)
def test_a_server_reusing_the_sdks_own_codes_is_not_a_transport_drop(
    code: int, message: str
) -> None:
    """The error code travels in the server's payload, so it cannot decide this alone.

    A server may answer with -32000 or 408 for reasons of its own, and a buggy
    one that reuses either would have every application failure recorded as a
    dropped connection and skipped, leaving a degraded verdict that names the
    wrong cause.
    """
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    assert _is_all_isolated_failure(McpError(ErrorData(code=code, message=message))) is False


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="mcp is an optional extra")
def test_a_server_echoing_the_closed_connection_wording_with_detail_is_not_a_drop() -> None:
    """The wording is the only thing separating the two, and a server can type it.

    What a server cannot do is send it the way the SDK does. The SDK builds the
    reply with a code and a message and nothing else, so a payload carrying
    detail beside them came from a server whatever it says. Both shapes below
    would otherwise be swallowed and the dimension dropped from the verdict.

    The last case is the control: strip the detail and the same payload is the
    drop signal again, which is what keeps this from passing by refusing
    everything.
    """
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    with_data = ErrorData(code=-32000, message="Connection closed", data={"upstream": "vendor-api"})
    with_extra = ErrorData.model_validate(
        {"code": -32000, "message": "Connection closed", "requestId": "req-9"}
    )
    bare = ErrorData(code=-32000, message="Connection closed")

    assert _is_all_isolated_failure(McpError(with_data)) is False
    assert _is_all_isolated_failure(McpError(with_extra)) is False
    assert _is_all_isolated_failure(McpError(bare)) is True


@pytest.mark.skipif(importlib.util.find_spec("mcp") is None, reason="mcp is an optional extra")
@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["connection_closed", "read_timeout"])
async def test_the_sdks_own_transport_failures_are_still_recognised(failure: str) -> None:
    """Drive the two failures the SDK raises itself and require the predicate to claim both.

    Separating those from a server's own error rests on details this repository
    does not own: the exact wording of the closed-connection message, and the
    fact that the timeout is raised from inside an exception handler. So this
    drives a real session instead of building the exception, and goes red if a
    later SDK changes either one.
    """
    from datetime import timedelta

    from mcp.client.session import ClientSession
    from mcp.shared.exceptions import McpError
    from mcp.shared.memory import create_client_server_memory_streams

    read_timeout = timedelta(seconds=0.2) if failure == "read_timeout" else None
    async with create_client_server_memory_streams() as (
        (client_read, client_write),
        (server_read, server_write),
    ):
        async with ClientSession(
            client_read, client_write, read_timeout_seconds=read_timeout
        ) as session:
            async with anyio.create_task_group() as task_group:

                async def take_the_request_and_fail() -> None:
                    async for _ in server_read:
                        if failure == "connection_closed":
                            await server_write.aclose()
                        # For the timeout arm the request is simply never
                        # answered, which is what the client is timing.
                        return

                task_group.start_soon(take_the_request_and_fail)
                with pytest.raises(McpError) as caught:
                    await session.list_tools()
                task_group.cancel_scope.cancel()

    assert _is_all_isolated_failure(caught.value) is True


# ---------------------------------------------------------------------------
# Verifier-stage isolation.
#
# The dimension stage is only one of three places this engine does work. The
# adversarial verifiers are spawned into the run's background set and drained by
# wait_quiescence(), which re-raises everything except cancellation and budget
# exhaustion; the clean-verify is awaited directly at the end of _run. A worker
# that dies in either place therefore discarded a review whose dimensions had
# already reported, throwing away findings that were already on the run.
# ---------------------------------------------------------------------------


class _DeadVerifierReview(ReviewEngine):
    """The dimension reports a finding; the verifier that finding triggers dies."""

    def __init__(self, failure: BaseException) -> None:
        super().__init__(dimensions=("security",), verify_clean=False)
        self._failure = failure
        self.issues_at_verdict = -1

    async def _review_dimension(self, run, artifact: str, dimension: str) -> None:
        await run.emit(
            IssueFound(
                dimension=dimension,
                description="a finding worth verifying",
                severity="critical",
            )
        )

    async def _verify(self, run, issue) -> None:
        # The sleep is load-bearing, not scene-setting. A spawned task that
        # finishes before the drain is entered removes itself from the active
        # set via its done-callback, so wait_quiescence() finds nothing to
        # collect and the exception is discarded. Failing while still in flight
        # is what puts this arm on the isolation predicate rather than on that
        # timing, and it is also what a real verifier does: it works, then dies.
        await asyncio.sleep(0.05)
        raise self._failure

    async def _verdict(self, run, artifact: str, dimensions: tuple[str, ...]) -> str:
        self.issues_at_verdict = len(run.by_type(IssueFound))
        return "verdict reached"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make_failure", "expected_label"),
    [
        # The class that actually took the lane down. It subclasses
        # ProviderError, so it always qualified for isolation — it escaped
        # because this path had no isolation to qualify for.
        (
            lambda: WorkerLivenessError("worker produced no first stream output"),
            "WorkerLivenessError",
        ),
        (lambda: anyio.ClosedResourceError(), "ClosedResourceError"),
        (lambda: ProviderContextError("provider context overflow"), "ProviderContextError"),
        # Two verifiers dying together is the observed shape: the drain gathers
        # every spawned failure and reports them as one group.
        (
            lambda: ExceptionGroup(
                "unhandled errors in a TaskGroup",
                [
                    WorkerLivenessError("worker produced no first stream output"),
                    WorkerLivenessError("worker produced no first stream output"),
                ],
            ),
            "WorkerLivenessError",
        ),
    ],
)
async def test_a_dead_verifier_does_not_discard_a_review_whose_dimensions_succeeded(
    make_failure, expected_label: str
) -> None:
    events: list[dict] = []
    engine = _DeadVerifierReview(make_failure())

    result = await engine.run("artifact", on_event=events.append)

    assert result == "verdict reached"
    # The evidence the dead verifier was auditing is still on the run. This is
    # the whole point: the findings existed before the verifier was spawned.
    assert engine.issues_at_verdict == 1
    assert result.degraded is True
    assert result.skipped == [f"verify-security ({expected_label})"]
    # The marker has to reach the caller, not just the run: this string is what
    # the CLI folds into the recorded error for the run, so a degraded review
    # says which stage degraded rather than only that something did.
    assert result.degrade_reason == f"emission_failure: verify-security ({expected_label})"
    assert any(
        event["type"] == "verification_failed"
        and event["stage"] == "verify-security"
        and event["error_type"] == expected_label
        for event in events
    )


class _NonTransportVerifierReview(_DeadVerifierReview):
    def __init__(self) -> None:
        super().__init__(ValueError("a genuine defect in the verifier"))


@pytest.mark.asyncio
async def test_a_non_transport_verifier_failure_still_ends_the_run() -> None:
    """Isolation is a claim about transport, not a blanket swallow.

    Without this arm the fix reads identically whether the predicate is
    consulted or the except clause simply discards everything.
    """
    engine = _NonTransportVerifierReview()

    with pytest.raises(ValueError, match="a genuine defect in the verifier"):
        await engine.run("artifact")


class _DeadCleanVerifierReview(ReviewEngine):
    """No issues, so the clean-verify runs — and its worker dies."""

    def __init__(self, failure: BaseException) -> None:
        super().__init__(dimensions=("security",), verify_clean=True)
        self._failure = failure

    async def _review_dimension(self, run, artifact: str, dimension: str) -> None:
        await run.emit(DimensionClean(dimension=dimension, rationale="nothing found"))

    async def _verify_clean(self, run, artifact: str, dimensions: tuple[str, ...]) -> None:
        raise self._failure

    async def _verdict(self, run, artifact: str, dimensions: tuple[str, ...]) -> str:
        return "clean verdict reached"


@pytest.mark.asyncio
async def test_a_dead_clean_verifier_degrades_instead_of_killing_the_run() -> None:
    events: list[dict] = []
    engine = _DeadCleanVerifierReview(WorkerLivenessError("worker produced no first stream output"))

    result = await engine.run("artifact", on_event=events.append)

    assert result == "clean verdict reached"
    assert result.degraded is True
    assert result.skipped == ["verify-clean (WorkerLivenessError)"]
    assert result.degrade_reason == "emission_failure: verify-clean (WorkerLivenessError)"
    assert any(
        event["type"] == "verification_failed"
        and event["stage"] == "verify-clean"
        and event["error_type"] == "WorkerLivenessError"
        for event in events
    )


@pytest.mark.asyncio
async def test_a_non_transport_clean_verifier_failure_still_ends_the_run() -> None:
    engine = _DeadCleanVerifierReview(ValueError("a genuine defect in the clean verifier"))

    with pytest.raises(ValueError, match="a genuine defect in the clean verifier"):
        await engine.run("artifact")


# -- run-wide refusals are not per-dimension blips ----------------------------
# Every one of these derives from ProviderError, so the isolated set matched
# them all and each was recorded as a skipped dimension. The run then reached a
# verdict over an artifact whose dimensions were never read, and the reason was
# a bad credential or a safety refusal rather than a dropped socket.


@pytest.mark.parametrize(
    "failure",
    [
        ProviderAuthError("invalid api key"),
        ProviderQuotaError("rate limit exceeded"),
        ProviderSafetyError("content flagged by a safety filter"),
        ProviderUnsupportedModelError("unknown model"),
    ],
    ids=lambda failure: type(failure).__name__,
)
def test_a_refusal_that_describes_the_run_is_not_isolated(failure: BaseException) -> None:
    assert _is_all_isolated_failure(failure) is False


def test_the_neighbouring_provider_failures_are_still_isolated() -> None:
    """The control for the exclusion: it must not widen into ProviderError itself.

    Without this arm, deleting the whole isolated set passes every assertion
    above, because refusing everything satisfies a suite that only ever asks
    what is refused.
    """
    assert _is_all_isolated_failure(ProviderContextError("provider context overflow")) is True
    assert _is_all_isolated_failure(WorkerLivenessError("no first stream output")) is True
    assert _is_all_isolated_failure(anyio.ClosedResourceError()) is True


def test_a_group_of_transport_failures_carrying_one_refusal_is_not_isolated() -> None:
    # The shape that hides it: several dimensions die of transport at once and
    # one dies of a bad credential. Judged as a group, the majority reads as an
    # ordinary blip.
    group = ExceptionGroup(
        "unhandled errors in a TaskGroup",
        [
            anyio.ClosedResourceError(),
            WorkerLivenessError("no first stream output"),
            ProviderAuthError("invalid api key"),
        ],
    )

    assert _is_all_isolated_failure(group) is False


@pytest.mark.asyncio
async def test_a_clean_verifier_refused_for_credentials_ends_the_run() -> None:
    """The end-to-end arm: the predicate is consulted where it decides a run.

    Its control is the WorkerLivenessError case above, which takes the same
    path with the same engine and degrades to a verdict. One failure class
    reaches a result and the other does not, so this cannot pass by the engine
    having stopped isolating anything.
    """
    engine = _DeadCleanVerifierReview(ProviderAuthError("invalid api key"))

    with pytest.raises(ProviderAuthError, match="invalid api key"):
        await engine.run("artifact")


# ---------------------------------------------------------------------------
# A spawned task's failure has to outlive the task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("settle_before_drain", [True, False], ids=["fast", "slow"])
async def test_a_spawned_failure_is_raised_whether_or_not_it_beat_the_drain(
    settle_before_drain,
) -> None:
    """Timing must not decide whether a run-ending refusal is noticed.

    Spawned tasks take themselves off the active set as they finish. When the
    drain was the only thing reading failures, anything that finished first was
    already gone by the time it looked, and the drain saw an empty set and
    reported nothing wrong.

    That is backwards from which failures matter. A refusal the provider issues
    without doing any work -- a bad key here -- comes back almost at once, so
    the failures that describe the whole run were the ones most reliably lost,
    while a slow one was caught. Both timings are asserted together because
    either alone reads as correct: the slow arm passed throughout.
    """
    run = ReviewEngine().new_run()

    async def refused() -> None:
        if not settle_before_drain:
            await asyncio.sleep(0.05)
        raise ProviderAuthError("invalid api key")

    run.spawn(refused())
    if settle_before_drain:
        # Let it finish, and let its done callback run, before the drain starts.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    with pytest.raises(ProviderAuthError, match="invalid api key"):
        await run.wait_quiescence()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_coro, label",
    [
        (lambda: asyncio.sleep(0), "a task that simply succeeded"),
        (lambda: _raise(EngineBudgetError("exhausted")), "declined discretionary work"),
    ],
)
async def test_the_drain_stays_quiet_for_outcomes_that_are_not_failures(make_coro, label) -> None:
    """The must-not-fire side, so the arm above cannot pass by raising always.

    Collecting failures as tasks settle widens what reaches the drain, and a
    budget refusal is the one outcome that travels this path routinely without
    meaning anything went wrong.
    """
    run = ReviewEngine().new_run()
    run.spawn(make_coro())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await run.wait_quiescence()  # must not raise


async def _raise(exc: BaseException) -> None:
    raise exc


@pytest.mark.asyncio
async def test_a_cancelled_spawned_task_is_not_reported_as_a_failure() -> None:
    """Cancellation is how the engine stops its own work, not a defect.

    Recorded as a failure it would turn every budget stop and deadline into a
    run-ending error, so this is asserted against the same collection path the
    two arms above use.
    """
    run = ReviewEngine().new_run()
    task = run.spawn(asyncio.sleep(3600))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    await run.wait_quiescence()  # must not raise
